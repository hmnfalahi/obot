"""Bot-to-bot conversations in groups, routed inside Obot (SPEC 8.5, P2-3).

Telegram does not show bots each other's messages unless Bot-to-Bot mode is turned
on in BotFather. All Obot bots run here, so when one of them addresses another
in a group we hand the message over ourselves, and loops are controlled centrally:

- a conversation always starts from a human message (its root);
- it lasts at most MAX_BOT_TURNS bot messages;
- a bot repeating text already said in the thread ends it;
- a bot may answer SKIP to end it gracefully.
"""

import hashlib
import re
from dataclasses import dataclass

from redis.asyncio import Redis

MAX_BOT_TURNS = 10
THREAD_TTL_SECONDS = 3600
SKIP = "SKIP"


@dataclass(frozen=True)
class Thread:
    """A group conversation started by the human message `root`."""

    root: int
    depth: int = 0  # bot messages so far

    def next(self) -> "Thread":
        return Thread(self.root, self.depth + 1)

    @property
    def can_continue(self) -> bool:
        return self.depth < MAX_BOT_TURNS


def addresses(text: str, reply_to_user_id: int | None, username: str, tg_bot_id: int) -> bool:
    """Whether a bot's group message is meant for the bot @username."""
    if reply_to_user_id == tg_bot_id:
        return True
    return re.search(rf"@{re.escape(username)}\b", text, flags=re.IGNORECASE) is not None


def is_skip(text: str) -> bool:
    return text.strip().strip("[]().").upper() == SKIP


def bot_note(sender_name: str, thread: Thread) -> str:
    return (
        f"{sender_name}, another bot in this group, addressed you. This is message "
        f"{thread.depth} of at most {MAX_BOT_TURNS} in this bot conversation. Reply briefly "
        "and stay on the topic the people started. If you have nothing useful to add, or "
        f"the question is answered, reply with exactly {SKIP} and nothing else."
    )


def _digest(text: str) -> str:
    normalized = " ".join(text.casefold().split())
    return hashlib.sha256(normalized.encode()).hexdigest()[:32]


async def is_new_in_thread(redis: Redis, chat_id: int, thread: Thread, text: str) -> bool:
    """Record `text` in the thread; False if a bot already said the same thing."""
    key = f"b2b:{chat_id}:{thread.root}"
    async with redis.pipeline(transaction=True) as pipe:
        pipe.sadd(key, _digest(text))
        pipe.expire(key, THREAD_TTL_SECONDS)
        added, _ = await pipe.execute()
    return bool(added)


def peers_note(peers: list[tuple[str, str]]) -> str | None:
    """Tell a bot which other Obot bots in the group it can talk to."""
    if not peers:
        return None
    names = ", ".join(f"@{username} ({name})" for username, name in peers)
    return (
        f"Other bots in this group: {names}. To ask one of them, mention it by its @username "
        "in your reply; do this when people ask you to involve it or its help is clearly "
        "needed, not otherwise."
    )
