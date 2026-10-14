"""/chatbot endpoints (chatbot spec sections 3, 5).

No `Authorization` bearer guard here, unlike every other router - the spec
has `user_id` arriving directly in the request body rather than a token
(spec section 4.1), so this is treated as a service-to-service endpoint. See
`.claude/plans/chatbot.md` for that decision.
"""

import json
import logging
from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from sqlalchemy.orm import Session

from app.agent import graph as agent_graph
from app.dependencies import DbSession
from app.models.chat_message import MessageRole
from app.schemas.chatbot import (
    ChatHistoryResponse,
    ChatMessageOut,
    ChatQueryRequest,
    ConversationListResponse,
    ConversationSummaryOut,
)
from app.services import chat_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/chatbot", tags=["chatbot"])

_PROCESSING_FAILED_MESSAGE = "Processing failed, please retry."


def _sse(payload: dict) -> str:
    return f"data: {json.dumps(payload)}\n\n"


def _persist_ai_reply(db: Session, conversation_id: str, user_id: int, message: str) -> None:
    """Runs after `[DONE]` has already been sent.

    Reuses the request's own `db` session rather than opening a new one: this
    FastAPI version keeps a `Depends(..., yield)` dependency's cleanup on the
    *outer* per-request exit stack, which only runs after the `StreamingResponse`
    has finished sending its body - so `db` is still open here (verified against
    the installed `fastapi.routing` source; re-check this if FastAPI's dependency
    lifecycle ever changes).

    Spec section 8: a failed write is logged, not surfaced to the client - the
    client has already received the full stream by this point.
    """
    if not message:
        return
    try:
        chat_service.save_message(
            db,
            conversation_id=conversation_id,
            user_id=user_id,
            role=MessageRole.AI,
            message=message,
        )
    except Exception:
        logger.exception("Failed to persist AI reply for conversation_id=%s", conversation_id)


@router.post(
    "/queries",
    summary="Submit a chatbot query and stream the reply",
    response_class=StreamingResponse,
)
def submit_query(payload: ChatQueryRequest, db: DbSession) -> StreamingResponse:
    """Validation and lookup failures below run before any response bytes are
    sent, so they surface as a normal `AppError` -> JSON error response (spec
    section 8: "500 if the failure occurs before streaming begins"). Once
    `StreamingResponse` is returned, headers are already on the wire, so only
    failures *inside* `event_stream` get the in-band SSE error event.
    """
    user = chat_service.resolve_user(db, payload.user_id)
    history = chat_service.get_conversation_messages(
        db, payload.conversation_id, newest_first=False
    )
    chat_service.save_message(
        db,
        conversation_id=payload.conversation_id,
        user_id=user.id,
        role=MessageRole.USER,
        message=payload.text,
    )
    lc_history = agent_graph.to_lc_messages(history)

    async def event_stream() -> AsyncIterator[str]:
        chunks: list[str] = []
        try:
            async for chunk in agent_graph.stream_reply(db, user, lc_history, payload.text):
                chunks.append(chunk)
                yield _sse({"chunk": chunk})
        except Exception:
            logger.exception(
                "Chatbot agent failed mid-stream for conversation_id=%s",
                payload.conversation_id,
            )
            yield _sse({"error": _PROCESSING_FAILED_MESSAGE})
            yield "data: [DONE]\n\n"
            return

        yield "data: [DONE]\n\n"
        _persist_ai_reply(db, payload.conversation_id, user.id, "".join(chunks))

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get(
    "/conversations/{conversation_id}/history",
    response_model=ChatHistoryResponse,
    summary="Fetch the full history of a conversation",
)
def get_history(conversation_id: str, db: DbSession) -> ChatHistoryResponse:
    """An unseeded `conversation_id` is a `200` with an empty list, never a
    `404` - matching the empty-range-is-200 convention already used for
    expense listing rather than treating "no messages yet" as an error.
    """
    messages = chat_service.get_conversation_messages(db, conversation_id, newest_first=True)
    return ChatHistoryResponse(
        conversation_id=conversation_id,
        messages=[
            ChatMessageOut(role=message.role, message=message.message, date=message.date)
            for message in messages
        ],
    )


@router.get(
    "/fetch_all_conversation_ids",
    response_model=ConversationListResponse,
    summary="List every conversation a user has, newest-created-first",
)
def fetch_all_conversation_ids(
    db: DbSession, user_id: Annotated[int, Query(ge=1)]
) -> ConversationListResponse:
    """An unknown `user_id` still 404s via `resolve_user`, for consistency with
    `submit_query` - only an *existing* user with zero conversations gets the
    empty-list `200` spec section 5.4 describes.
    """
    user = chat_service.resolve_user(db, user_id)
    conversations = chat_service.list_conversations_for_user(db, user.id)
    return ConversationListResponse(
        user_id=user.id,
        conversations=[
            ConversationSummaryOut(conversation_id=conversation_id, created_at=created_at)
            for conversation_id, created_at in conversations
        ],
    )
