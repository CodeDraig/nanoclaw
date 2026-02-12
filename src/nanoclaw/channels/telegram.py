"""Telegram channel implementation using python-telegram-bot.

Replaces src/channels/whatsapp.ts (284 lines). Uses the Telegram Bot API
via the `python-telegram-bot` library (v21+, async).

Key differences from WhatsApp:
- Auth is a single TELEGRAM_BOT_TOKEN env var (no QR code scanning)
- Chat IDs are numeric (negative for groups, positive for DMs)
- No periodic group metadata sync needed
- Message formatting supports Markdown/HTML natively
- Bot identity is inherent — no need for assistant name prefix
"""

from __future__ import annotations

import asyncio
from typing import Callable, Awaitable

from telegram import Update
from telegram.ext import (
    Application,
    ContextTypes,
    MessageHandler,
    filters,
)

from ..config import ASSISTANT_NAME, TELEGRAM_BOT_TOKEN
from ..logger import logger
from ..types import NewMessage


# Type aliases for callbacks
OnMessageCallback = Callable[[str, NewMessage], Awaitable[None]]
OnMetadataCallback = Callable[[str, str], Awaitable[None]]


class TelegramChannel:
    """Telegram Bot API channel implementation.

    Implements the Channel protocol defined in types.py.
    """

    name: str = "telegram"

    def __init__(
        self,
        on_message: OnMessageCallback,
        on_metadata: OnMetadataCallback,
    ) -> None:
        self._on_message = on_message
        self._on_metadata = on_metadata
        self._app: Application | None = None  # type: ignore[type-arg]
        self._connected: bool = False
        self._bot_id: int | None = None
        self._bot_username: str | None = None

    async def connect(self) -> None:
        """Connect to Telegram and start polling for updates."""
        if not TELEGRAM_BOT_TOKEN:
            raise RuntimeError(
                "TELEGRAM_BOT_TOKEN not set. "
                "Create a bot via @BotFather and set the token."
            )

        self._app = (
            Application.builder()
            .token(TELEGRAM_BOT_TOKEN)
            .build()
        )

        # Register message handler for text messages
        self._app.add_handler(
            MessageHandler(
                filters.TEXT & ~filters.COMMAND,
                self._handle_message,
            )
        )

        # Initialize and get bot info
        await self._app.initialize()
        bot_info = await self._app.bot.get_me()
        self._bot_id = bot_info.id
        self._bot_username = bot_info.username
        logger.info(
            "Telegram bot connected",
            bot_username=self._bot_username,
            bot_id=self._bot_id,
        )

        # Start polling in the background
        await self._app.start()
        await self._app.updater.start_polling(drop_pending_updates=True)  # type: ignore[union-attr]
        self._connected = True

    async def disconnect(self) -> None:
        """Stop the bot and disconnect."""
        if self._app is not None:
            self._connected = False
            if self._app.updater and self._app.updater.running:
                await self._app.updater.stop()
            await self._app.stop()
            await self._app.shutdown()
            logger.info("Telegram bot disconnected")

    def is_connected(self) -> bool:
        """Check if the bot is currently connected."""
        return self._connected

    async def send_message(self, chat_id: str, text: str) -> None:
        """Send a text message to a chat."""
        if self._app is None or not self._connected:
            logger.warning("Cannot send message — bot not connected", chat_id=chat_id)
            return

        try:
            # Split long messages (Telegram limit is 4096 chars)
            max_len = 4096
            if len(text) <= max_len:
                await self._app.bot.send_message(
                    chat_id=int(chat_id),
                    text=text,
                )
            else:
                # Split at line boundaries when possible
                chunks = _split_message(text, max_len)
                for chunk in chunks:
                    await self._app.bot.send_message(
                        chat_id=int(chat_id),
                        text=chunk,
                    )
                    await asyncio.sleep(0.1)  # Brief delay between chunks
        except Exception as err:
            logger.error("Failed to send message", chat_id=chat_id, error=str(err))

    async def set_typing(self, chat_id: str, is_typing: bool) -> None:
        """Send or clear a typing indicator."""
        if self._app is None or not self._connected or not is_typing:
            return
        try:
            await self._app.bot.send_chat_action(
                chat_id=int(chat_id),
                action="typing",
            )
        except Exception:
            pass  # Non-critical

    async def _handle_message(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
    ) -> None:
        """Handle an incoming text message from Telegram."""
        if update.message is None or update.message.text is None:
            return

        msg = update.message
        chat_id = str(msg.chat_id)
        sender_user = msg.from_user

        # Determine sender info
        if sender_user:
            sender_id = str(sender_user.id)
            sender_name = sender_user.full_name or sender_user.username or sender_id
            is_from_me = sender_user.id == self._bot_id
        else:
            sender_id = "unknown"
            sender_name = "Unknown"
            is_from_me = False

        # Build timestamp
        timestamp = msg.date.isoformat() if msg.date else ""

        # Record chat metadata for group discovery
        chat_name = msg.chat.title or msg.chat.full_name or chat_id
        await self._on_metadata(chat_id, timestamp)

        # Build NewMessage
        new_msg = NewMessage(
            id=str(msg.message_id),
            chat_id=chat_id,
            sender=sender_id,
            sender_name=sender_name,
            content=msg.text,
            timestamp=timestamp,
            is_from_me=is_from_me,
        )

        await self._on_message(chat_id, new_msg)

    async def get_chat_name(self, chat_id: str) -> str | None:
        """Fetch the display name for a chat from Telegram."""
        if self._app is None:
            return None
        try:
            chat = await self._app.bot.get_chat(int(chat_id))
            return chat.title or chat.full_name or None
        except Exception:
            return None


def _split_message(text: str, max_len: int) -> list[str]:
    """Split a long message into chunks, preferring line boundaries."""
    chunks: list[str] = []
    while len(text) > max_len:
        # Find the last newline within the limit
        split_at = text.rfind("\n", 0, max_len)
        if split_at == -1 or split_at < max_len // 2:
            # No good newline, split at max_len
            split_at = max_len
        chunks.append(text[:split_at])
        text = text[split_at:].lstrip("\n")
    if text:
        chunks.append(text)
    return chunks
