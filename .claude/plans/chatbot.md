# Chatbot Endpoint — Implementation Plan

## Context

Spec at [`.claude/specs/chatbot.md`](../specs/chatbot.md). This adds a standalone
chatbot module (LangGraph-backed, Postgres-persisted, SSE-streamed) alongside
the existing expense/user domains, following the same layering already
established in this codebase: SQLAlchemy ORM model → Pydantic schema →
service layer → router, errors as `AppError` subclasses, responses in the
`status/message/data` envelope (`app/schemas/common.py`,
`app/core/exceptions.py`).

Outcome: `POST /chatbot/queries` (SSE),
`GET /chatbot/conversations/{conversation_id}/history` (JSON), and
`GET /chatbot/fetch_all_conversation_ids` (JSON, spec §5.4 — added after the
first two shipped, for a sidebar-style conversation list), all fully tested
with the LLM call stubbed so CI never needs a live API key.

---

## Resolved ambiguities

The spec (§8 especially) leaves several things open or in tension with how
the rest of this codebase already works. Implement as stated here; the two
marked **confirm** are worth a real yes/no since they change code shape, not
just a validation detail.

| Issue | Spec tension | Decision |
| --- | --- | --- |
| Auth model **(confirm)** | §4.1 has `user_id` "obtained directly from the request body"; no auth mentioned | Treat `/chatbot/*` as service-to-service — **no** `Authorization` bearer guard, unlike every other router which resolves the caller from a JWT via `CurrentUser`. `user_id` is validated against `users` and 404s if absent. This is a real deviation from the rest of the API's trust model — flag before shipping. |
| Status code for missing/blank fields | §8 wants `400` for missing/empty `text`/`conversation_id`/`user_id` | Keep pydantic-required fields → `422`, matching every other endpoint's schema-validation convention. Only a present-but-blank `text` is a hand-checked `400 BadRequestError` (a business rule, not a shape failure). |
| Unknown `conversation_id` | Spec: treat as new conversation, don't reject | Implemented literally — no rows found ⇒ empty prior history, first turn. |
| History ordering | Sample response shows the later-timestamped `AI` message before the earlier `USER` one (newest-first) | `GET .../history` returns newest-first (`ORDER BY date DESC`), matching both the sample and the existing `sort_order=DESC` default on expense listing. |
| LLM provider | Not specified anywhere in the spec | **OpenAI** via `langchain-openai` (`ChatOpenAI`); model name in `Settings.chatbot_model` (default `gpt-5`), key in `OPENAI_API_KEY`. Swap is contained to `app/agent/llm.py`. Originally built against Anthropic Claude; switched per explicit request. |
| Memory mechanism | Spec §6 wants "memory" + "conversation history tied to conversation_id" as two bullets, but they're the same requirement | No separate LangGraph checkpointer. Rehydrate the graph's message history **from the `chat_history` table itself** on every call — one store, no drift between "what the agent remembers" and what the history endpoint reports. |
| Failed chat-history DB write | Spec: log, don't fail the client | The AI-message write happens *after* `[DONE]` has already been sent, so this falls out naturally — see router notes below. |
| "Creation timestamp" of a conversation (§5.4) | There is no separate `conversations` table — a conversation is just a shared `conversation_id` across `chat_history` rows | Defined as `MIN(date)` across that `conversation_id`'s rows — i.e. the timestamp of its first (USER) message. Computed with SQL `func.min`, not by loading and sorting rows in Python. |
| Unknown `user_id` on `/chatbot/fetch_all_conversation_ids` | §5.4 says "`user_id` with no conversations" is `200` + `[]`, but doesn't separately address a `user_id` that doesn't exist at all | Kept consistent with decision #1 above: `chat_service.resolve_user` still runs first and 404s for a `user_id` absent from `users`. The `200` + `[]` case in §5.4 is for an *existing* user who simply hasn't started a conversation yet — not a stand-in for "don't validate the user." |

---

## Stack additions

```
langgraph>=0.2.0
langchain>=1.0.0
langchain-core>=0.3.0
langchain-openai>=0.2.0
```

Pinned lower bounds pulled in `langgraph==1.2.11` / `langchain==1.4.0` /
`langchain-openai==1.6.2` in practice — a major-version jump from the
`0.2`/`0.3` API this plan was originally sketched against.
`langgraph.prebuilt.create_react_agent` is deprecated as of LangGraph 1.0 in
favor of `langchain.agents.create_agent` (removal slated for v2.0), so
`app/agent/graph.py` uses the latter, which is why `langchain` itself
(not just `langchain-core`) is a direct dependency. Its compiled graph's
model node is named `"model"`, not `"agent"` — that's the value
`stream_reply`'s `metadata.get("langgraph_node")` filter checks.

