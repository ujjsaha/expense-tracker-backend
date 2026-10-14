"""Chatbot request/response schemas (spec sections 4-5, 7)."""

from typing import Annotated

from pydantic import BaseModel, Field, StringConstraints

from app.models.chat_message import MessageRole
from app.schemas.common import IsoUtcDateTime

QueryTextStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
ConversationIdStr = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=64)
]


class ChatQueryRequest(BaseModel):
    """Body of `POST /chatbot/queries` (spec section 4.1)."""

    text: QueryTextStr = Field(examples=["What were my total expenses last month?"])
    conversation_id: ConversationIdStr = Field(examples=["conv_12345"])
    user_id: int = Field(ge=1, examples=[6789])


class ChatMessageOut(BaseModel):
    """One row of `GET /chatbot/conversations/{conversation_id}/history` (spec section 5.3)."""

    role: MessageRole
    message: str
    date: IsoUtcDateTime


class ChatHistoryResponse(BaseModel):
    conversation_id: str
    messages: list[ChatMessageOut]


class ConversationSummaryOut(BaseModel):
    """One entry of `GET /chatbot/fetch_all_conversation_ids` (spec section 5.4).

    `created_at` is the timestamp of the conversation's first message, not its
    most recent activity - see `chat_service.list_conversations_for_user`.
    """

    conversation_id: str
    created_at: IsoUtcDateTime


class ConversationListResponse(BaseModel):
    user_id: int
    conversations: list[ConversationSummaryOut]
