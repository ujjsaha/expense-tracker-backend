# Expense Tracker — Backend Services

FastAPI implementation of the SRS at `.vscode/.claude/specs/expense_tracker.md`.

| | |
| --- | --- |
| Framework | FastAPI + Pydantic v2 |
| Datastore | SQLite via SQLAlchemy 2.0 |
| Auth | JWT bearer tokens with refresh rotation |
| Password hashing | bcrypt |
| Docs | `/docs` (Swagger) and `/redoc` |

---

## Quick start

```bash
python -m venv .venv
.venv\Scripts\Activate.ps1          # PowerShell.  bash: source .venv/Scripts/activate
pip install -r requirements.txt

copy .env.example .env              # bash: cp .env.example .env
# For anything other than local development, set a real JWT_SECRET_KEY:
#   python -c "import secrets; print(secrets.token_urlsafe(48))"

uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000/docs.

```bash
pytest -q                            # 124 tests
ruff check . && ruff format --check .
```

---

## Endpoints

Twelve endpoints: the seven in SRS §5, plus five from the §8 open items that were
brought into scope (refresh tokens, OTP-verified reset, update, delete).

| Method | Path | Auth |
| --- | --- | --- |
| POST | `/user/register_user` | public |
| POST | `/user/login_user` | public |
| POST | `/user/refresh_token` | public |
| POST | `/user/logout` | bearer |
| POST | `/user/forgot_password/request_otp` | public |
| POST | `/user/forgot_password` | public |
| GET | `/user/fetch_user_by_id?user_id=` | bearer |
| POST | `/expense/create_expense` | bearer |
| POST | `/expense/fetch_expense` | bearer |
| GET | `/expense/fetch_expense_by_id?expense_id=` | bearer |
| PUT | `/expense/update_expense?expense_id=` | bearer |
| DELETE | `/expense/delete_expense?expense_id=` | bearer |

### Password reset (OTP)

The SRS names no SMS provider, so delivery is mocked.

1. `POST /user/forgot_password/request_otp` with `{"mobile": "9876543210"}`.
   The response is identical whether or not the number is registered — it is not
   a user-enumeration oracle.
2. **Read the code from the server log** (`[MOCK SMS] ...`), or set
   `EXPOSE_OTP_IN_RESPONSE=true` to have it echoed in the response body. That
   flag is refused when `APP_ENV=production`.
3. `POST /user/forgot_password` with `mobile`, `otp`, `password`,
   `confirm_password`.

Codes expire after 10 minutes, are single-use, lock out after 5 wrong attempts,
and a successful reset revokes every existing session.

`app/notifications/sms.py` is the seam to replace with a real gateway. In
production it currently logs an error rather than pretending delivery succeeded.

---

## Configuration

All settings come from the environment (`.env` locally). See `.env.example`.

| Variable | Default | Notes |
| --- | --- | --- |
| `APP_ENV` | `development` | `development` / `test` / `production` |
| `DATABASE_URL` | `sqlite:///./expense_tracker.db` | |
| `JWT_SECRET_KEY` | placeholder | **Startup fails** if left at the placeholder, or under 32 chars, when `APP_ENV != development` |
| `ACCESS_TOKEN_EXPIRE_MINUTES` | `15` | |
| `REFRESH_TOKEN_EXPIRE_DAYS` | `7` | |
| `OTP_EXPIRE_MINUTES` / `OTP_MAX_ATTEMPTS` | `10` / `5` | |
| `EXPOSE_OTP_IN_RESPONSE` | `false` | Blocked in production |
| `BCRYPT_ROUNDS` | `12` | Tests drop this to 4 for speed |

---

## Design decisions

Points where the SRS was silent or self-contradictory, and what was chosen:

**Response shapes.** SRS §6 mandates a `{status, message, data}` envelope, but
§5.2 and §5.7 show bare objects. Each endpoint returns the literal body its §5
entry specifies; the §6.2 *failure* envelope applies to every error.

