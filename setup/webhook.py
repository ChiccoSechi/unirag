"""Registers (or checks) the bot's webhook on Telegram.

Usage:
  uv run python setup/webhook.py https://unirag-inf.YOUR-SUBDOMAIN.workers.dev
  uv run python setup/webhook.py --info        # only the current state

Uses TELEGRAM_BOT_TOKEN and TELEGRAM_WEBHOOK_SECRET from `.env`. Never prints token or secret:
Telegram's API URLs contain the token, so not even those.
"""

import argparse
import logging
import sys

import httpx

from core.config import env_var, load_env

WEBHOOK_PATH = "/telegram"  # same as WEBHOOK_PATH in bot/src/bot.ts

logging.getLogger("httpx").setLevel(logging.WARNING)


def call(method: str, data: dict | None = None) -> dict:
    token = env_var("TELEGRAM_BOT_TOKEN")
    try:
        r = httpx.post(f"https://api.telegram.org/bot{token}/{method}", json=data or {}, timeout=30)
    except httpx.HTTPError as e:
        sys.exit(f"Network error: {type(e).__name__}")
    body = r.json()
    if not body.get("ok"):
        sys.exit(f"Telegram answered {r.status_code}: {body.get('description')}")
    return body["result"]


def show_info() -> None:
    info = call("getWebhookInfo")
    print(f"URL:              {info.get('url') or '(none)'}")
    print(f"Pending updates:  {info.get('pending_update_count', 0)}")
    print(f"Allowed updates:  {info.get('allowed_updates', 'all')}")
    if info.get("last_error_message"):
        print(f"Last error:       {info['last_error_message']}")


def main() -> None:
    args = argparse.ArgumentParser(description="The bot's webhook on Telegram")
    args.add_argument("worker_url", nargs="?", help="address of the Worker (https://...)")
    args.add_argument("--info", action="store_true", help="only show the state")
    a = args.parse_args()
    load_env()

    if not a.info:
        if not a.worker_url or not a.worker_url.startswith("https://"):
            sys.exit("The Worker's address is needed, starting with https://")
        url = a.worker_url.rstrip("/") + WEBHOOK_PATH
        call(
            "setWebhook",
            {
                "url": url,
                "secret_token": env_var("TELEGRAM_WEBHOOK_SECRET"),
                "allowed_updates": ["message"],
                "drop_pending_updates": True,
                "max_connections": 10,
            },
        )
        print("Webhook registered.\n")
    show_info()


if __name__ == "__main__":
    main()
