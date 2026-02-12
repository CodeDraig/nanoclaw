"""Message formatting utilities.

Port of src/router.ts — pure string operations for formatting messages
for agent input and outbound display.
"""

from __future__ import annotations

from .types import NewMessage


def escape_xml(s: str) -> str:
    """Escape XML special characters in a string."""
    return (
        s.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def format_messages(messages: list[NewMessage]) -> str:
    """Format a list of messages into an XML-like structure for the agent.

    Each message is wrapped in <message> tags with sender metadata.
    """
    if not messages:
        return ""

    parts: list[str] = []
    for msg in messages:
        sender_attr = escape_xml(msg.sender_name or msg.sender)
        content = escape_xml(msg.content)
        parts.append(
            f'<message sender="{sender_attr}" timestamp="{msg.timestamp}">'
            f"{content}"
            f"</message>"
        )

    return "\n".join(parts)


def format_outbound(text: str) -> str:
    """Format an outbound message from the bot.

    For Telegram, the bot's identity is inherent in the message sender,
    so no prefix is needed (unlike WhatsApp which required "Andy: " prefix).
    """
    return text.strip()
