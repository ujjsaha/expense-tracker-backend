"""Chat history ORM model (chatbot spec section 7)."""

from datetime import datetime
from enum import StrEnum
from typing import TYPE_CHECKING

from sqlalchemy import Enum as SAEnum
from sqlalchemy import ForeignKey, Index, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.clock import utc_now
from app.db.base import Base
from app.db.types import UTCDateTime

if TYPE_CHECKING:
    from app.models.user import User


class MessageRole(StrEnum):
    USER = "USER"
    AI = "AI"


class ChatMessage(Base):
    __tablename__ = "chat_history"
    __table_args__ = (
        # Supports both memory rehydration and the history endpoint, both of
        # which filter on conversation_id and order by date.
        Index("ix_chat_history_conversation_id_date", "conversation_id", "date"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    role: Mapped[MessageRole] = mapped_column(
        SAEnum(
            MessageRole,
            native_enum=False,
            length=8,
            values_callable=lambda enum_cls: [member.value for member in enum_cls],
        ),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    date: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)

    user: Mapped["User"] = relationship()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ChatMessage id={self.id} conversation_id={self.conversation_id!r} "
            f"role={self.role}>"
        )
