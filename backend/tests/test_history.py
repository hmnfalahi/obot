from obot.bots.service import ManagedBotInfo, register_managed_bot
from obot.chat.history import ChatScope, recent_messages, record_message
from obot.db.models import SenderKind


async def make_bot(session, cipher, tg_bot_id: int) -> int:
    async def fetch_token(_: int) -> str:
        return f"{tg_bot_id}:token"

    result = await register_managed_bot(
        session, cipher, fetch_token, 1001, ManagedBotInfo(tg_bot_id, f"b{tg_bot_id}", "B")
    )
    return result.bot.id


async def test_history_is_isolated_per_bot_and_chat(session, cipher) -> None:
    bot_a = await make_bot(session, cipher, 1)
    bot_b = await make_bot(session, cipher, 2)
    scopes = [ChatScope(bot_a, 10), ChatScope(bot_a, 20), ChatScope(bot_b, 10)]
    for scope in scopes:
        await record_message(
            session, scope, sender_kind=SenderKind.HUMAN, text=f"{scope.bot_id}/{scope.tg_chat_id}"
        )

    for scope in scopes:
        texts = [m.text for m in await recent_messages(session, scope, limit=10)]
        assert texts == [f"{scope.bot_id}/{scope.tg_chat_id}"]


async def test_recent_messages_returns_latest_oldest_first(session, cipher) -> None:
    scope = ChatScope(await make_bot(session, cipher, 1), 10)
    ids = []
    for i in range(5):
        m = await record_message(session, scope, sender_kind=SenderKind.HUMAN, text=str(i))
        ids.append(m.id)

    assert [m.text for m in await recent_messages(session, scope, limit=3)] == ["2", "3", "4"]
    assert [m.text for m in await recent_messages(session, scope, limit=10, before_id=ids[3])] == [
        "0",
        "1",
        "2",
    ]
