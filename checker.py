"""ZHS court availability watcher.

Logs into kurse.zhs-muenchen.de via TUM SSO (session is cached and reused),
queries the booking API for the courts/days/times defined in config.yaml and
sends a push notification via ntfy.sh when a matching slot is free.

Required environment variables:
  TUM_USER    TUM login (e.g. ab12cde)
  TUM_PASS    TUM password
  NTFY_TOPIC  ntfy.sh topic name to publish notifications to

Optional:
  STATE_DIR   where session + notification state is stored (default: ./state)
"""

import json
import os
import re
import sys
import time
import datetime as dt
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

BASE = "https://kurse.zhs-muenchen.de"
OFFER_ID = "21114da0-4246-42b1-bab6-8d7ac49bb14f"
OFFER_URL = f"{BASE}/de/product-offers/{OFFER_ID}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TZ = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc

SLOTS_QUERY = """query List_product_slots($productID: UUID!, $input: BookingSlotsInput!) {
  booking_slots(product_id: $productID, input: $input) {
    start
    end
    booking_period_start
    booking_period_end
    availability
    blocked_by_resource
 }
}"""

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
STATE_FILE = STATE_DIR / "state.json"
COOKIE_FILE = STATE_DIR / "cookies.json"

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
            "mo": 0, "di": 1, "mi": 2, "do": 3, "fr": 4, "sa": 5, "so": 6}


# ---------------------------------------------------------------- state

def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {"notified": {}, "login_failed_at": None, "error_notified": False}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=1), encoding="utf-8")


def load_cookies(session):
    if not COOKIE_FILE.exists():
        return
    try:
        for c in json.loads(COOKIE_FILE.read_text(encoding="utf-8")):
            session.cookies.set_cookie(requests.cookies.create_cookie(**c))
    except Exception as e:
        print(f"could not load cookies ({e}), starting fresh")


def save_cookies(session):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cookies = [{"name": c.name, "value": c.value, "domain": c.domain,
                "path": c.path, "expires": c.expires, "secure": c.secure}
               for c in session.cookies]
    COOKIE_FILE.write_text(json.dumps(cookies), encoding="utf-8")


# ---------------------------------------------------------------- login

def form_action(resp):
    m = re.search(r'<form[^>]*action="([^"]+)"[^>]*>', resp.text)
    return requests.compat.urljoin(
        resp.url, m.group(1).replace("&#x3a;", ":").replace("&amp;", "&"))


def form_inputs(resp):
    data = {}
    for m in re.finditer(r'<input[^>]*name="([^"]+)"[^>]*>', resp.text):
        tag, name = m.group(0), m.group(1)
        if 'type="checkbox"' in tag or 'type="submit"' in tag:
            continue
        val = re.search(r'value="([^"]*)"', tag)
        data[name] = val.group(1) if val else ""
    return data


class LoginError(Exception):
    pass


def is_logged_in(session):
    r = session.get(BASE + "/services/identity/sessions/whoami", timeout=30)
    return r.status_code == 200


