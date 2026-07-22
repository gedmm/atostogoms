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
import re
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


# Price patterns, checked in this order, first match in the text wins
# (deal-blog titles almost always lead with the headline price, so the
# earliest match in the string is the one we want, not the cheapest).
# Each tuple is (compiled regex with one capture group for the number, currency code).
_PRICE_PATTERNS = [
    (re.compile(r'(?:€|EUR)\s?(\d[\d.,]*)', re.I), "EUR"),
    (re.compile(r'(\d[\d.,]*)\s?(?:€|EUR)\b', re.I), "EUR"),
    (re.compile(r'(?:£|GBP)\s?(\d[\d.,]*)', re.I), "GBP"),
    (re.compile(r'(\d[\d.,]*)\s?(?:£|GBP)\b', re.I), "GBP"),
    (re.compile(r'(?:\$|USD)\s?(\d[\d.,]*)', re.I), "USD"),
    (re.compile(r'(\d[\d.,]*)\s?(?:\$|USD)\b', re.I), "USD"),
    (re.compile(r'(\d[\d.,]*)\s?(?:zł|PLN)\b', re.I), "PLN"),
    (re.compile(r'(\d[\d.,]*)\s?(?:kr|SEK|DKK|NOK)\b', re.I), "SEK"),
]


def extract_price_eur(text: str, rates: dict):
    """Find the first price mentioned in text and convert it to EUR.

    Returns (eur_amount, original_amount, currency_code) or None if no
    price pattern is found at all.
    """
    best = None  # (position, eur_amount, original_amount, currency)
    for pattern, currency in _PRICE_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        if best is not None and m.start() >= best[0]:
            continue  # a match earlier in the string already wins
        raw = m.group(1).replace(",", "")
        try:
            amount = float(raw)
        except ValueError:
            continue
        rate = rates.get(currency, 1.0)
        best = (m.start(), amount * rate, amount, currency)

    if best is None:
        return None
    _, eur_amount, original_amount, currency = best
    return eur_amount, original_amount, currency


def evaluate_deal(entry, cfg: dict, ignore_keywords: bool = False):
    """Decide whether a post matches, and why.

    Two-tier rule:
      Tier 1 ("priority"): departs a priority airport/city AND price <= price_max_normal_eur.
      Tier 2 ("hot"): departs ANYWHERE in Europe AND (error-fare language OR price <= price_max_hot_eur).

    Returns a dict with keys: matched (bool), tier (str|None), price_eur,
    price_original, currency, is_error_fare (bool). Always returns a dict
    (never None) so callers can inspect price info even on a non-match.
    """
    title = entry.get("title", "")
    summary = entry.get("summary", "")
    haystack = f"{title} {summary}"

    price_info = extract_price_eur(haystack, cfg.get("currency_to_eur_rates", {}))
    price_eur, price_original, currency = price_info if price_info else (None, None, None)

    is_error_fare = matches_keywords(haystack, cfg.get("error_fare_keywords", []))
    in_priority_cities = matches_keywords(haystack, cfg.get("origin_priority_keywords", []))
    in_europe = matches_keywords(haystack, cfg.get("origin_europe_keywords", []))
    dest_ok = matches_keywords(haystack, cfg.get("destination_keywords", []))

    result = {
        "matched": False,
        "tier": None,
        "price_eur": price_eur,
        "price_original": price_original,
        "currency": currency,
        "is_error_fare": is_error_fare,
    }

    if not dest_ok:
        return result

    if ignore_keywords:
        result["matched"] = True
        result["tier"] = "test"
        return result

    price_max_hot = cfg.get("price_max_hot_eur", 350)
    price_max_normal = cfg.get("price_max_normal_eur", 550)

    # Tier 2 first: any European origin, error fare OR very cheap.
    if in_europe and (is_error_fare or (price_eur is not None and price_eur <= price_max_hot)):
        result["matched"] = True
        result["tier"] = "hot"
        return result

    # Tier 1: priority route, under the normal price cap. Price must be known.
    if in_priority_cities and price_eur is not None and price_eur <= price_max_normal:
        result["matched"] = True
        result["tier"] = "priority"
        return result

    return result


# ---------------------------------------------------------------------
# Notifiers
# ---------------------------------------------------------------------

def build_label(match: dict) -> str:
    """Human-readable tag describing why a deal matched, for use in alerts."""
    tier = match.get("tier")
    price_eur = match.get("price_eur")
    price_bit = f"~€{price_eur:.0f}" if price_eur is not None else "price unknown"

    if tier == "hot":
        reason = "error fare" if match.get("is_error_fare") else f"under hot-deal threshold, {price_bit}"
        return f"🔥 HOT DEAL ({reason})"
    if tier == "priority":
        return f"⭐ PRIORITY ROUTE ({price_bit})"
    if tier == "test":
        return "🧪 TEST MODE"
    return "✈️ DEAL"


def notify_console(source: str, entry, match: dict) -> None:
    print("\n" + "=" * 60)
    print(f"{build_label(match)} — {source}")
    print(entry.get("title", "(no title)"))
    print(entry.get("link", ""))
    print("=" * 60)