`psycopg[binary]` and `SQLAlchemy` are already in `requirements.txt` — no new
DB driver, since persistence goes through the existing `Session`, not a
LangGraph-native Postgres checkpointer.

`.env.example` / `.env` additions:
```
OPENAI_API_KEY=
CHATBOT_MODEL=gpt-5
```

`app/core/config.py` (`Settings`) additions:
```python
openai_api_key: str = ""
chatbot_model: str = "gpt-5"
```

---

## File layout

Additions marked `+`, following the existing `app/` layout.

```
app/
├── agent/                    +
│   ├── __init__.py
│   ├── llm.py                 # ChatOpenAI factory, reads Settings
│   ├── tools.py                # read-only tools wrapping expense_service/user_service
│   └── graph.py                 # StateGraph + compiled singleton + stream_reply()
├── models/
│   └── chat_message.py       + # ChatMessage, MessageRole enum
├── schemas/
│   └── chatbot.py            + # ChatQueryRequest, ChatMessageOut, ChatHistoryResponse,
│                                # ConversationSummaryOut, ConversationListResponse
├── services/
│   └── chat_service.py       + # save_message, get_conversation_messages, resolve_user,
│                                # list_conversations_for_user
├── routers/
│   └── chatbot.py            + # POST /chatbot/queries
│                                # GET /chatbot/conversations/{id}/history
│                                # GET /chatbot/fetch_all_conversation_ids
tests/
└── test_chatbot.py           +
```

---

## Data model — `app/models/chat_message.py`

Same enum pattern as `ExpenseCategory`/`UserType`:

```python
class MessageRole(StrEnum):
    USER = "USER"
    AI = "AI"

class ChatMessage(Base):
    __tablename__ = "chat_history"
    __table_args__ = (Index("ix_chat_history_conversation_id_date", "conversation_id", "date"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    role: Mapped[MessageRole] = mapped_column(
        SAEnum(MessageRole, native_enum=False, length=8,
               values_callable=lambda enum_cls: [m.value for m in enum_cls]),
        nullable=False,
    )
    message: Mapped[str] = mapped_column(Text, nullable=False)
    date: Mapped[datetime] = mapped_column(UTCDateTime, default=utc_now, nullable=False)
```

`user_id` is an FK to `users.id` (int) — the spec's "String / Integer" is the
*wire* type; storage matches the existing `User.id` type. `conversation_id`
is client-generated and opaque (no FK, no uniqueness — many rows per id).

Register `ChatMessage`, `MessageRole` in `app/models/__init__.py` so
`init_db()` picks up the table.

---

## Schemas — `app/schemas/chatbot.py`

```python
class ChatQueryRequest(BaseModel):
    text: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    conversation_id: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    user_id: int

class ChatMessageOut(BaseModel):
    role: MessageRole
    message: str
    date: IsoUtcDateTime

class ChatHistoryResponse(BaseModel):
    conversation_id: str
    messages: list[ChatMessageOut]

class ConversationSummaryOut(BaseModel):
    conversation_id: str
    created_at: IsoUtcDateTime

class ConversationListResponse(BaseModel):
    user_id: int
    conversations: list[ConversationSummaryOut]
```

`user_id` arrives as a required query parameter on the router (spec §5.4),
not a body field, so there's no separate request schema for it — FastAPI's
`Annotated[int, Query(ge=1)]` on the path function is enough, matching how
`expense_id`/`user_id` query params are already declared in
`app/routers/expense.py`.

---

## Service — `app/services/chat_service.py`

```python
def resolve_user(db: Session, user_id: int) -> User:
    """NotFoundError -> 404 if user_id doesn't exist."""

def get_conversation_messages(db: Session, conversation_id: str, *, newest_first: bool) -> list[ChatMessage]: ...

def save_message(db: Session, *, conversation_id: str, user_id: int, role: MessageRole, message: str) -> ChatMessage: ...

def list_conversations_for_user(db: Session, user_id: int) -> list[tuple[str, datetime]]:
    """Every distinct conversation_id for user_id, paired with its earliest
    message's timestamp, newest-created-first. One SQL query:
    SELECT conversation_id, MIN(date) FROM chat_history
    WHERE user_id = :user_id GROUP BY conversation_id ORDER BY MIN(date) DESC
    """
```

---

## Agent — `app/agent/`

- `tools.py`: read-only LangChain tools, built fresh per request and closed
  over the resolved `db`/`user` so one conversation can never see another
  user's data — e.g. `get_expenses_in_range(...)` wrapping
  `expense_service.list_expenses`, `get_user_profile()` wrapping the existing
  user lookup.
