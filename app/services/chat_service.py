"""Chat history persistence (chatbot spec sections 6-8).

Conversation memory is rehydrated from this table on every request rather
than through a separate LangGraph checkpointer - one store, no drift between
what the agent remembers and what the history endpoint reports.
"""

import logging
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.exceptions import NotFoundError
from app.models.chat_message import ChatMessage, MessageRole
from app.models.user import User
from app.services.user_service import get_user_by_id

logger = logging.getLogger(__name__)


def resolve_user(db: Session, user_id: int) -> User:
    """Spec section 4.1: `user_id` arrives directly in the request body, not a
    token, so its existence is checked explicitly here."""
    user = get_user_by_id(db, user_id)
    if user is None:
        raise NotFoundError("User not found")
    return user


def get_conversation_messages(
    db: Session, conversation_id: str, *, newest_first: bool
) -> list[ChatMessage]:
    """Unknown `conversation_id` returns an empty list, never an error (spec
    section 8 - it is treated as the start of a new conversation)."""
    column = ChatMessage.date
    ordering = column.desc() if newest_first else column.asc()
    tiebreak = ChatMessage.id.desc() if newest_first else ChatMessage.id.asc()

    query = (
        select(ChatMessage)
        .where(ChatMessage.conversation_id == conversation_id)
        .order_by(ordering, tiebreak)
    )
    return list(db.scalars(query).all())


def save_message(
    db: Session, *, conversation_id: str, user_id: int, role: MessageRole, message: str
) -> ChatMessage:
    chat_message = ChatMessage(
        conversation_id=conversation_id,
        user_id=user_id,
        role=role,
        message=message,
    )
    db.add(chat_message)
    db.commit()
    db.refresh(chat_message)
    logger.info(
        "Saved %s message id=%s for conversation_id=%s", role.value, chat_message.id,
        conversation_id,
    )
    return chat_message


def list_conversations_for_user(db: Session, user_id: int) -> list[tuple[str, datetime]]:
    """Every distinct conversation_id the user has messages in, paired with its
    earliest message's timestamp (spec section 5.4), newest-created-first.

    An existing user with no conversations yet returns an empty list, not an
    error - the caller is responsible for 404ing an unknown user_id itself
    (via `resolve_user`), which is a separate concern from "no rows found".
    """
    created_at = func.min(ChatMessage.date)
    query = (
        select(ChatMessage.conversation_id, created_at)
        .where(ChatMessage.user_id == user_id)
        .group_by(ChatMessage.conversation_id)
        .order_by(created_at.desc())
    )
    return list(db.execute(query).all())
