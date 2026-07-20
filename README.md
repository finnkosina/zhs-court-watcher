# ZHS Court Watcher 🎾

Watches the [ZHS tennis court booking page](https://kurse.zhs-muenchen.de/de/product-offers/21114da0-4246-42b1-bab6-8d7ac49bb14f)
for free slots on the days/times you define in `config.yaml` and pushes a
notification to your phone via [ntfy.sh](https://ntfy.sh). Runs for free on
GitHub Actions every 30 minutes — no laptop needed.

How it works: it logs into kurse.zhs-muenchen.de through the TUM SSO once,
caches the session cookie between runs (so your TUM account sees roughly one
login per day, not one per check), queries the same GraphQL API the booking
page itself uses, and only notifies about slots that are **newly** free — no
repeat notifications for the same slot.

## 1. Get notifications on your phone (2 min)

1. Install the **ntfy** app ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) / [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)).
2. In the app: **Subscribe to topic** → enter your topic name (see below).
   Treat the topic name like a password — anyone who knows it can read your notifications.

## 2. Host it on GitHub Actions (5 min)

1. Create a **private** repository on github.com (e.g. `zhs-watcher`).
2. In the repo: **Settings → Secrets and variables → Actions → New repository secret**, add three secrets:
   - `TUM_USER` — your TUM id (e.g. `ab12cde`)
   - `TUM_PASS` — your TUM password
   - `NTFY_TOPIC` — your ntfy topic name
3. Push this folder to the repo:

   ```
   git remote add origin https://github.com/<you>/zhs-watcher.git
   git push -u origin main
   ```

4. Go to the **Actions** tab, open **Check ZHS courts**, press **Run workflow**
   to test it. After that it runs automatically every 30 minutes
   (07:00–23:00 German time).

## 3. Change what is watched

Edit `config.yaml` (days, time windows, courts) and push. Times are German
local time; a slot is reported when it lies fully inside a window.

## Run locally (optional)

```
pip install -r requirements.txt
set TUM_USER=... & set TUM_PASS=... & set NTFY_TOPIC=...
python checker.py
```

## Notes

- Booking opens 7 days in advance. When a new day becomes bookable, or someone
  cancels, you get a push — tap it to jump straight to the booking page.
- GitHub pauses scheduled workflows after ~60 days without a commit; it emails
  you first. Push any commit to keep it alive.
- If the TUM login fails (e.g. password changed), you get one warning
  notification and checks pause for 6 h before retrying — your account will
  not be hammered with bad logins.
- Never commit your password: credentials live only in GitHub secrets.