def login(session, user, password):
    """TUM SSO (Shibboleth) login. Raises LoginError on bad credentials."""
    r = session.get(BASE + "/auth/login", params={"return_to": BASE + "/de"}, timeout=30)
    flow_id = re.search(r"flow=([a-f0-9-]{36})", r.url).group(1)
    csrf = re.search(r'name="csrf_token" type="hidden" value="([^"]+)"', r.text).group(1)

    r = session.post(BASE + f"/services/identity/self-service/login?flow={flow_id}",
                     data={"csrf_token": csrf, "method": "oidc", "provider": "oidc-tum"},
                     timeout=30)

    # Shibboleth client-storage interstitial page(s)
    for _ in range(3):
        if 'name="j_username"' in r.text:
            break
        data = form_inputs(r)
        data["_eventId_proceed"] = ""
        r = session.post(form_action(r), data=data, timeout=30)
    if 'name="j_username"' not in r.text:
        raise LoginError(f"never reached TUM credential form (stuck on {r.url})")

    data = form_inputs(r)
    data.update({"j_username": user, "j_password": password, "_eventId_proceed": ""})
    r = session.post(form_action(r), data=data, timeout=30)

    # consent / storage-write interstitials until we are back on the booking site
    for _ in range(4):
        if "login.tum.de" in r.url and 'name="j_password"' in r.text:
            raise LoginError("TUM rejected the credentials (login form shown again)")
        if "kurse.zhs-muenchen.de" in r.url:
            break
        if not re.search(r"<form[^>]*>", r.text):
            break
        data = form_inputs(r)
        data["_eventId_proceed"] = ""
        r = session.post(form_action(r), data=data, timeout=30)

    if not is_logged_in(session):
        raise LoginError(f"SSO flow finished on {r.url} but no session was established")
    print("logged in via TUM SSO")


# ---------------------------------------------------------------- booking API

def fetch_courts(session):
    """The offer page embeds all courts (products) as JSON in an x-data attribute."""
    r = session.get(OFFER_URL, timeout=30)
    r.raise_for_status()
    products = None
    for m in re.finditer(r'x-data="([^"]*product_offer[^"]*)"', r.text):
        raw = m.group(1).replace("&#34;", '"').replace("&quot;", '"').replace("&amp;", "&")
        try:
            data = json.loads(raw)
            products = data["data"]["product_offer"]["products"]
            break
        except (json.JSONDecodeError, KeyError, TypeError):
            continue
    if not products:
        raise RuntimeError("could not find court list on the offer page")
    courts = []
    for p in products:
        num = re.search(r"\d+", p["name"])
        courts.append({"id": p["id"], "name": p["name"].strip(),
                       "number": int(num.group(0)) if num else None})
    return sorted(courts, key=lambda c: (c["number"] is None, c["number"]))


def fetch_slots(session, product_id, start, end):
    r = session.post(
        BASE + "/api/query",
        json={"query": SLOTS_QUERY,
              "variables": {"productID": product_id,
                            "input": {"start": start.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                                      "end": end.strftime("%Y-%m-%dT%H:%M:%S.000Z")}}},
        headers={"Accept-Language": "de_DE"}, timeout=30)
    r.raise_for_status()
    out = r.json()
    if out.get("errors"):
        raise RuntimeError(f"booking_slots error: {out['errors']}")
    return out["data"]["booking_slots"] or []


# ---------------------------------------------------------------- matching

def parse_ts(s):
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(TZ)


def parse_hhmm(s):
    h, m = str(s).split(":")
    return dt.time(int(h), int(m))


def court_wanted(court, wanted):
    if not wanted:
        return True
    for w in wanted:
        if isinstance(w, int) or str(w).isdigit():
            if court["number"] == int(w):
                return True
        elif str(w).casefold() in court["name"].casefold():
            return True
    return False


def slot_matches(slot_start, slot_end, watches):
    for w in watches:
        w_start, w_end = parse_hhmm(w["start"]), parse_hhmm(w["end"])
        for d in w["days"]:
            d = str(d).strip().casefold()
            if d in WEEKDAYS:
                if slot_start.weekday() != WEEKDAYS[d]:
                    continue
            else:  # explicit date like 2026-07-25
                if slot_start.date() != dt.date.fromisoformat(d):
                    continue
            if slot_start.time() >= w_start and slot_end.time() <= w_end:
                return True
    return False


# ---------------------------------------------------------------- notify

def notify(topic, title, body, priority="high", tags="tennis"):
    r = requests.post(
        f"https://ntfy.sh/{topic}",
        data=body.encode("utf-8"),
        headers={"Title": title, "Priority": priority, "Tags": tags,
                 "Click": OFFER_URL},
        timeout=30)
    r.raise_for_status()


