"""ZHS court watcher — controlled from your phone via ntfy.

Send a plain message to your ntfy topic to control it (no title needed,
just type into the topic in the ntfy app):

    watch tue 20:00              watch next Tuesday 20:00, any court
    watch 24.07. 18:00 courts 2,5
    watch tomorrow 8pm
    list                         show active watches
    cancel 1                     cancel watch #1
    cancel all                   cancel everything
    help                         show this help

The watcher checks the booking API once per minute (one batched request for
all courts) while at least one watch is active, and pushes an URGENT
notification the moment a watched slot has a free spot.

Environment variables: TUM_USER, TUM_PASS, NTFY_TOPIC
Optional: STATE_DIR, CHECK_INTERVAL_SEC (60), MAX_RUNTIME_MIN (70)
Run with --once for a single check cycle (no loop).
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

BASE = "https://kurse.zhs-muenchen.de"
OFFER_ID = "21114da0-4246-42b1-bab6-8d7ac49bb14f"
OFFER_URL = f"{BASE}/de/product-offers/{OFFER_ID}"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
TZ = ZoneInfo("Europe/Berlin")
UTC = dt.timezone.utc

CHECK_INTERVAL = int(os.environ.get("CHECK_INTERVAL_SEC", "60"))
MAX_RUNTIME_MIN = int(os.environ.get("MAX_RUNTIME_MIN", "70"))
NIGHT_PAUSE = (dt.time(23, 0), dt.time(6, 30))  # no ZHS queries in this local window

STATE_DIR = Path(os.environ.get("STATE_DIR", "state"))
STATE_FILE = STATE_DIR / "state.json"
COOKIE_FILE = STATE_DIR / "cookies.json"

SLOT_FIELDS = "{ start end booking_period_start availability blocked_by_resource }"

WEEKDAYS = {
    "mon": 0, "monday": 0, "mo": 0, "montag": 0,
    "tue": 1, "tuesday": 1, "di": 1, "dienstag": 1, "tues": 1,
    "wed": 2, "wednesday": 2, "mi": 2, "mittwoch": 2,
    "thu": 3, "thursday": 3, "do": 3, "donnerstag": 3, "thur": 3, "thurs": 3,
    "fri": 4, "friday": 4, "fr": 4, "freitag": 4,
    "sat": 5, "saturday": 5, "sa": 5, "samstag": 5,
    "sun": 6, "sunday": 6, "so": 6, "sonntag": 6,
}

HELP_TEXT = """Send a message to this topic to control the watcher:

watch tue 20:00 - watch next Tuesday 20:00 (any court)
watch 24.07. 18:00 courts 2,5 - only courts 2 and 5
watch tomorrow 8pm
list - show active watches
cancel 1 - cancel watch #1
cancel all - cancel everything