- `llm.py`: `ChatOpenAI(model=settings.chatbot_model, api_key=..., streaming=True)`.
- `graph.py`: a small tool-calling `StateGraph` (or
  `langgraph.prebuilt.create_react_agent`) exposing the single entrypoint the
  router uses:
  ```python
  async def stream_reply(db: Session, user: User, history: list[BaseMessage], text: str) -> AsyncIterator[str]:
      """Yields text chunks as they're generated. No checkpointer configured —
      history comes in as an argument, rehydrated from chat_history by the caller."""
  ```

Build this last — stub it first (echo/canned response) so the router and
tests can be built and passing before the real tool-calling logic is tuned.

---

## Router — `app/routers/chatbot.py`

```python
router = APIRouter(prefix="/chatbot", tags=["chatbot"])

@router.post("/queries", summary="Submit a chatbot query and stream the reply")
def submit_query(payload: ChatQueryRequest, db: DbSession) -> StreamingResponse:
    user = chat_service.resolve_user(db, payload.user_id)
    history = chat_service.get_conversation_messages(db, payload.conversation_id, newest_first=False)
    chat_service.save_message(db, conversation_id=payload.conversation_id,
                               user_id=user.id, role=MessageRole.USER, message=payload.text)

    async def event_stream() -> AsyncIterator[str]:
        chunks: list[str] = []
        try:
            async for chunk in agent_graph.stream_reply(db, user, to_lc_messages(history), payload.text):
                chunks.append(chunk)
                yield f"data: {json.dumps({'chunk': chunk})}\n\n"
        except Exception:
            logger.exception("Chatbot agent failed mid-stream for conversation_id=%s", payload.conversation_id)
            yield f"data: {json.dumps({'error': 'Processing failed, please retry.'})}\n\n"
            yield "data: [DONE]\n\n"
            return

        yield "data: [DONE]\n\n"
        _persist_ai_reply(payload.conversation_id, user.id, "".join(chunks))

    return StreamingResponse(event_stream(), media_type="text/event-stream")
```

Two non-obvious constraints this relies on:

1. **`StreamingResponse` sends the `200` status/headers before the body
   generator yields anything.** A real `500` is therefore only possible for
   failures *before* `StreamingResponse(...)` is returned — which is exactly
   what happens here: `resolve_user` and history lookup run in the plain
   function body and go through the normal `AppError` → JSON path. Only
   failures *inside* `event_stream()` (the LangGraph call) get the in-band
   `data: {"error": ...}` + `[DONE]` treatment, matching spec §8 exactly.
2. **Whether `db` is safe to reuse inside `event_stream()` depends on the
   installed FastAPI's dependency-cleanup timing — verified, not assumed.**
   Checked against `fastapi/routing.py` in the installed version
   (`fastapi==0.141.1`): a `Depends(..., yield)` dependency's exit is
   registered on the *outer*, per-request `AsyncExitStack`
   (`fastapi_inner_astack`), which only closes **after**
   `await response(scope, receive, send)` finishes — i.e. after the
   `StreamingResponse` body has fully sent. So `db` is still open for the
   whole generator, and `_persist_ai_reply` reuses it directly rather than
   opening a second session:
   ```python
   def _persist_ai_reply(db: Session, conversation_id: str, user_id: int, message: str) -> None:
       try:
           chat_service.save_message(db, conversation_id=conversation_id,
                                      user_id=user_id, role=MessageRole.AI, message=message)
       except Exception:
           logger.exception("Failed to persist AI reply for conversation_id=%s", conversation_id)
   ```
   **Re-check this against `fastapi.routing` if the FastAPI version ever
   changes** — on an older/different version where the dependency closes
   before the body streams, this would need to go back to opening a fresh
   session. Note `app.db.session.SessionLocal` directly is *not* a safe
   fallback for that either, since it bypasses the test suite's
   `dependency_overrides` on `get_db` and points at the wrong engine under
   test (confirmed: `tests/test_chatbot.py` failed with `no such table:
   chat_history` the first time this was tried).
   This also delivers spec §8's "log only, don't fail the response" for a
   failed write — the client already has `[DONE]` by the time this runs.

```python
@router.get("/conversations/{conversation_id}/history", response_model=ChatHistoryResponse)
def get_history(conversation_id: str, db: DbSession) -> ChatHistoryResponse:
    messages = chat_service.get_conversation_messages(db, conversation_id, newest_first=True)
    return ChatHistoryResponse(
        conversation_id=conversation_id,
        messages=[ChatMessageOut(role=m.role, message=m.message, date=m.date) for m in messages],
    )
```