**Amounts are integer minor units.** SQLite has no exact decimal type —
`Numeric` round-trips through a C double, losing cents and making `ORDER BY
amount` unreliable. `amount_minor` stores paise; `Decimal` conversion happens at
the schema boundary (`app/core/money.py`). JSON carries a number (`1250.5`) to
match the SRS examples rather than Pydantic's default string form.

**`confirm_password` is checked in the service, not the schema.** §5.1 and §5.4
require **400** for a mismatch. A Pydantic `model_validator` failure becomes 422
before the handler runs, so the comparison lives in
`user_service.assert_passwords_match`.

**`HTTPBearer(auto_error=False)`.** FastAPI's default returns **403** when the
`Authorization` header is missing; §4.4 requires **401**. Disabling the built-in
error lets `dependencies.py` raise the correct status. There is a parametrized
test over every protected route guarding this.

**`fetch_expense` is a POST.** SRS §5.6 defines its criteria as a JSON body
(`from_date`, `to_date`, `sort_by`, `sort_order`, `page_size`, `page`). A body on
a `GET` is not sent by Swagger UI or by most HTTP clients and has no clean
OpenAPI representation, so the route is a `POST` even though it only reads.

**Both range bounds are inclusive instants.** `from_date` and `to_date` are
required ISO 8601 datetimes; `to_date` is inclusive of exactly the instant given:

| `to_date` | Covers |
| --- | --- |
| `2026-08-01T12:00:00Z` | up to and including exactly that instant |
| `2026-08-01T23:59:59Z` | the whole day |
| `2026-08-01` | only `00:00:00Z` — a bare date is *not* widened to the day |

Naive datetimes are read as UTC and offsets are converted to it. A malformed or
missing bound is a 422 naming the offending field; `to_date < from_date` is a
**400**, since §5.6 makes it a business rule rather than a schema error.

`sort_by` is lower case (`date_time`, `amount`, `expense_category`,
`expense_name`) and `sort_order` upper case (`ASC`, `DESC`). Neither is coerced,
so wrong casing is a 422.

**Password capped at 72 bytes.** bcrypt truncates beyond that; without the cap, a
long passphrase would be silently weakened. Enforced in the schema as a 422.

**403 vs 404.** For expenses, a missing id is 404 and someone else's is 403, per
the §5.7 table. `fetch_user_by_id` does the same, which means the status code
pair reveals which user ids exist. That is the SRS's stated contract; collapsing
both to 403 would close the leak if that is preferred.

**`date_time` allows 60s of clock skew** so a client whose clock runs marginally
fast is not rejected by the "not a future date" rule.

---

## Layout

```
app/
├── main.py              app factory, lifespan, router mounts
├── core/                config, security, logging, exceptions, money, clock
├── db/                  Base, engine/session, UTCDateTime type
├── models/              User, Expense, RefreshToken, PasswordResetOTP
├── schemas/             request/response models and validation rules
├── routers/             user.py, expense.py
├── services/            business logic
├── notifications/       mock SMS gateway
└── dependencies.py      auth guard, DB session
tests/                   124 tests
```

**Security notes.** Passwords and OTPs are bcrypt-hashed; refresh tokens are
stored as SHA-256 digests (high entropy, and lookup must be deterministic).
Password fields exist only on request models, so they cannot appear in a
response. Every log line carries an `X-Request-ID` and passes through a
redaction filter that strips passwords, OTPs and tokens.

**Performance.** `ix_expenses_user_id_date_time` covers the range query; sorting,
counting, `LIMIT` and `OFFSET` all execute in SQL, never in application memory
(SRS §7).

---

## Not implemented

Deferred deliberately: Alembic migrations (schema is created by
`Base.metadata.create_all` at startup — add Alembic before the first deployed
schema change), multi-currency, rate limiting, a real SMS gateway, and HTTPS
termination (a deployment concern).
