"""LangGraph agent entrypoint for the chatbot endpoint (chatbot spec section 6).

No checkpointer is configured here - conversation memory is rehydrated by the
caller from `chat_history` on every call (`app/services/chat_service.py`),
which is also what `GET /chatbot/conversations/{id}/history` reads. See
`.claude/plans/chatbot.md` for the rationale (one store, no drift).
"""

import logging
from collections.abc import AsyncIterator

from langchain.agents import create_agent
from langchain_core.messages import AIMessageChunk, BaseMessage, HumanMessage, SystemMessage
from sqlalchemy.orm import Session

from app.agent.llm import get_chat_model
from app.agent.tools import build_tools
from app.models.chat_message import ChatMessage, MessageRole
from app.models.user import User

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = (
    "You are the Expense Tracker's assistant. Answer questions about the "
    "current user's own expenses and profile using the tools provided. "
    "You have no access to any other user's data and must never speculate "
    "about it. Keep answers concise and use the user's currency figures "
    "exactly as the tools report them."
)


def to_lc_messages(history: list[ChatMessage]) -> list[BaseMessage]:
    """`history` must already be oldest-first (see `chat_service.get_conversation_messages`)."""
    lc_messages: list[BaseMessage] = []
    for entry in history:
        if entry.role is MessageRole.USER:
            lc_messages.append(HumanMessage(content=entry.message))
        else:
            lc_messages.append(AIMessageChunk(content=entry.message))
    return lc_messages


async def stream_reply(
    db: Session, user: User, history: list[BaseMessage], text: str
) -> AsyncIterator[str]:
    """Yields text chunks as the agent generates its reply to `text`."""
    agent = create_agent(get_chat_model(), build_tools(db, user))
    messages = [SystemMessage(content=SYSTEM_PROMPT), *history, HumanMessage(content=text)]

    async for chunk, metadata in agent.astream(
        {"messages": messages}, stream_mode="messages"
    ):
        # Tool-call chunks and non-final-node output (e.g. the "tools" node)
        # are filtered out; only the model's own text tokens are streamed.
        if metadata.get("langgraph_node") != "model" or not isinstance(chunk, AIMessageChunk):
            continue

        content = chunk.content
        if isinstance(content, str):
            if content:
                yield content
        else:
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text" and block.get("text"):
                    yield block["text"]
