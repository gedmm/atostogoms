# Flight Deal / Error Fare Monitor

Polls RSS feeds from travel-deal blogs (TravelFree, Secret Flying, Fly4Free,
The Flight Deal, Thrifty Traveler, Fare Deal Alert — plus any you add) and
notifies you the moment a new post matches your keywords (error fare,
mistake fare, glitch, fuel dump, or your own custom list). It never shows
you the same post twice.

## Why RSS instead of scraping HTML directly?

These sites already publish RSS feeds. Reading those is far more reliable
than parsing raw HTML: feeds don't break every time a site redesigns its
page, they're much lighter-weight, and they're the intended way to consume
this content, so it avoids the maintenance headache (and any ToS friction)
of scraping rendered pages. If you ever want to add a site that has no
feed, that's still possible with `feedparser`+`BeautifulSoup`, but expect
to re-write the parser whenever the site's markup changes — start with RSS
wherever you can.

## 1. Install

```bash
cd deal_monitor
pip install -r requirements.txt
```

## 2. Configure

Open `config.yaml`:
- Add/remove RSS feeds under `feeds:`. Most travel blogs run WordPress, so
  `https://sitename.com/feed/` usually works even for sites not listed.
- Filtering uses a **two-tier rule** — a post is flagged if either is true:
  - **Priority routes**: departs one of your `origin_priority_keywords`
    airports/cities AND price ≤ `price_max_normal_eur` (default €550).
  - **Hot deals**: departs *anywhere* in `origin_europe_keywords` AND
    it's an error/mistake fare (`error_fare_keywords`) OR price ≤
    `price_max_hot_eur` (default €350) — error fares bypass the price
    check entirely, since they're worth knowing about even if expensive.
  - Price is parsed straight out of the post's title/summary text
    (handles €, £, $, PLN, and Nordic "kr", converted to EUR using the
    approximate rates in `currency_to_eur_rates` — update these
    periodically since they'll drift). If no price can be found in the
    text, a post can still match Tier 2 via error-fare language alone,
    but never matches Tier 1 (price is required there).
  - Edit `origin_priority_keywords` to your actual home airports —
    ships with Vilnius, Riga, Warsaw, Copenhagen, Stockholm, and Milan
    by default.
  - Optionally narrow by destination with `destination_keywords`
    (empty = any destination).
- Pick a notification channel below and enable it.

Each alert is tagged so you can tell at a glance why it matched:
`🔥 HOT DEAL (error fare)`, `🔥 HOT DEAL (under hot-deal threshold, ~€180)`,
or `⭐ PRIORITY ROUTE (~€480)`.

## 3. Set up notifications (pick at least one)

Console output is on by default (prints to your terminal) — good for
testing, but you'll want a real notifier for anything time-sensitive,
since error fares often vanish within minutes.

**Telegram (recommended — instant, free, works on your phone):**
1. Message [@BotFather](https://t.me/BotFather) on Telegram, `/newbot`, get a token.
2. Message [@userinfobot](https://t.me/userinfobot) to get your chat ID.
3. Copy `.env.example` to `.env`, fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
4. In `config.yaml`, set `notifications.telegram.enabled: true`.
5. `export $(cat .env | xargs)` before running (or use a process manager that loads `.env`).

**Discord:** create a webhook in your server (Server Settings → Integrations →
Webhooks), put the URL in `.env` as `DISCORD_WEBHOOK_URL`, enable it in config.

**Email (enabled by default in `config.yaml`):**
1. Copy `.env.example` to `.env`.
2. Fill in `DEAL_MONITOR_EMAIL_FROM`, `DEAL_MONITOR_EMAIL_PASSWORD`, `DEAL_MONITOR_EMAIL_TO`.
   - **Gmail**: use an [App Password](https://myaccount.google.com/apppasswords) (requires
     2-Step Verification turned on) — your normal password won't work.
   - **Outlook/Hotmail**: `smtp_host: smtp.office365.com`, port `587`, use your normal
     password or an app password if you have MFA enabled.
   - **Yahoo**: `smtp_host: smtp.mail.yahoo.com`, port `587`, requires an app password.
   - **Custom/work SMTP**: set `smtp_host`/`smtp_port` in `config.yaml` under
     `notifications.email` to match your provider.
3. If you don't want email, set `notifications.email.enabled: false` in `config.yaml`.

Each matching deal sends one email with the title and link — no batching, so you see
error fares the moment they're detected rather than in a daily digest.

## 4. Run it

```bash
# one-off check
python deal_monitor.py

# keep running, polling every N minutes (set in config.yaml)
python deal_monitor.py --loop

# ignore keyword filters, alert on every single new post (useful for testing)
python deal_monitor.py --all
```

## 5. Keep it running long-term

Pick one:

### Option A — GitHub Actions (free, no server needed)

A ready-to-use workflow lives at `.github/workflows/deal-monitor.yml`. It
runs a single check every 15 minutes on GitHub's schedule, then commits
the updated `data/seen_items.json` back to the repo so dedup state
persists between runs — no server of your own required.

1. Push this project to a **public** GitHub repo (public repos get
   unlimited free Actions minutes; private repos get 2,000 free
   min/month, which a 15-min-interval job will exceed). Nothing in this
   codebase is sensitive — credentials live in Secrets, not in the repo.
2. In the repo: **Settings → Secrets and variables → Actions → New
   repository secret**. Add whichever of these your enabled notifiers
   need: `DEAL_MONITOR_EMAIL_FROM`, `DEAL_MONITOR_EMAIL_PASSWORD`,
   `DEAL_MONITOR_EMAIL_TO`, `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`,
   `DISCORD_WEBHOOK_URL`.
3. In **Settings → Actions → General → Workflow permissions**, select
   "Read and write permissions" (needed for the workflow to commit
   `seen_items.json` back).
4. That's it — it starts running on the cron schedule automatically.
   You can also trigger a run manually from the **Actions** tab
   (`Flight Deal Monitor` → **Run workflow**) to test it immediately.
5. Check the **Actions** tab for logs / failures (e.g. a feed URL
   changing) any time.

Note: GitHub's scheduler is best-effort — under high platform load, a
"every 15 minutes" cron can slip by a few extra minutes. Fine for this
use case; if you need tighter timing, use Option B or C below instead.

### Option B — Docker (best if you want it always running on your own machine/server)

```bash
cp .env.example .env      # fill in your notification credentials
docker compose up -d --build
docker compose logs -f    # watch it work / debug
```

This builds the image, starts the monitor in `--loop` mode, and restarts it
automatically on reboot or crash (`restart: unless-stopped`). Two things are
mounted from your host so you don't need to rebuild the image to use them:
- `config.yaml` — edit feeds/keywords anytime, then `docker compose restart`.
- `./data/` — holds `seen_items.json`, so your dedup history survives
  container rebuilds/updates.

To stop it: `docker compose down`. To run a single check instead of the
continuous loop, uncomment the `command:` override in `docker-compose.yml`.

### Option C — Cron
Run `python deal_monitor.py` every 10–15 min via crontab. Dedup state
persists in `seen_items.json`, so this behaves the same as `--loop` in
terms of not repeating alerts.

### Option D — systemd (Linux server/VPS/Raspberry Pi, without Docker)
Use the included `deal-monitor.service`, edit the paths/user, then:
```bash
sudo cp deal-monitor.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now deal-monitor
```

Any of Options B–D works fine on a cheap always-on VPS or a spare
Raspberry Pi if you don't want GitHub Actions or your own machine
running 24/7.

## Notes & limits

- Error fares often live and die within minutes, so poll interval matters —
  15 min is a reasonable default; drop to 5 if you want to be more
  aggressive (be considerate of the sites' servers, though).
- This reads public RSS feeds, which is exactly what they're published
  for — much lighter and more respectful of the source sites than scraping
  full pages repeatedly.
- Feed URLs occasionally change if a site migrates platforms; if a source
  goes quiet, check `sitename.com/feed/` still resolves in a browser.
- `seen_items.json` caps at the most recent 5000 entries so it won't grow
  unbounded.
