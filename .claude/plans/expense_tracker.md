# Expense Tracker Backend — Implementation Plan

## Context

The repository is empty apart from the SRS at `.vscode/.claude/specs/expense_tracker.md` and a VS Code settings file. This plan builds the entire FastAPI backend from scratch to satisfy that spec.

The spec's §8 lists five open items. Three were confirmed **in scope** for this build:

- OTP verification on password reset
- Update and delete operations on expenses
- Refresh-token flow

Confirmed technical choices: **SQLite + SQLAlchemy 2.0**, **integer autoincrement IDs**, **full pytest suite**.

Outcome: a runnable, tested REST API with seven spec'd endpoints plus five added by the in-scope extras, self-documenting at `/docs`.

---

## Resolved ambiguities

These are decisions the spec left open or self-contradictory. Implement as stated here.

| Issue | Spec tension | Decision |
| --- | --- | --- |
| Response envelope | §6 mandates `{status, message, data}`; §5.2/§5.7 show bare objects | §5's literal bodies win per endpoint — they are the concrete contract. §6's **failure** envelope applies to *all* errors globally. |
| `user_id` / `expense_id` transport | "path or query parameter" | **Query parameter** — matches the flat `/user/fetch_user_by_id` naming. |
| Currency amounts | `decimal`, max 2 dp | Stored as **INTEGER minor units** (paise). SQLite has no exact decimal type; `Numeric` round-trips through float and breaks `sort_by=amount`. Converted to `Decimal` at the schema boundary. |
| Password max length | Only a minimum given | Cap at **72 characters** — bcrypt silently truncates beyond 72 bytes. Enforced in the schema so it is a clean 422, not a security hole. |
| `date_time` "not a future date" | No skew allowance | Reject anything more than **60s** past server `now(UTC)`, to tolerate client clock drift. |
| §5.4 `forgot_password` body | No `otp` field | Extended with a required `otp` field, since OTP is in scope. New companion endpoint issues the OTP. |
| Migrations | Not mentioned | `Base.metadata.create_all()` on startup. Alembic is deferred — noted in README as the first thing to add before a second datastore. |
| `register_user` re-registration | Not in the original spec; added later | Treated as an **upsert keyed by `email`** rather than a straight 409. `email` is the only field besides the immutable `password`/`confirm_password` that never changes once a user exists, so it is the natural lookup key; `mobile` is excluded from the key because it is one of the fields the update path is allowed to change. A `mobile` clash with a *different* existing user still 409s. |

---

## Stack

```
fastapi, uvicorn[standard]
pydantic[email] (v2), pydantic-settings
sqlalchemy>=2.0        # Mapped / mapped_column typed ORM
pyjwt                  # not python-jose (unmaintained)
bcrypt                 # used directly; passlib is stale and mis-handles bcrypt 4.x
pytest, httpx          # httpx drives Starlette TestClient
ruff                   # lint + format
```

---

## File layout

Follows §9, with additions marked `+`.

```
app/
├── main.py                     # app factory, router mounts, exception handlers, startup
├── core/
│   ├── config.py               # pydantic-settings Settings, cached accessor
│   ├── security.py             # hashing, JWT encode/decode, OTP generation
│   ├── logging.py            + # correlation-id logging setup + redaction filter
│   └── exceptions.py         + # AppError hierarchy + handler registration
├── db/                       +
│   ├── base.py                 # DeclarativeBase, Base.metadata
│   ├── session.py              # engine, SessionLocal, get_db dependency
│   └── types.py                # UTCDateTime TypeDecorator
├── models/
│   ├── user.py                 # User, UserType enum, UserStatus enum
│   ├── expense.py              # Expense, ExpenseCategory enum
│   └── auth.py               + # RefreshToken, PasswordResetOTP
├── schemas/
│   ├── common.py             + # SuccessResponse[T], FailureResponse, PaginatedResponse[T]
│   ├── user.py                 # register/login/refresh/forgot-password/user-out
│   └── expense.py              # create/update/out/query-params
├── routers/
│   ├── user.py
│   └── expense.py
├── services/
│   ├── user_service.py
│   ├── auth_service.py       + # tokens, refresh rotation, OTP lifecycle
│   └── expense_service.py
├── notifications/            + # mock OTP sender (logs; swap for a real SMS gateway later)
│   └── sms.py
└── dependencies.py             # get_current_user auth guard, pagination params

tests/
├── conftest.py
├── test_auth.py
├── test_user.py
├── test_expense_create.py
├── test_expense_fetch.py
└── test_expense_mutate.py

migrations/                     + # standalone, idempotent schema migrations (no Alembic yet)
├── __init__.py
└── m0001_add_user_type_and_status.py

.env.example, .gitignore, requirements.txt, pyproject.toml (ruff/pytest config), README.md
```

