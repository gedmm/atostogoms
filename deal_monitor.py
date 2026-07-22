#!/usr/bin/env python3
"""
Flight Deal / Error Fare Monitor
=================================
Polls RSS feeds from travel-deal blogs (TravelFree, Secret Flying,
Fly4Free, The Flight Deal, etc.), filters posts for error/mistake-fare
language (or any keywords you configure), and sends you a notification
the moment a new one appears.

Usage:
    python deal_monitor.py                 # run once
    python deal_monitor.py --loop          # run forever, polling on schedule
    python deal_monitor.py --all           # ignore keyword filter, alert on every post

Setup:
    pip install feedparser pyyaml requests
    cp config.yaml.example config.yaml   # (already provided as config.yaml)
    # edit config.yaml: add/remove feeds, set keywords, enable a notifier
    # set any needed env vars (see README.md), then run.

Scheduling in production:
    - Simplest: `python deal_monitor.py --loop` inside a `screen`/`tmux`
      session or as a systemd service (see deal-monitor.service).
    - Or run `python deal_monitor.py` from cron every N minutes instead
      of using --loop; both approaches dedupe against the same
      seen_items.json so you won't get repeat alerts either way.
"""

import argparse
import hashlib
import json
import logging
import os
import smtplib
import sys
import time
from email.mime.text import MIMEText
from pathlib import Path

import feedparser
import requests
import yaml

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("deal_monitor")

BASE_DIR = Path(__file__).resolve().parent


# ---------------------------------------------------------------------
# Config / state
# ---------------------------------------------------------------------

def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def load_seen(path: Path) -> set:
    if path.exists():
        try:
            return set(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError):
            log.warning("Could not read %s, starting fresh.", path)
    return set()


def save_seen(path: Path, seen: set) -> None:
    # Cap file size: keep the most recent 5000 hashes so it doesn't grow forever.
    trimmed = list(seen)[-5000:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(trimmed), encoding="utf-8")


def item_id(entry) -> str:
    """Stable unique id for a feed entry (guid if present, else hash of link+title)."""
    raw = entry.get("id") or entry.get("link", "") + entry.get("title", "")
    return hashlib.sha256(raw.encode("utf-8", "ignore")).hexdigest()


# ---------------------------------------------------------------------
# Fetching & filtering
# ---------------------------------------------------------------------

def fetch_feed(name: str, url: str):
    try:
        parsed = feedparser.parse(url, request_headers={"User-Agent": "Mozilla/5.0 (deal-monitor/1.0)"})
        if parsed.bozo and not parsed.entries:
            log.warning("Feed '%s' failed to parse cleanly (%s)", name, parsed.get("bozo_exception"))
        return parsed.entries
    except Exception as e:  # noqa: BLE001
        log.error("Error fetching feed '%s' (%s): %s", name, url, e)
        return []


def matches_keywords(text: str, keywords: list) -> bool:
    if not keywords:
        return True
    text_lower = text.lower()
    return any(kw.lower() in text_lower for kw in keywords)


def is_relevant(entry, cfg: dict, ignore_keywords: bool) -> bool:
    title = entry.get("title", "")
    summary = entry.get("summary", "")
    haystack = f"{title} {summary}"

    if not ignore_keywords and not matches_keywords(haystack, cfg.get("error_fare_keywords", [])):
        return False
    if not matches_keywords(haystack, cfg.get("origin_keywords", [])):
        return False
    if not matches_keywords(haystack, cfg.get("destination_keywords", [])):
        return False
    return True


# ---------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------

def notify_console(source: str, entry) -> None:
    print("\n" + "=" * 60)
    print(f"✈️  NEW DEAL — {source}")
    print(entry.get("title", "(no title)"))
    print(entry.get("link", ""))
    print("=" * 60)


def notify_telegram(cfg: dict, source: str, entry) -> None:
    token = os.environ.get(cfg["bot_token_env"], "")
    chat_id = os.environ.get(cfg["chat_id_env"], "")
    if not token or not chat_id:
        log.warning("Telegram enabled but %s/%s env vars not set.", cfg["bot_token_env"], cfg["chat_id_env"])
        return
    text = f"✈️ *{source}*\n{entry.get('title', '')}\n{entry.get('link', '')}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=15,
        )
        if not resp.ok:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("Telegram send error: %s", e)


