"""Telegram bot setup utility.

Replaces src/whatsapp-auth.ts — verifies the bot token and prints bot info.
Run with: python -m nanoclaw.telegram_setup
"""

from __future__ import annotations

import asyncio
import sys

from .config import TELEGRAM_BOT_TOKEN


async def _verify_bot() -> None:
    """Verify the Telegram bot token and print bot info."""
    from telegram import Bot

    if not TELEGRAM_BOT_TOKEN:
        print("\n❌ TELEGRAM_BOT_TOKEN is not set.")
        print("\nTo set up your Telegram bot:")
        print("  1. Open Telegram and message @BotFather")
        print("  2. Send /newbot and follow the prompts")
        print("  3. Copy the token and set it:")
        print("     export TELEGRAM_BOT_TOKEN='your-token-here'")
        print("  4. Run this script again")
        sys.exit(1)

    print("🔄 Verifying Telegram bot token...")
    try:
        bot = Bot(token=TELEGRAM_BOT_TOKEN)
        me = await bot.get_me()
        print(f"\n✅ Bot verified successfully!")
        print(f"   Name:     {me.full_name}")
        print(f"   Username: @{me.username}")
        print(f"   ID:       {me.id}")
        print(f"\n📋 Next steps:")
        print(f"   1. Add @{me.username} to your Telegram group")
        print(f"   2. Grant the bot admin permissions (to read messages)")
        print(f"   3. Start NanoClaw: python -m nanoclaw")
    except Exception as err:
        print(f"\n❌ Failed to verify bot token: {err}")
        print("   Check that your TELEGRAM_BOT_TOKEN is correct.")
        sys.exit(1)


def main() -> None:
    """Entry point for the telegram setup script."""
    asyncio.run(_verify_bot())


if __name__ == "__main__":
    main()
