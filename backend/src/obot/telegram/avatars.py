"""Bot avatars, set by their owners in the factory chat (SPEC 8.7).

After a bot is created the factory offers to set one; /avatar does it later.
The owner then sends a photo (or an image file), or a photo captioned with the
bot's @username at any time. The panel can upload one too.
"""

import io
import logging
from typing import Any

from aiogram import Bot, F, Router
from aiogram.enums import ChatType
from aiogram.filters import BaseFilter, Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from obot.bots.avatar import InvalidImage, square_jpeg
from obot.bots.service import get_or_create_user
from obot.db.models import Bot as BotRecord
from obot.db.models import BotStatus
from obot.i18n import lang_of, t
from obot.telegram.profile import set_avatar
from obot.telegram.runtime import Runtime

log = logging.getLogger(__name__)

router = Router(name="avatars")
router.message.filter(F.chat.type == ChatType.PRIVATE)

# How long the factory waits for the photo after the owner chose a bot.
WAIT_SECONDS = 600


def waiting_key(tg_user_id: int) -> str:
    return f"avatar-wait:{tg_user_id}"


def offer_keyboard(lang: str, bot_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=t(lang, "btn.avatar_upload"), callback_data=f"avatar:{bot_id}"
                ),
                InlineKeyboardButton(text=t(lang, "btn.later"), callback_data="avatar:later"),
            ]
        ]
    )


async def _owned_bots(session: AsyncSession, tg_user_id: int) -> list[BotRecord]:
    owner = await get_or_create_user(session, tg_user_id)
    rows = await session.scalars(
        select(BotRecord)
        .where(BotRecord.owner_id == owner.id, BotRecord.status == BotStatus.ACTIVE)
        .order_by(BotRecord.id)
    )
    return list(rows)


@router.message(Command("avatar"))
async def on_avatar_command(message: Message, session: AsyncSession) -> None:
    lang = lang_of(message.from_user.language_code)
    bots = await _owned_bots(session, message.from_user.id)
    if not bots:
        await message.answer(t(lang, "factory.mybots_empty"))
        return
    rows = [
        [InlineKeyboardButton(text=f"@{b.username}", callback_data=f"avatar:{b.id}")] for b in bots
    ]
    await message.answer(
        t(lang, "avatar.choose_bot"), reply_markup=InlineKeyboardMarkup(inline_keyboard=rows)
    )


@router.callback_query(F.data.startswith("avatar:"))
async def on_avatar_button(query: CallbackQuery, session: AsyncSession, runtime: Runtime) -> None:
    lang = lang_of(query.from_user.language_code)
    choice = query.data.removeprefix("avatar:")
    await query.answer()
    if choice == "later":
        await query.message.edit_reply_markup(reply_markup=None)
        return
    bot = next(
        (b for b in await _owned_bots(session, query.from_user.id) if str(b.id) == choice), None
    )
    if bot is None:
        return
    await runtime.redis.set(waiting_key(query.from_user.id), bot.id, ex=WAIT_SECONDS)
    await query.message.answer(t(lang, "avatar.send_photo", username=bot.username))


class AvatarTarget(BaseFilter):
    """An image meant as a bot's avatar: the factory is waiting for one, or the caption
    names one of the sender's bots."""

    async def __call__(
        self, message: Message, session: AsyncSession, runtime: Runtime, **_: Any
    ) -> bool | dict[str, Any]:
        waiting = await runtime.redis.get(waiting_key(message.from_user.id))
        bots = await _owned_bots(session, message.from_user.id)
        caption = (message.caption or "").lower()
        for bot in bots:
            if (waiting is not None and int(waiting) == bot.id) or (
                f"@{bot.username.lower()}" in caption
            ):
                return {"avatar_bot": bot}
        return False


@router.message(F.photo | F.document.mime_type.startswith("image/"), AvatarTarget())
async def on_avatar_image(
    message: Message, bot: Bot, runtime: Runtime, avatar_bot: BotRecord
) -> None:
    lang = lang_of(message.from_user.language_code)
    await runtime.redis.delete(waiting_key(message.from_user.id))
    file = message.photo[-1] if message.photo else message.document
    data = io.BytesIO()
    await bot.download(file, destination=data)
    try:
        jpeg = square_jpeg(data.getvalue())
    except InvalidImage:
        await message.answer(t(lang, "avatar.invalid"))
        return
    token = runtime.cipher.decrypt(avatar_bot.token_encrypted, str(avatar_bot.tg_bot_id))
    ok = await set_avatar(token, jpeg)
    key = "avatar.done" if ok else "avatar.failed"
    await message.answer(t(lang, key, username=avatar_bot.username))
