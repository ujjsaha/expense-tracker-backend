"""Chatbot endpoint tests (chatbot spec sections 3-8).

`app.agent.graph.stream_reply` is monkeypatched to a canned async generator
in every test here - the LangGraph agent itself is never invoked, so the
suite exercises the router, service layer, and persisted `chat_history` rows
without needing ANTHROPIC_API_KEY or a live model call.
"""

import json
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.chat_message import ChatMessage, MessageRole

CONVERSATION_ID = "conv-1"


def _sse_events(body: str) -> list[dict | str]:
    """Parses `data: ...` lines back into JSON payloads; `[DONE]` stays a string."""
    events: list[dict | str] = []
    for line in body.splitlines():
        if not line.startswith("data: "):
            continue
        raw = line[len("data: ") :]
        events.append(raw if raw == "[DONE]" else json.loads(raw))
    return events


async def _echo_stream_reply(db, user, history, text) -> AsyncIterator[str]:
    for chunk in [f"echo:{text}", " done"]:
        yield chunk


async def _failing_stream_reply(db, user, history, text) -> AsyncIterator[str]:
    yield "Partial"
    raise RuntimeError("boom")


@pytest.fixture(autouse=True)
def stub_agent(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("app.agent.graph.stream_reply", _echo_stream_reply)


def _submit(client: TestClient, user_id: int, text: str, conversation_id: str = CONVERSATION_ID):
    return client.post(
        "/chatbot/queries",
        json={"text": text, "conversation_id": conversation_id, "user_id": user_id},
    )


def _seed_message(
    db: Session, *, conversation_id: str, user_id: int, role: MessageRole, message: str, at: datetime
) -> ChatMessage:
    """Inserts a `ChatMessage` with an explicit `date`, bypassing the router -
    used to control ordering precisely rather than relying on real-clock gaps
    between fast, sequential `_submit` calls."""
    row = ChatMessage(
        conversation_id=conversation_id, user_id=user_id, role=role, message=message, date=at
    )
    db.add(row)
    db.commit()
    return row


# --------------------------------------------------------------------------- #
# POST /chatbot/queries
# --------------------------------------------------------------------------- #


def test_submit_query_streams_and_persists_both_messages(
    client: TestClient, db: Session, user_a: dict
):
    response = _submit(client, user_a["user_id"], "What did I spend last month?")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")

    events = _sse_events(response.text)
    assert events == [
        {"chunk": "echo:What did I spend last month?"},
        {"chunk": " done"},
        "[DONE]",
    ]

    rows = db.scalars(
        select(ChatMessage)
        .where(ChatMessage.conversation_id == CONVERSATION_ID)
        .order_by(ChatMessage.id)
    ).all()
    assert [row.role for row in rows] == [MessageRole.USER, MessageRole.AI]
    assert rows[0].message == "What did I spend last month?"
    assert rows[1].message == "echo:What did I spend last month? done"
    assert rows[0].user_id == rows[1].user_id == user_a["user_id"]


def test_second_turn_passes_prior_history_to_agent(
    client: TestClient, user_a: dict, monkeypatch: pytest.MonkeyPatch
):
    seen_history: list[list] = []

    async def _capturing_stream_reply(db, user, history, text):
        seen_history.append(list(history))
        yield "ok"

    monkeypatch.setattr("app.agent.graph.stream_reply", _capturing_stream_reply)

    _submit(client, user_a["user_id"], "first", conversation_id="conv-2")
    _submit(client, user_a["user_id"], "second", conversation_id="conv-2")

    assert len(seen_history) == 2
    assert seen_history[0] == []
    # First turn's USER + AI messages are now prior history for the second call.
    assert len(seen_history[1]) == 2


def test_new_conversation_id_starts_with_empty_history(client: TestClient, user_a: dict):
    """Spec section 8: an unrecognised conversation_id is a new conversation, not an error."""
    response = _submit(client, user_a["user_id"], "hello", conversation_id="brand-new-id")
    assert response.status_code == 200


def test_missing_text_is_422(client: TestClient, user_a: dict):
    response = client.post(
        "/chatbot/queries",
        json={"conversation_id": CONVERSATION_ID, "user_id": user_a["user_id"]},
    )
    assert response.status_code == 422


def test_blank_text_is_422(client: TestClient, user_a: dict):
    response = _submit(client, user_a["user_id"], "   ")
    assert response.status_code == 422


def test_unknown_user_id_is_404(client: TestClient):
    response = _submit(client, 999_999, "hello")
    assert response.status_code == 404
    assert response.json()["status"] == "failure"


def test_agent_failure_mid_stream_emits_error_event_with_200(
    client: TestClient, db: Session, monkeypatch: pytest.MonkeyPatch, user_a: dict
):
    monkeypatch.setattr("app.agent.graph.stream_reply", _failing_stream_reply)

    response = _submit(client, user_a["user_id"], "trigger failure", conversation_id="conv-fail")

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events == [
        {"chunk": "Partial"},
        {"error": "Processing failed, please retry."},
        "[DONE]",
    ]

    rows = db.scalars(
        select(ChatMessage).where(ChatMessage.conversation_id == "conv-fail")
    ).all()
    # Only the USER turn persists - no AI row when the stream fails.
    assert [row.role for row in rows] == [MessageRole.USER]


# --------------------------------------------------------------------------- #
# GET /chatbot/conversations/{conversation_id}/history
# --------------------------------------------------------------------------- #


def test_history_is_empty_for_unknown_conversation(client: TestClient):
    response = client.get("/chatbot/conversations/does-not-exist/history")
    assert response.status_code == 200
    assert response.json() == {"conversation_id": "does-not-exist", "messages": []}


def test_history_returns_newest_first(client: TestClient, user_a: dict):
    _submit(client, user_a["user_id"], "first", conversation_id="conv-3")
    _submit(client, user_a["user_id"], "second", conversation_id="conv-3")

    response = client.get("/chatbot/conversations/conv-3/history")
    assert response.status_code == 200

    body = response.json()
    assert body["conversation_id"] == "conv-3"
    roles_and_messages = [(m["role"], m["message"]) for m in body["messages"]]
    assert roles_and_messages == [
        ("AI", "echo:second done"),
        ("USER", "second"),
        ("AI", "echo:first done"),
        ("USER", "first"),
    ]


# --------------------------------------------------------------------------- #
# GET /chatbot/fetch_all_conversation_ids
# --------------------------------------------------------------------------- #


def _fetch_conversation_ids(client: TestClient, user_id: int):
    return client.get("/chatbot/fetch_all_conversation_ids", params={"user_id": user_id})


def test_fetch_all_conversation_ids_empty_for_user_with_no_conversations(
    client: TestClient, user_a: dict
):
    response = _fetch_conversation_ids(client, user_a["user_id"])
    assert response.status_code == 200
    assert response.json() == {"user_id": user_a["user_id"], "conversations": []}


def test_fetch_all_conversation_ids_unknown_user_is_404(client: TestClient):
    response = _fetch_conversation_ids(client, 999_999)
    assert response.status_code == 404
    assert response.json()["status"] == "failure"


def test_fetch_all_conversation_ids_missing_user_id_is_422(client: TestClient):
    response = client.get("/chatbot/fetch_all_conversation_ids")
    assert response.status_code == 422


def test_fetch_all_conversation_ids_orders_by_first_message_not_last_activity(
    client: TestClient, db: Session, user_a: dict
):
    """Spec section 5.4: `created_at` is each conversation's first-message
    timestamp, and the list sorts newest-created-first - so a conversation
    that started earlier but was replied to most recently must still sort
    *behind* one that simply started later."""
    now = datetime.now(UTC)
    _seed_message(
        db, conversation_id="older-conv", user_id=user_a["user_id"], role=MessageRole.USER,
        message="hi", at=now - timedelta(hours=2),
    )
    _seed_message(
        db, conversation_id="newer-conv", user_id=user_a["user_id"], role=MessageRole.USER,
        message="hi", at=now - timedelta(hours=1),
    )
    # Most recent activity overall, but in the conversation that started first.
    _seed_message(
        db, conversation_id="older-conv", user_id=user_a["user_id"], role=MessageRole.AI,
        message="reply", at=now,
    )

    response = _fetch_conversation_ids(client, user_a["user_id"])
    assert response.status_code == 200

    body = response.json()
    assert body["user_id"] == user_a["user_id"]
    assert [c["conversation_id"] for c in body["conversations"]] == ["newer-conv", "older-conv"]


def test_fetch_all_conversation_ids_scoped_to_requesting_user(
    client: TestClient, db: Session, user_a: dict, user_b: dict
):
    _seed_message(
        db, conversation_id="a-conv", user_id=user_a["user_id"], role=MessageRole.USER,
        message="hi", at=datetime.now(UTC),
    )
    _seed_message(
        db, conversation_id="b-conv", user_id=user_b["user_id"], role=MessageRole.USER,
        message="hi", at=datetime.now(UTC),
    )

    response = _fetch_conversation_ids(client, user_a["user_id"])
    assert response.status_code == 200
    conversation_ids = [c["conversation_id"] for c in response.json()["conversations"]]
    assert conversation_ids == ["a-conv"]