You get an URGENT push the moment a watched slot is free."""


# ---------------------------------------------------------------- state

def load_state():
    state = {}
    if STATE_FILE.exists():
        try:
            state = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            state = {}
    state.setdefault("watches", [])
    state.setdefault("next_watch_id", 1)
    state.setdefault("last_poll", time.time() - 300)
    state.setdefault("seen_msg_ids", [])
    state.setdefault("login_failed_at", None)
    state.setdefault("error_notified", False)
    return state


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

class LoginError(Exception):
    pass


class AuthExpired(Exception):
    pass


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

    for _ in range(3):  # Shibboleth client-storage interstitial page(s)
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

    for _ in range(4):  # consent / storage-write interstitials
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
            products = json.loads(raw)["data"]["product_offer"]["products"]
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


def fetch_slots_batch(session, court_ids, start_utc, end_utc):
    """One request for many courts, using GraphQL aliases. Returns {court_id: [slots]}."""
    var_defs = ", ".join(f"$p{i}: UUID!" for i in range(len(court_ids)))
    parts = "\n".join(f"c{i}: booking_slots(product_id: $p{i}, input: $input) {SLOT_FIELDS}"
                      for i in range(len(court_ids)))
    query = f"query Batch({var_defs}, $input: BookingSlotsInput!) {{\n{parts}\n}}"
    variables = {f"p{i}": cid for i, cid in enumerate(court_ids)}
    variables["input"] = {"start": start_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z"),
                          "end": end_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")}
    r = session.post(BASE + "/api/query", json={"query": query, "variables": variables},
                     headers={"Accept-Language": "de_DE"}, timeout=30)
    r.raise_for_status()
    out = r.json()
    if out.get("errors"):
        msg = json.dumps(out["errors"])
        if "unauthenticated" in msg or "401" in msg:
            raise AuthExpired(msg)
        raise RuntimeError(f"booking_slots error: {msg}")
    return {court_ids[int(k[1:])]: (v or []) for k, v in out["data"].items()}


# ---------------------------------------------------------------- watch parsing

def parse_when(text, now=None):
    """Parse 'tue 20:00', '24.07. 18:00', 'tomorrow 8pm', '2026-07-24 18:00' …
    Returns aware local datetime or None."""
    now = now or dt.datetime.now(TZ)
    t = text.strip().lower()
    t = re.sub(r"\b(this|next|on|at|am|um|den|der)\b", " ", t)

    # --- time of day
    hour = minute = None
    m = re.search(r"\b(\d{1,2}):(\d{2})\s*(am|pm|uhr)?\b", t)
    if m:
        hour, minute = int(m.group(1)), int(m.group(2))
        if m.group(3) == "pm" and hour < 12:
            hour += 12
        t = t.replace(m.group(0), " ")
    else:
        m = re.search(r"\b(\d{1,2})\s*(am|pm|uhr)\b", t)
        if m:
            minute = 0
            if m.group(2) == "uhr":
                hour = int(m.group(1))
            else:
                hour = int(m.group(1)) % 12
                if m.group(2) == "pm":
                    hour += 12
            t = t.replace(m.group(0), " ")
    if hour is None:
        return None

    # --- date
    date = None
    m = re.search(r"\b(\d{4})-(\d{2})-(\d{2})\b", t)
    if m:
        date = dt.date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    if date is None:
        m = re.search(r"\b(\d{1,2})\.(\d{1,2})\.?(\d{4})?", t)
        if m:
            year = int(m.group(3)) if m.group(3) else now.year
            date = dt.date(year, int(m.group(2)), int(m.group(1)))
            if not m.group(3) and date < now.date():
                date = date.replace(year=year + 1)
    if date is None:
        if re.search(r"\btomorrow\b|\bmorgen\b", t):
            date = now.date() + dt.timedelta(days=1)
        elif re.search(r"\btoday\b|\bheute\b", t):
            date = now.date()
    if date is None:
        for word in re.findall(r"[a-zäöü]+", t):
            if word in WEEKDAYS:
                ahead = (WEEKDAYS[word] - now.weekday()) % 7
                date = now.date() + dt.timedelta(days=ahead)
                if ahead == 0 and dt.time(hour, minute) <= now.time():
                    date += dt.timedelta(days=7)
                break
    if date is None:
        return None

    return dt.datetime(date.year, date.month, date.day, hour, minute, tzinfo=TZ)


def parse_courts(text):
    """Extract 'courts 2,5' / 'platz 3' → list of ints (empty = any)."""
    m = re.search(r"\b(?:courts?|platz|plätze|plaetze|p)\s*[:.]?\s*([\d,\s]+)", text.lower())
    if not m:
        return []
    return sorted({int(n) for n in re.findall(r"\d+", m.group(1))})


def fmt_when(iso):
    d = dt.datetime.fromisoformat(iso)
    return f"{d:%a %d.%m. %H:%M}"


def fmt_watch(w):
    courts = ", ".join(str(c) for c in w["courts"]) if w["courts"] else "any court"
    return f"#{w['id']} {fmt_when(w['when'])} ({courts})"


# ---------------------------------------------------------------- ntfy

def notify(topic, title, body, priority="default", tags="tennis"):
    r = requests.post(f"https://ntfy.sh/{topic}", data=body.encode("utf-8"),
                      headers={"Title": title, "Priority": priority, "Tags": tags,
                               "Click": OFFER_URL}, timeout=30)
    r.raise_for_status()


def poll_commands(topic, state):
    """Fetch untitled messages (= commands typed by the user) since last poll."""
    since = int(state["last_poll"]) - 5
    r = requests.get(f"https://ntfy.sh/{topic}/json", params={"poll": "1", "since": since},
                     timeout=30)
    r.raise_for_status()
    state["last_poll"] = time.time()
    cmds = []
    for line in r.text.splitlines():
        if not line.strip():
            continue
        try:
            m = json.loads(line)
        except json.JSONDecodeError:
            continue
        if m.get("event") != "message" or m.get("title"):
            continue  # titled messages are our own notifications
        if m.get("id") in state["seen_msg_ids"]:
            continue
        state["seen_msg_ids"] = (state["seen_msg_ids"] + [m["id"]])[-100:]
        cmds.append(m.get("message", ""))
    return cmds


def handle_command(cmd, state, topic):
    text = cmd.strip()
    low = text.lower()
    print(f"command: {text!r}")

    if low in ("help", "?", "h"):
        notify(topic, "ZHS watcher help", HELP_TEXT, tags="information")
        return

    if low in ("list", "status", "l"):
        if state["watches"]:
            body = "\n".join(fmt_watch(w) for w in state["watches"])
        else:
            body = "No active watches. Send e.g. 'watch tue 20:00' to add one."
        notify(topic, "Active watches", body, tags="clipboard")
        return

    if low in ("cancel all", "stop", "clear", "cancel"):
        n = len(state["watches"])
        state["watches"] = []
        notify(topic, "Watches cancelled", f"Removed {n} watch(es).", tags="wastebasket")
        return

    m = re.match(r"(?:cancel|delete|remove)\s+#?(\d+)$", low)
    if m:
        wid = int(m.group(1))
        before = len(state["watches"])
        state["watches"] = [w for w in state["watches"] if w["id"] != wid]
        if len(state["watches"]) < before:
            notify(topic, "Watch cancelled", f"Removed watch #{wid}.", tags="wastebasket")
        else:
            notify(topic, "Not found", f"No watch #{wid}. Send 'list' to see them.",
                   tags="warning")
        return

    # otherwise: treat as a new watch ("watch ..." prefix optional)
    body = re.sub(r"^(?:watch|w)\b", "", low).strip()
    when = parse_when(body)
    if when is None:
        notify(topic, "Did not understand that",
               f"Could not parse '{text}'.\n\n{HELP_TEXT}", tags="warning")
        return
    if when <= dt.datetime.now(TZ):
        notify(topic, "Time is in the past", f"{when:%a %d.%m. %H:%M} already passed.",
               tags="warning")
        return
    courts = parse_courts(body)
    w = {"id": state["next_watch_id"], "when": when.isoformat(), "courts": courts,
         "notified": {}, "fulfilled": False}
    state["next_watch_id"] += 1
    state["watches"].append(w)
    courts_txt = ("courts " + ", ".join(map(str, courts))) if courts else "any court"
    print(f"added watch: {fmt_watch(w)}")
    notify(topic, "Watch added",
           f"Watching {when:%A %d.%m.%Y %H:%M} ({courts_txt}).\n"
           f"You get an urgent push when a spot is free. Checking every minute.",
           tags="white_check_mark")


# ---------------------------------------------------------------- checking

def prune_watches(state, topic):
    now = dt.datetime.now(TZ)
    keep = []
    for w in state["watches"]:
        if dt.datetime.fromisoformat(w["when"]) > now:
            keep.append(w)
        else:
            print(f"watch expired: {fmt_watch(w)}")
            if not w["fulfilled"]:
                notify(topic, "Watch expired",
                       f"{fmt_when(w['when'])} passed without a free spot.",
                       tags="hourglass")
    state["watches"] = keep


def check_watches(session, courts, state, topic):
    """One batched request per watch; notify about newly freed spots."""
    now_utc = dt.datetime.now(UTC)
    for w in state["watches"]:
        target = dt.datetime.fromisoformat(w["when"])
        wanted = [c for c in courts if not w["courts"] or c["number"] in w["courts"]]
        if not wanted:
            continue
        start = (target - dt.timedelta(hours=1)).astimezone(UTC)
        end = (target + dt.timedelta(hours=2)).astimezone(UTC)
        slots_by_court = fetch_slots_batch(session, [c["id"] for c in wanted], start, end)
        names = {c["id"]: c["name"] for c in wanted}

        free = {}
        for cid, slots in slots_by_court.items():
            for slot in slots:
                s = dt.datetime.fromisoformat(slot["start"].replace("Z", "+00:00"))
                e = dt.datetime.fromisoformat(slot["end"].replace("Z", "+00:00"))
                if not (s <= target.astimezone(UTC) < e):
                    continue  # slot does not cover the watched time
                if slot["availability"] <= 0 or slot.get("blocked_by_resource"):
                    continue
                bps = slot.get("booking_period_start")
                if bps and dt.datetime.fromisoformat(bps.replace("Z", "+00:00")) > now_utc:
                    continue  # not bookable yet
                free[f"{cid}|{slot['start']}"] = names[cid]

        new = {k: v for k, v in free.items() if k not in w["notified"]}
        w["notified"] = {k: w["notified"].get(k) or time.time() for k in free}

        if new:
            w["fulfilled"] = True
            courts_txt = ", ".join(sorted(n.replace("Tennisplatz", "Platz") for n in new.values()))
            print(f"FREE for watch #{w['id']}: {courts_txt}")
            notify(topic, f"FREE: {fmt_when(w['when'])}",
                   f"{courts_txt} is free at {fmt_when(w['when'])} — book it now!",
                   priority="urgent", tags="tennis,rotating_light")


def is_night(now=None):
    t = (now or dt.datetime.now(TZ)).time()
    start, end = NIGHT_PAUSE
    return t >= start or t < end


# ---------------------------------------------------------------- main loop

def main():
    # .strip() guards against stray whitespace/newlines pasted into secrets
    user = os.environ["TUM_USER"].strip()
    password = os.environ["TUM_PASS"].strip()
    topic = os.environ["NTFY_TOPIC"].strip()
    once = "--once" in sys.argv

    state = load_state()
    session = requests.Session()
    session.headers["User-Agent"] = UA
    load_cookies(session)

    courts = None          # fetched lazily, once per job
    logged_in = False
    consecutive_errors = 0
    deadline = time.monotonic() + MAX_RUNTIME_MIN * 60

    def ensure_session():
        nonlocal logged_in
        if logged_in:
            return
        if not is_logged_in(session):
            print("no valid session, logging in…")
            login(session, user, password)
            save_cookies(session)
        logged_in = True

    print(f"watcher started: {len(state['watches'])} active watch(es), "
          f"interval {CHECK_INTERVAL}s, max runtime {MAX_RUNTIME_MIN} min")

    while True:
        tick = time.monotonic()
        try:
            for cmd in poll_commands(topic, state):
                handle_command(cmd, state, topic)
            prune_watches(state, topic)

            login_blocked = False
            if state["login_failed_at"]:
                failed = dt.datetime.fromisoformat(state["login_failed_at"])
                login_blocked = dt.datetime.now(UTC) - failed < dt.timedelta(hours=6)
                if not login_blocked:
                    state["login_failed_at"] = None

            if state["watches"] and not login_blocked and not is_night():
                ensure_session()
                if courts is None:
                    courts = fetch_courts(session)
                    print(f"loaded {len(courts)} courts")
                try:
                    check_watches(session, courts, state, topic)
                except AuthExpired:
                    print("session expired, logging in again")
                    logged_in = False
                    session.cookies.clear()
                    ensure_session()
                    check_watches(session, courts, state, topic)
            consecutive_errors = 0
            state["error_notified"] = False

        except LoginError as e:
            print(f"LOGIN FAILED: {e}", file=sys.stderr)
            state["login_failed_at"] = dt.datetime.now(UTC).isoformat()
            if not state["error_notified"]:
                notify(topic, "ZHS watcher: login failed",
                       f"{e}\nChecks are paused for 6 h so the account is not "
                       f"hammered. Commands still work.", tags="warning")
                state["error_notified"] = True
        except Exception as e:
            consecutive_errors += 1
            print(f"ERROR ({consecutive_errors}): {type(e).__name__}: {e}", file=sys.stderr)
            if consecutive_errors == 5 and not state["error_notified"]:
                notify(topic, "ZHS watcher: checks failing",
                       f"{type(e).__name__}: {e}", tags="warning")
                state["error_notified"] = True
            if consecutive_errors >= 5:
                time.sleep(240)  # back off when the site keeps erroring

        save_state(state)
        if once or time.monotonic() > deadline:
            break
        time.sleep(max(5, CHECK_INTERVAL - (time.monotonic() - tick)))

    print("runtime limit reached, exiting (next scheduled run takes over)")


if __name__ == "__main__":
    main()