def notify_telegram(cfg: dict, source: str, entry, match: dict) -> bool:
    token = os.environ.get(cfg["bot_token_env"], "")
    chat_id = os.environ.get(cfg["chat_id_env"], "")
    if not token or not chat_id:
        log.warning("Telegram enabled but %s/%s env vars not set.", cfg["bot_token_env"], cfg["chat_id_env"])
        return False
    text = f"{build_label(match)}\n*{source}*\n{entry.get('title', '')}\n{entry.get('link', '')}"
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            data={"chat_id": chat_id, "text": text, "parse_mode": "Markdown", "disable_web_page_preview": False},
            timeout=15,
        )
        if not resp.ok:
            log.error("Telegram send failed: %s %s", resp.status_code, resp.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("Telegram send error: %s", e)
        return False


def notify_discord(cfg: dict, source: str, entry, match: dict) -> bool:
    webhook = os.environ.get(cfg["webhook_url_env"], "")
    if not webhook:
        log.warning("Discord enabled but %s env var not set.", cfg["webhook_url_env"])
        return False
    content = f"{build_label(match)}\n**{source}**\n{entry.get('title', '')}\n{entry.get('link', '')}"
    try:
        resp = requests.post(webhook, json={"content": content}, timeout=15)
        if resp.status_code >= 300:
            log.error("Discord send failed: %s %s", resp.status_code, resp.text)
            return False
        return True
    except requests.RequestException as e:
        log.error("Discord send error: %s", e)
        return False


def notify_email(cfg: dict, source: str, entry, match: dict) -> bool:
    from_addr = os.environ.get(cfg["from_addr_env"], "")
    password = os.environ.get(cfg["password_env"], "")
    to_addr = os.environ.get(cfg["to_addr_env"], "")
    if not (from_addr and password and to_addr):
        log.warning("Email enabled but credentials env vars not fully set.")
        return False
    label = build_label(match)
    body = f"{label}\n\n{entry.get('title', '')}\n\n{entry.get('link', '')}\n\nSource: {source}"
    msg = MIMEText(body)
    msg["Subject"] = f"[Deal Alert] {label} — {entry.get('title', '')}"
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"]) as server:
            server.starttls()
            server.login(from_addr, password)
            server.sendmail(from_addr, [to_addr], msg.as_string())
        return True
    except Exception as e:  # noqa: BLE001
        log.error("Email send error: %s", e)
        return False


def dispatch_notifications(cfg: dict, source: str, entry, match: dict) -> bool:
    """Fire all enabled notifiers for one matching entry.

    Returns False if ANY enabled channel failed to deliver, so the caller
    can surface that as a run failure rather than silently succeeding.
    """
    notif_cfg = cfg.get("notifications", {})
    all_ok = True
    if notif_cfg.get("console", True):
        notify_console(source, entry, match)  # console can't meaningfully "fail"
    if notif_cfg.get("telegram", {}).get("enabled"):
        all_ok = notify_telegram(notif_cfg["telegram"], source, entry, match) and all_ok
    if notif_cfg.get("discord", {}).get("enabled"):
        all_ok = notify_discord(notif_cfg["discord"], source, entry, match) and all_ok
    if notif_cfg.get("email", {}).get("enabled"):
        all_ok = notify_email(notif_cfg["email"], source, entry, match) and all_ok
    return all_ok


# ---------------------------------------------------------------------
# Core run loop
# ---------------------------------------------------------------------

def run_once(cfg: dict, seen_path: Path, ignore_keywords: bool = False) -> tuple[int, int]:
    """Returns (new_matching_deals, notification_failures)."""
    seen = load_seen(seen_path)
    new_count = 0
    failure_count = 0

    for feed in cfg.get("feeds", []):
        name, url = feed["name"], feed["url"]
        entries = fetch_feed(name, url)
        log.info("Checked '%s' — %d entries", name, len(entries))

        for entry in entries:
            eid = item_id(entry)
            if eid in seen:
                continue
            seen.add(eid)  # mark as seen regardless of match, so we don't re-check it forever

            match = evaluate_deal(entry, cfg, ignore_keywords)
            if match["matched"]:
                new_count += 1
                if not dispatch_notifications(cfg, name, entry, match):
                    failure_count += 1
                    log.error("Notification delivery FAILED for: %s", entry.get("title", ""))

    save_seen(seen_path, seen)
    return new_count, failure_count


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
                found, failures = run_once(cfg, seen_path, ignore_keywords=args.all)
                log.info("Cycle complete — %d new matching deal(s), %d notification failure(s).", found, failures)
            except Exception as e:  # noqa: BLE001 — keep the loop alive on transient errors
                log.error("Unexpected error in cycle: %s", e)
            time.sleep(interval)
    else:
        found, failures = run_once(cfg, seen_path, ignore_keywords=args.all)
        log.info("Done — %d new matching deal(s), %d notification failure(s).", found, failures)
        if failures:
            log.error(
                "%d deal(s) were found but NOT successfully delivered to a notification channel. "
                "Exiting with an error so this run is visible as a failure.", failures
            )
            sys.exit(1)
        sys.exit(0)


if __name__ == "__main__":
    main()