---

## Data model

**users** — `id` PK autoincrement, `name`, `mobile` (unique, indexed), `email` (unique, indexed), `password_hash`, `user_type` (enum `admin`/`member`, default `member`), `status` (enum `active`/`inactive`, default `active`), `created_at`, `updated_at`
- `user_type` and `status` are `SAEnum(native_enum=False)` columns — a `VARCHAR` + `CHECK` constraint, not a native DB enum type, matching `expense_category`'s pattern. Added via `migrations/m0001_add_user_type_and_status.py` (no Alembic yet), which backfills existing rows to the defaults in the same `ALTER TABLE ADD COLUMN ... DEFAULT ... CHECK (...)` statement.

**expenses** — `id` PK, `user_id` FK→users.id (`ondelete=CASCADE`), `expense_category`, `expense_name`, `amount_minor` INTEGER, `date_time` (UTC), `created_at`, `updated_at`
- Composite index `ix_expenses_user_date_time` on `(user_id, date_time)` — §7 performance requirement.

**refresh_tokens** — `id`, `user_id` FK, `token_hash` (unique, indexed), `expires_at`, `revoked_at` nullable, `created_at`

**password_reset_otps** — `id`, `user_id` FK, `otp_hash`, `expires_at`, `consumed_at` nullable, `attempts` INTEGER default 0, `created_at`

Only hashes of secrets are persisted — never a raw refresh token or OTP.

`db/types.py::UTCDateTime` normalizes aware datetimes to UTC on bind and re-attaches `timezone.utc` on load, so SQLite's naive storage never leaks a naive datetime into business logic.

---

## Validation rules (`schemas/`)

Per §3, enforced declaratively so no unvalidated data reaches a service.

- `name` — `min_length=2, max_length=50`, pattern `^[A-Za-z ][A-Za-z ]*$`, whitespace-stripped, collapse internal runs.
- `mobile` — pattern `^(\d{10}|\+[1-9]\d{7,14})$` (10 digits *or* E.164).
- `email` — `EmailStr`, lowercased for storage and uniqueness.
- `password` — field validator returning **all** failures at once: length 8–72, ≥1 upper, ≥1 lower, ≥1 digit, ≥1 special. Shared as a reusable `PasswordStr` annotated type across register / forgot-password.
- `confirm_password` — `@model_validator(mode="after")`; mismatch raises so the router maps it to **400**, not 422 (§5.1 is explicit that this is a 400).
- `expense_category` — `ExpenseCategory` str enum: `Food, Travel, Shopping, Groceries, Bills, Rent, Health, Education, Entertainment, Other`.
- `expense_name` — 1–100 after strip; reject whitespace-only.
- `amount` — `Decimal`, `gt=0`, `max_digits=12`, `decimal_places=2`, plus an explicit exponent check so `10.999` is rejected rather than rounded.
- `date_time` — must be tz-aware or assumed UTC; rejected if `> now(UTC) + 60s`.
- All password fields live only on request models. No response model has a password field — structurally impossible, not filtered.

---

## Auth design

`core/security.py`:
- `hash_password` / `verify_password` — bcrypt, cost 12.
- `create_access_token(user_id)` — claims `sub`, `iat`, `exp`, `jti`, `typ="access"`. TTL 15 min.
- `create_refresh_token(user_id)` — same shape, `typ="refresh"`, TTL 7 days; the raw token is returned once and only its SHA-256 hash is stored.
- `decode_token(token, expected_typ)` — raises typed errors for expired vs malformed vs wrong-type; every case surfaces as **401**.