def notify_discord(cfg: dict, source: str, entry) -> None:
    webhook = os.environ.get(cfg["webhook_url_env"], "")
    if not webhook:
        log.warning("Discord enabled but %s env var not set.", cfg["webhook_url_env"])
        return
    content = f"✈️ **{source}**\n{entry.get('title', '')}\n{entry.get('link', '')}"
    try:
        resp = requests.post(webhook, json={"content": content}, timeout=15)
        if resp.status_code >= 300:
            log.error("Discord send failed: %s %s", resp.status_code, resp.text)
    except requests.RequestException as e:
        log.error("Discord send error: %s", e)


def notify_email(cfg: dict, source: str, entry) -> None:
    from_addr = os.environ.get(cfg["from_addr_env"], "")
    password = os.environ.get(cfg["password_env"], "")
    to_addr = os.environ.get(cfg["to_addr_env"], "")
    if not (from_addr and password and to_addr):
        log.warning("Email enabled but credentials env vars not fully set.")
        return
    body = f"{entry.get('title', '')}\n\n{entry.get('link', '')}\n\nSource: {source}"
    msg = MIMEText(body)
    msg["Subject"] = f"[Deal Alert] {entry.get('title', '')}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
    except Exception as e:  # noqa: BLE001
        log.error("Email send error: %s", e)


def dispatch_notifications(cfg: dict, source: str, entry) -> None:
    notif_cfg = cfg.get("notifications", {})
    if notif_cfg.get("console", True):
        notify_console(source, entry)
    if notif_cfg.get("telegram", {}).get("enabled"):
        notify_telegram(notif_cfg["telegram"], source, entry)
    if notif_cfg.get("discord", {}).get("enabled"):
        notify_discord(notif_cfg["discord"], source, entry)
    if notif_cfg.get("email", {}).get("enabled"):
        notify_email(notif_cfg["email"], source, entry)


# ---------------------------------------------------------------------
# Core run loop
# ---------------------------------------------------------------------

def run_once(cfg: dict, seen_path: Path, ignore_keywords: bool = False) -> int:
    seen = load_seen(seen_path)
    new_count = 0

    for feed in cfg.get("feeds", []):
        name, url = feed["name"], feed["url"]
        entries = fetch_feed(name, url)
        log.info("Checked '%s' — %d entries", name, len(entries))

        for entry in entries:
            eid = item_id(entry)
            if eid in seen:
                continue
            seen.add(eid)  # mark as seen regardless of match, so we don't re-check it forever

            if is_relevant(entry, cfg, ignore_keywords):
                dispatch_notifications(cfg, name, entry)
                new_count += 1

    save_seen(seen_path, seen)
    return new_count


def main():
    parser = argparse.ArgumentParser(description="Monitor travel-deal RSS feeds for error fares.")
    parser.add_argument("--config", default=str(BASE_DIR / "config.yaml"), help="Path to config.yaml")
    parser.add_argument("--loop", action="store_true", help="Run forever, polling on the configured interval")
    parser.add_argument("--all", action="store_true", help="Ignore keyword filters — alert on every new post")
    args = parser.parse_args()

    cfg = load_config(Path(args.config))
    seen_path = BASE_DIR / cfg.get("seen_items_file", "seen_items.json")

    if args.loop:
        interval = cfg.get("poll_interval_minutes", 15) * 60
        log.info("Starting monitor loop — polling every %d minutes. Ctrl+C to stop.", interval // 60)
        while True:
            try:
                found = run_once(cfg, seen_path, ignore_keywords=args.all)
                log.info("Cycle complete — %d new matching deal(s).", found)
            except Exception as e:  # noqa: BLE001 — keep the loop alive on transient errors
                log.error("Unexpected error in cycle: %s", e)
            time.sleep(interval)
    else:
        found = run_once(cfg, seen_path, ignore_keywords=args.all)
        log.info("Done — %d new matching deal(s).", found)
        sys.exit(0)


if __name__ == "__main__":
    main()
