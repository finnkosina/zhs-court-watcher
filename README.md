# ZHS Court Watcher 🎾

Watches the [ZHS tennis court booking page](https://kurse.zhs-muenchen.de/de/product-offers/21114da0-4246-42b1-bab6-8d7ac49bb14f)
for the exact date & time you name, checks **every minute**, and sends an
**urgent push notification** the moment a spot frees up. You control it
entirely **from your phone** by typing messages in the ntfy app — no laptop
needed for anything.

## Control it from your phone

Open your topic in the ntfy app and just send a message:

| you send                        | what happens                                  |
|---------------------------------|-----------------------------------------------|
| `watch tue 20:00`               | watch next Tuesday 20:00, any court           |
| `watch 24.07. 18:00 courts 2,5` | same, but only courts 2 and 5                 |
| `watch tomorrow 8pm`            | English/German dates & times both work        |
| `list`                          | shows your active watches                     |
| `cancel 1` / `cancel all`       | remove watch #1 / everything                  |
| `help`                          | command reference                             |

Commands are picked up within a minute and confirmed with a push. When a
watched slot has a free spot you get an **urgent** notification — tap it to
jump straight to the booking page. Watches expire on their own once the time
passes.

## How it works

- Logs into kurse.zhs-muenchen.de through TUM SSO **once**, then reuses the
  cached session (your TUM account sees ~1 login/day, not one per check).
- Checks all watched courts in **one single batched API request per minute** —
  about as much traffic as a person leaving the booking page open.
- Runs on GitHub Actions: the scheduled job loops internally for ~70 min
  checking every 60 s, and the schedule immediately starts the next one —
  continuous coverage, no laptop.
- Checks pause 23:00–06:30 (commands still work). If a login ever fails you
  get one warning and logins pause for 6 h — the account is never hammered.

## Setup (once, ~7 min)

1. **Phone**: install the **ntfy** app
   ([Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy) /
   [iOS](https://apps.apple.com/us/app/ntfy/id1625396347)) and subscribe to
   your topic. Treat the topic name like a password — anyone who knows it can
   read and send.
2. **GitHub**: create a **public** repository (public repos get unlimited free
   Actions minutes; a private one would exhaust the free tier in ~2 days of
   continuous watching — the code contains no secrets, so public is safe).
3. In the repo: *Settings → Secrets and variables → Actions*, add:
   - `TUM_USER` — your TUM id
   - `TUM_PASS` — your TUM password
   - `NTFY_TOPIC` — your ntfy topic name
4. Push this folder:

   ```
   git remote add origin https://github.com/<you>/<repo>.git
   git push -u origin main
   ```

5. *Actions* tab → **Check ZHS courts** → **Run workflow** once to start it.
   From then on the schedule keeps it running.

## Run locally (optional)

```
pip install -r requirements.txt
set TUM_USER=... & set TUM_PASS=... & set NTFY_TOPIC=...
python checker.py          # loop
python checker.py --once   # single check
```

## Notes

- Booking opens 7 days in advance; if you watch a time further out, you're
  notified the moment its booking window opens with a free spot.
- GitHub pauses schedules after ~60 days without a commit (it emails you
  first) — push any commit to revive it.
- Credentials live only in GitHub secrets, never in the code.
