from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from obot.core.agent import BotProfile, Turn, build_instructions, generate_reply
from obot.telegram.sender import split_text


def test_instructions_use_persona_or_default() -> None:
    custom = build_instructions(BotProfile("Rex", {"system_prompt": "You are a pirate."}))
    default = build_instructions(BotProfile("Rex"))

    assert "Your name is Rex." in custom
    assert "You are a pirate." in custom
    assert "helpful, friendly assistant" in default
    assert "plain text" in default


async def test_generate_reply_sends_instructions_and_history() -> None:
    seen: list[ModelMessage] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.extend(messages)
        return ModelResponse(parts=[TextPart("hello back")])

    history = [Turn("user", "hi"), Turn("assistant", "hey")]
    reply = await generate_reply(
        FunctionModel(respond), BotProfile("Rex", {"system_prompt": "Be brief."}), history, "how?"
    )

    assert reply.text == "hello back"
    requests = [m for m in seen if isinstance(m, ModelRequest)]
    assert "Be brief." in requests[-1].instructions
    prompts = [p.content for m in requests for p in m.parts if p.part_kind == "user-prompt"]
    assert prompts == ["hi", "how?"]
    assert any(isinstance(m, ModelResponse) and m.parts[0].content == "hey" for m in seen)


def test_split_text_respects_limit_and_boundaries() -> None:
    text = "a" * 6 + "\n\n" + "b" * 6 + " " + "c" * 12

    chunks = split_text(text, limit=10)

    assert all(len(c) <= 10 for c in chunks)
    assert chunks[:2] == ["a" * 6, "b" * 6]
    assert "".join(chunks) == text.replace("\n", "").replace(" ", "")


def test_split_text_short_text_is_single_chunk() -> None:
    assert split_text("hello") == ["hello"]