An unknown `conversation_id` is `200` with `messages: []`, matching the
empty-range-is-200 convention already used for expense listing, not `404`.

```python
@router.get("/fetch_all_conversation_ids", response_model=ConversationListResponse)
def fetch_all_conversation_ids(
    db: DbSession, user_id: Annotated[int, Query(ge=1)]
) -> ConversationListResponse:
    user = chat_service.resolve_user(db, user_id)  # 404 for an unknown user_id
    conversations = chat_service.list_conversations_for_user(db, user.id)
    return ConversationListResponse(
        user_id=user.id,
        conversations=[
            ConversationSummaryOut(conversation_id=cid, created_at=created_at)
            for cid, created_at in conversations
        ],
    )
```

`user_id` is a required query parameter (`Annotated[int, Query(ge=1)]`,
matching `expense_id`/`user_id` elsewhere in the codebase), not a path or
body field — a missing value is FastAPI's own `422`, no custom schema
needed. An existing user with zero conversations is `200` + `conversations:
[]`, never `404` (spec §5.4); a `user_id` that doesn't exist in `users` at
all is still `404` via `resolve_user`, for consistency with the other two
endpoints.

Wire into `app/main.py`: import and `include_router`, extend
`API_DESCRIPTION` with a "Chatbot domain" bullet.

---

## Error handling summary

| Scenario | Mechanism |
| --- | --- |
| Missing `text`/`conversation_id`/`user_id` | pydantic required-field → `422` |
| Blank `text` | `422` via `StringConstraints(min_length=1)` after strip |
| `user_id` not found | `chat_service.resolve_user` → `NotFoundError` → `404` |
| Unknown `conversation_id` | Not an error — empty history, new conversation |
| Agent failure before first chunk | Propagates from `submit_query` body → global handler → `500` |
| Agent failure mid-stream | Caught in `event_stream()` → `data: {"error": ...}` + `[DONE]`, HTTP stays `200` |
| AI-message DB write failure | Caught + logged in `_persist_ai_reply`, client unaffected |
| History read DB failure | Global `Exception` handler → `500` |
| Missing `user_id` on `fetch_all_conversation_ids` | FastAPI required-query-param → `422` |
| `user_id` not found on `fetch_all_conversation_ids` | `chat_service.resolve_user` → `NotFoundError` → `404` |
| Existing `user_id` with zero conversations | `200` + `conversations: []`, not an error |

---

## Tests — `tests/test_chatbot.py`

Reuse `client`/`db`/`user_a` fixtures from `conftest.py`. Monkeypatch
`app.agent.graph.stream_reply` to a fixed async generator (e.g.
`["Your ", "total ", "is ", "$450."]`) — no live LLM call in CI.

- Happy path: fresh `conversation_id` → SSE body ends `data: [DONE]`; `USER`
  row exists pre-stream, `AI` row exists once `TestClient` finishes reading
  the response (synchronous ASGI test client — no async race).
- Second call, same `conversation_id` → mocked agent receives prior turn's
  messages as history.
- Missing/blank `text` → `422`.
- Unknown `user_id` → `404`.
- Mocked agent raises mid-generator → SSE `error` event + `[DONE]`, status
  still `200`.
- `GET .../history` on unseeded id → `200`, `messages: []`.
- `GET .../history` after a couple of turns → newest-first ordering, correct roles.
- `GET /chatbot/fetch_all_conversation_ids` for a user with zero conversations → `200`, `conversations: []`.
- Same, for an unknown `user_id` → `404`.
- Same, after conversations in two different `conversation_id`s (at different
  times) → both listed, newest-created-first, each `created_at` matching that
  conversation's first message's timestamp - not its most recent one.
- A second user's conversations never appear in the first user's list.

---

## Build order

1. `app/models/chat_message.py` + register in `app/models/__init__.py`.
2. `app/schemas/chatbot.py`.
3. `app/services/chat_service.py`.
4. `app/agent/llm.py` + `tools.py` + `graph.py`, stubbed initially.
5. `app/routers/chatbot.py`, wired into `app/main.py`.
6. `tests/test_chatbot.py` against the stub.
7. Swap the stub for real tool-calling logic — done, using OpenAI (`ChatOpenAI` in `app/agent/llm.py`); a real `OPENAI_API_KEY` still needs to be filled into `.env` before `/chatbot/queries` can reach a live model.
8. `GET /chatbot/fetch_all_conversation_ids` (spec §5.4, added after the above shipped): `chat_service.list_conversations_for_user`, `ConversationSummaryOut`/`ConversationListResponse` in `app/schemas/chatbot.py`, the router function, and its tests.