# ---------------------------------------------------------------- main

def main():
    user = os.environ["TUM_USER"]
    password = os.environ["TUM_PASS"]
    topic = os.environ["NTFY_TOPIC"]

    cfg = yaml.safe_load(Path(__file__).with_name("config.yaml").read_text(encoding="utf-8"))
    watches = cfg.get("watches") or []
    wanted_courts = cfg.get("courts") or []
    days_ahead = int(cfg.get("days_ahead", 8))
    if not watches:
        print("config.yaml has no watches defined — nothing to do")
        return

    state = load_state()

    # if the last login attempt failed, back off for 6 h so the account
    # is not hammered with bad credentials
    if state.get("login_failed_at"):
        failed = dt.datetime.fromisoformat(state["login_failed_at"])
        if dt.datetime.now(UTC) - failed < dt.timedelta(hours=6):
            print("skipping run: last login failed less than 6 h ago")
            return

    session = requests.Session()
    session.headers["User-Agent"] = UA
    load_cookies(session)

    try:
        if not is_logged_in(session):
            print("no valid session, logging in…")
            login(session, user, password)
        save_cookies(session)
        state["login_failed_at"] = None

        courts = [c for c in fetch_courts(session) if court_wanted(c, wanted_courts)]
        print(f"checking {len(courts)} courts, {days_ahead} days ahead")

        now = dt.datetime.now(UTC)
        start = dt.datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(UTC)
        end = start + dt.timedelta(days=days_ahead)

        free = {}  # key -> info
        for court in courts:
            for slot in fetch_slots(session, court["id"], start, end):
                if slot["availability"] <= 0 or slot.get("blocked_by_resource"):
                    continue
                bps = slot.get("booking_period_start")
                if bps and dt.datetime.fromisoformat(bps.replace("Z", "+00:00")) > now:
                    continue  # not bookable yet — would be noise
                s, e = parse_ts(slot["start"]), parse_ts(slot["end"])
                if not slot_matches(s, e, watches):
                    continue
                key = f"{court['id']}|{slot['start']}"
                free[key] = {"court": court["name"], "start": s, "end": e}
            time.sleep(0.7)  # be gentle with the API

        # notify only about slots that were not free on the previous run
        new = {k: v for k, v in free.items() if k not in state["notified"]}
        # slots that are gone can trigger again if they free up later
        state["notified"] = {k: state["notified"].get(k) or dt.datetime.now(UTC).isoformat()
                             for k in free}

        if new:
            by_time = {}
            for v in new.values():
                label = f"{v['start']:%a %d.%m. %H:%M}–{v['end']:%H:%M}"
                by_time.setdefault((v["start"], label), []).append(v["court"])
            lines = []
            for (_, label), names in sorted(by_time.items()):
                short = ", ".join(n.replace("Tennisplatz", "Platz") for n in sorted(names))
                lines.append(f"{label}: {short}")
            body = "\n".join(lines)
            title = f"{len(new)} free court slot{'s' if len(new) > 1 else ''} at ZHS!"
            print("NOTIFY:\n" + body)
            notify(topic, title, body)
        else:
            print(f"no new slots ({len(free)} matching slots already known)")

        state["error_notified"] = False

    except LoginError as e:
        print(f"LOGIN FAILED: {e}", file=sys.stderr)
        state["login_failed_at"] = dt.datetime.now(UTC).isoformat()
        if not state.get("error_notified"):
            notify(topic, "ZHS watcher: login failed",
                   f"{e}\nChecks are paused for 6 h. Fix credentials if this persists.",
                   priority="default", tags="warning")
            state["error_notified"] = True
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        if not state.get("error_notified"):
            notify(topic, "ZHS watcher: check failed",
                   f"{type(e).__name__}: {e}", priority="default", tags="warning")
            state["error_notified"] = True
        raise
    finally:
        save_state(state)


if __name__ == "__main__":
    main()
