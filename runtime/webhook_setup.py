"""Stage 2 Phase 1 — webhook URL helper + (guarded) registration.

Phase 1: dry-run only. Prints the webhook URL Telegram's setWebhook would be
called with. Actually calling Telegram requires the explicit --apply flag —
never invoked automatically, never called from webhook_app.py or main.py.
"""

from __future__ import annotations

import argparse
import asyncio
import os

from aiogram import Bot


def build_webhook_url(base_url: str, bot_id: int) -> str:
    return f"{base_url.rstrip('/')}/webhook/{bot_id}"


async def set_webhook_for_bot(token: str, base_url: str, bot_id: int, secret: str) -> None:
    """Actually calls Telegram's setWebhook. Only reached via --apply (see main())."""
    url = build_webhook_url(base_url, bot_id)
    bot = Bot(token=token)
    try:
        await bot.set_webhook(url=url, secret_token=secret)
    finally:
        await bot.session.close()


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Compute a bot's webhook URL. Phase 1 default is a dry run (prints the "
            "URL only) — pass --apply to actually call Telegram's setWebhook."
        )
    )
    parser.add_argument("bot_id", type=int)
    parser.add_argument(
        "--base-url",
        default=os.getenv("PUBLIC_BASE_URL", ""),
        help="Public HTTPS base URL, e.g. https://your-app.up.railway.app",
    )
    parser.add_argument("--token", default="", help="Bot token — required only with --apply")
    parser.add_argument("--secret", default=os.getenv("WEBHOOK_SECRET", ""))
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Actually call Telegram's setWebhook. Off by default in Phase 1.",
    )
    args = parser.parse_args()

    if not args.base_url:
        raise SystemExit("--base-url or PUBLIC_BASE_URL env var is required")

    url = build_webhook_url(args.base_url, args.bot_id)
    print(f"Webhook URL for bot {args.bot_id}: {url}")

    if not args.apply:
        print("Dry run (Phase 1 default) — pass --apply to actually register it with Telegram.")
        return

    if not args.token:
        raise SystemExit("--token is required with --apply")
    if not args.secret:
        raise SystemExit("--secret or WEBHOOK_SECRET env var is required with --apply")

    asyncio.run(set_webhook_for_bot(args.token, args.base_url, args.bot_id, args.secret))
    print("Webhook registered with Telegram.")


if __name__ == "__main__":
    main()
