"""Deciding whether a group message is addressed to the bot (SPEC 3.3, phase 1)."""

import re

from aiogram.enums import MessageEntityType
from aiogram.types import Message


def is_addressed(message: Message, bot_id: int, username: str) -> bool:
    """True for a mention, a reply to the bot's message, or a command aimed at the bot."""
    reply = message.reply_to_message
    if reply and reply.from_user and reply.from_user.id == bot_id:
        return True
    text = message.text or ""
    handle = f"@{username}".lower()
    for entity in message.entities or []:
        fragment = entity.extract_from(text).lower()
        if entity.type == MessageEntityType.MENTION and fragment == handle:
            return True
        if (
            entity.type == MessageEntityType.TEXT_MENTION
            and entity.user
            and entity.user.id == bot_id
        ):
            return True
        if entity.type == MessageEntityType.BOT_COMMAND and fragment.endswith(handle):
            return True
    return False


def strip_mention(text: str, username: str) -> str:
    cleaned = re.sub(rf"@{re.escape(username)}\b", "", text, flags=re.IGNORECASE)
    return re.sub(r"\s{2,}", " ", cleaned).strip()