`auth_service.authenticate` (+, added) — after the password hash check succeeds, rejects `status = inactive` accounts with `ForbiddenError` (403, message "Account is inactive"). The status check runs strictly after credential verification, not before: checking it first would let an unauthenticated caller learn whether an email is registered and active without knowing the password, which is the same enumeration-oracle problem §5.3's generic "Invalid email or password" message already guards against. Because the 403 is only reachable once the password has already been confirmed correct, naming the specific reason discloses nothing new.

`dependencies.py::get_current_user`:
- Uses `HTTPBearer(auto_error=False)`. **This matters** — FastAPI's default `HTTPBearer` returns `403` when the header is absent, but §4.4 requires `401`. With `auto_error=False` we raise our own 401.
- Decodes, loads the `User` row, 401 if the row is gone.

Ownership (§4.4): `fetch_user_by_id` compares the query `user_id` against `current_user.id` → **403** on mismatch. Expense reads/mutations compare `expense.user_id` → **403** if it exists but belongs to someone else, **404** if it does not exist. (Deliberate: the spec's §5.7 table distinguishes the two.)

**Refresh flow** — access token is short-lived; `POST /user/refresh_token` validates the presented refresh token against its stored hash, checks `revoked_at is null` and `expires_at`, then **rotates**: the old row is revoked and a new pair issued. Reuse of an already-revoked token revokes the whole user's token family and returns 401.

**OTP flow** — `request_otp` generates a 6-digit code via `secrets.randbelow`, stores its hash with a 10-minute expiry, invalidates prior unconsumed codes, and hands it to `notifications/sms.py` (logs it at INFO; the log line is the delivery channel until a real gateway exists). The response is generic regardless of whether the mobile exists, so it is not a user-enumeration oracle. `forgot_password` then verifies the code, increments `attempts`, locks out after 5, and marks `consumed_at` on success — a code is single-use.

`EXPOSE_OTP_IN_RESPONSE` (default `false`, refuses to turn on when `APP_ENV=production`) returns the OTP in the response body for local testing.

---

## Endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| POST | `/user/register_user` | Public | + Upsert keyed by `email`: 201 for a new user, 200 for an update to an existing one; 409 on `mobile` already belonging to another user; `password`/`confirm_password`/`email` silently ignored on the update path; optional `user_type`/`status`, default `member`/`active`, not echoed in the response |
| POST | `/user/login_user` | Public | 200; user details + `access_token` + `refresh_token` + `token_type: bearer`. Generic failure message per §5.3; + `status = inactive` rejected with 403 once credentials are confirmed |
| POST | `/user/refresh_token` | Public | + Rotates the pair |
| POST | `/user/logout` | Required | + Revokes the presented refresh token |
| POST | `/user/forgot_password/request_otp` | Public | + Issues OTP; always 200 |
| POST | `/user/forgot_password` | Public | Body extended with `otp`; 400 mismatch, 404 unknown mobile |
| GET | `/user/fetch_user_by_id?user_id=` | Required | 403 for another user's record |
| POST | `/user/fetch_all_users` | Required | + Every user, sorted and paginated; criteria in a JSON body; optional `search` filters name/mobile/email/user_type/status (partial, case-insensitive, OR) |
| POST | `/user/deactivate_user` | Required (admin) | + Sets `status` to the value given in the body - `active` activates, `inactive` deactivates; any other value is 422; 403 if requester is not an admin, 404 unknown id |
| POST | `/expense/create_expense` | Required | `user_id` from JWT, never the body |
| POST | `/expense/fetch_expense` | Required | Range + sort + paginate; criteria in a JSON body |
| GET | `/expense/fetch_expense_by_id?expense_id=` | Required | 403 vs 404 distinguished |
| PUT | `/expense/update_expense?expense_id=` | Required | + Partial body; same ownership rules |
| DELETE | `/expense/delete_expense?expense_id=` | Required | + Hard delete, 200 with success envelope |

**`fetch_expense` specifics** — the criteria arrive as a JSON body bound to `ExpenseFetchRequest`, which is why the route is a `POST` despite being a read: a body on a `GET` is not sent by Swagger UI or by most HTTP clients, and it cannot be expressed cleanly in OpenAPI.

```json
{
  "from_date": "2026-08-06T00:00:00Z",
  "to_date": "2026-08-07T05:05:00Z",
  "sort_by": "amount",
  "sort_order": "ASC",
  "page_size": 10,
  "page": 1
}
```

| Field | Type | Required | Allowed values | Default |
| --- | --- | --- | --- | --- |
| `from_date` | ISO 8601 UTC datetime | Yes | — | — |
| `to_date` | ISO 8601 UTC datetime | Yes | must be ≥ `from_date` | — |
| `sort_by` | string (enum) | No | `expense_category`, `expense_name`, `amount`, `date_time` | `date_time` |
| `sort_order` | string (enum) | No | `ASC`, `DESC` | `DESC` |
| `page_size` | positive integer | No | 1–100 (`MAX_PAGE_SIZE`) | `10` |
| `page` | positive integer | No | ≥ 1 | `1` |

Both bounds are inclusive **instants** — no date-only special case. `to_date` covers exactly the instant given, so a whole day needs `T23:59:59Z`; the earlier "a bare `end_date` covers the whole day" rule is gone, and a bare date now simply means midnight. Naive values are read as UTC, offset values converted to it. Internally `ExpenseFetchRequest.date_range` turns the pair into the half-open `DateRange` the query uses (`end_exclusive = to_date + 1µs`), so `parse_range_bound`/`build_date_range` and the `DateRangeParams` dependency are gone.

`to_date < from_date` → **400** (a business rule, so checked in `expense_service.list_expenses` *after* schema parsing, deliberately not a model validator that would surface it as a 422). `sort_by` and `sort_order` are `StrEnum`s so a bad value — including wrong casing, since neither is coerced — is a natural 422. `MAX_PAGE_SIZE` lives in `schemas/expense.py` rather than the service, so the request model can bound `page_size` without a circular import. Sorting always gets `expense_id` as a tiebreaker for stable pagination. `LIMIT`/`OFFSET` and `COUNT` execute in SQL — §7 forbids in-memory pagination. Empty range → 200 with `data: []`, never 404.

**`fetch_all_users` specifics** (§5.8, search added) — same shape as `fetch_expense` minus the date range: criteria arrive as a JSON body bound to `UserFetchAllRequest`, sorting and pagination execute in SQL via `user_service.list_users`, and `user_id` is the tiebreaker for stable paging. No ownership filter applies — any authenticated user may list every account, since the SRS only restricts *single*-record reads (§4.4) to the caller's own id. `SortOrder`, `DEFAULT_PAGE_SIZE`, and `MAX_PAGE_SIZE` were promoted from `schemas/expense.py` into `schemas/common.py` so both endpoints share one definition instead of two copies.

An optional `search: str | None` field filters the listing before sorting/pagination run: `user_service.list_users` builds a five-way `or_()` over `User.name`, `User.mobile`, `User.email`, `User.user_type`, and `User.status`, each compared with SQLAlchemy's `.ilike(f"%{search}%")` (case-insensitive substring, dialect-portable — compiles to `LOWER(x) LIKE LOWER(y)` on SQLite instead of relying on Postgres-only `ILIKE`). `search` is stripped first; `None`, missing, or blank-after-strip skips the `where()` entirely so the query is identical to no-search. Because the filter is a `WHERE` clause applied before the `COUNT(*)` subquery, `total_records`/`total_pages` reflect the filtered set, not the whole table.

```json
{
  "sort_by": "name",
  "sort_order": "ASC",
  "page_size": 10,
  "page": 1,
  "search": "priya"
}
```

| Field | Type | Required | Allowed values | Default |
| --- | --- | --- | --- | --- |
| `sort_by` | string (enum) | No | `name`, `email`, `mobile` | `name` |
| `sort_order` | string (enum) | No | `ASC`, `DESC` | `ASC` |
| `page_size` | positive integer | No | 1–100 (`MAX_PAGE_SIZE`) | `10` |
| `page` | positive integer | No | ≥ 1 | `1` |
| `search` | string or `null` | No | any string; matched against name/mobile/email/user_type/status | `null` |

Response is `PaginatedResponse[UserAdminOut]` — `user_id`, `name`, `email`, `mobile`, `user_type`, `status` per row, same structural guarantee against leaking `password_hash` as every other user response. `UserAdminOut` extends `UserOut` (used by `login_user` and `fetch_user_by_id`, which do not expose `user_type`/`status`) — this is deliberately the one admin-facing listing that does.

**`deactivate_user` specifics** (§5.9, admin-only) — body is `UserDeactivateRequest {user_id, status}`; `status` is a `UserStatus` member and *selects the outcome*: `active` activates, `inactive` deactivates, and the handler writes exactly that value to `user.status`. Any other value never reaches the handler — the enum rejects it with 422 at the schema boundary. Only an admin (`requester.user_type is UserType.ADMIN`) may call this: `user_service.set_user_status` checks the requester's role before existence, so a non-admin requester is 403 regardless of the target `user_id`; an admin targeting an unknown id gets 404. Response is the plain `MessageResponse` envelope, same as `logout`/`forgot_password`, with the message ("User activated successfully" / "User deactivated successfully") chosen by the router from `payload.status`. The operation is idempotent — setting a user to the status it already has still commits and returns 200.

---

## Cross-cutting

**Error handling** (`core/exceptions.py`) — an `AppError` base with `ConflictError`, `NotFoundError`, `ForbiddenError`, `UnauthorizedError`, `BusinessRuleError`, each carrying a status code and message. Handlers registered on the app for `AppError`, `HTTPException`, `RequestValidationError` (→ 422, pydantic details mapped into `errors[]`), and bare `Exception` (→ 500, logged with traceback, generic client message — §7 forbids leaking stack traces).

**Logging** (`core/logging.py`) — middleware assigns/propagates `X-Request-ID`, stored in a `ContextVar` and injected into every record. A redaction filter scrubs `password`, `confirm_password`, `otp`, `access_token`, `refresh_token`, and `authorization` from any log payload.

**Config** (`core/config.py`) — `BaseSettings` reading `.env`: `APP_ENV`, `DATABASE_URL`, `JWT_SECRET_KEY`, `JWT_ALGORITHM`, `ACCESS_TOKEN_EXPIRE_MINUTES=15`, `REFRESH_TOKEN_EXPIRE_DAYS=7`, `OTP_EXPIRE_MINUTES=10`, `OTP_MAX_ATTEMPTS=5`, `EXPOSE_OTP_IN_RESPONSE=false`, `BCRYPT_ROUNDS=12`. Startup **fails hard** if `JWT_SECRET_KEY` is unset or left at the example value while `APP_ENV != development`. `.env` is gitignored; `.env.example` is committed.

---

## Build order

1. **Scaffold** — `requirements.txt`, `pyproject.toml`, `.gitignore`, `.env.example`, `core/config.py`, `core/logging.py`, `db/base.py`, `db/session.py`, `db/types.py`, minimal `main.py` that boots.
2. **Models** — `models/user.py`, `models/expense.py`, `models/auth.py` + indexes.
3. **Security** — `core/security.py`, `core/exceptions.py`, `notifications/sms.py`.
4. **Schemas** — `schemas/common.py`, `schemas/user.py`, `schemas/expense.py` (all §3 validators).
5. **Guard** — `dependencies.py`.
6. **Services** — `user_service`, `auth_service`, `expense_service`.
7. **Routers** — `routers/user.py`, `routers/expense.py`; wire handlers and OpenAPI `responses={}` metadata into `main.py`.
8. **Tests** — `conftest.py` first, then the five test modules.
9. **README** — run instructions, env vars, the OTP-via-logs note, deferred items.

Each step should leave the app importable and `uvicorn app.main:app` bootable.

---

## Test plan (`tests/`)

`conftest.py` — in-memory SQLite with `StaticPool` + `check_same_thread=False` so one connection is shared across the test client; `create_all` per test via a function-scoped fixture; `app.dependency_overrides[get_db]`. Fixtures: `client`, `registered_user`, `auth_headers`, `other_user_headers`, `seeded_expenses` (~25 rows spanning dates, amounts, and categories, for pagination and sort assertions).

| Module | Covers |
| --- | --- |
| `test_auth.py` | Login success/failure; generic failure message; inactive-account login → 403 (including via `deactivate_user` then login); missing header → 401 (not 403); malformed, expired, and wrong-`typ` tokens → 401; refresh rotation; reused-refresh revocation; logout |
| `test_user.py` | Register happy path; each §3.1 rule rejected individually; mismatched confirm → 400; re-registering an existing email updates in place (200, not 409), case-insensitively; update ignores `password`/`confirm_password`/`email`; a different `email` cannot rename the account and is handled as a new registration; update `mobile` clashing with another user → 409 without partially applying; duplicate mobile on a genuinely new user → 409; fetch own → 200; fetch another's → 403; unknown id → 404; **no response body contains a password field**; OTP request/verify; wrong OTP; expired OTP; attempt lockout; `fetch_all_users` unauthenticated → 401, default sort/pagination, sort by each field × order, invalid `sort_by`/`sort_order` → 422, pagination arithmetic across pages, `search` matches partially and case-insensitively across name/mobile/email/user_type/status (OR), no match → empty page not 404, absent/null/blank `search` → unfiltered; `deactivate_user` as admin activates and deactivates any account (own or other's) → 200, both directions idempotent; as non-admin → 403 regardless of target; unknown id (as admin) → 404; invalid `status` → 422 |
| `test_expense_create.py` | Happy path → 201; `user_id` in body ignored in favour of the JWT; amount ≤ 0; 3-decimal amount; future `date_time`; empty name; 101-char name; invalid category |
| `test_expense_fetch.py` | Range inclusivity at both bounds; `to_date < from_date` → 400; equal bounds allowed; naive and offset bounds normalised to UTC; a bare date read as midnight; missing/malformed bound → 422 at `["body", …]`; each `sort_by` × `sort_order` ordering asserted; wrong-cased enums → 422; defaults applied when optional fields are omitted; pagination arithmetic (`total_records`, `total_pages`, page contents don't overlap); `page=0` → 422; `page_size=101` → 422; empty range → 200 + `[]`; another user's expenses never appear |
| `test_expense_mutate.py` | Update own → 200 and persisted; update another's → 403; update missing → 404; partial update leaves other fields intact; delete own → 200 then subsequent fetch → 404; delete another's → 403 and row survives |

---

## Verification

```bash
python -m venv .venv && .venv/Scripts/activate      # PowerShell: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                 # then set JWT_SECRET_KEY
pytest -q                                            # full suite must pass
ruff check . && ruff format --check .
uvicorn app.main:app --reload
```

Then, manually against `http://127.0.0.1:8000/docs`:

1. `register_user` → 201 with a `user_id`. Repeat the same body → 200 with the same `user_id` (update, not duplicate).
2. `login_user` → copy `access_token`; **confirm no password field appears anywhere in the response**.
3. Authorize in Swagger, then `create_expense` → 201.
4. `fetch_expense` with a body whose `from_date`/`to_date` cover that expense → verify `total_records`, `total_pages`, and ordering; flip `sort_order` between `ASC` and `DESC`; request `page: 2`; send `to_date` earlier than `from_date` → **400**.
5. `fetch_expense_by_id` → 200. Register a second user, log in as them, request the first user's `expense_id` → **403**.
6. Call any protected route with no `Authorization` header → **401** (not 403 — this is the `auto_error=False` check).
7. `forgot_password/request_otp`, read the OTP from the server log, `forgot_password` with it → 200; then log in with the new password; then replay the same OTP → 400.
8. Confirm `/redoc` renders and every endpoint documents its non-200 responses.

---

## Explicitly out of scope

Multi-currency (§8.5), Alembic migrations, rate limiting, a real SMS gateway, HTTPS termination (deployment concern per §7), and any front-end.
