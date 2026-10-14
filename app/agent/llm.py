"""Chat model factory for the chatbot agent.

Kept as the single place that knows the LLM provider, so swapping providers
touches only this file (see `.claude/plans/chatbot.md`, decision on LLM choice).
"""

from langchain_openai import ChatOpenAI

from app.core.config import get_settings


def get_chat_model() -> ChatOpenAI:
    settings = get_settings()
    return ChatOpenAI(
        model=settings.chatbot_model,
        api_key=settings.openai_api_key,
        streaming=True,
        timeout=60,
    )
