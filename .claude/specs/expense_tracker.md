# Expense Tracker — Backend Services

## Software Requirements Specification

| Field            | Value                                  |
| ---------------- | -------------------------------------- |
| Document title   | Expense Tracker Backend Services — SRS |
| Version          | 1.0                                    |
| Status           | Draft                                  |
| Date             | 01 August 2026                         |
| Framework        | FastAPI (Python)                       |
| Validation layer | Pydantic                               |
| Auth mechanism   | JWT (Bearer)                           |

---

## 1. Introduction

### 1.1 Purpose

This document specifies the functional and technical requirements for the backend REST services of an Expense Tracker application. It defines the API surface, request/response contracts, data models, validation rules, and security requirements needed for implementation and testing.

### 1.2 Scope

The backend exposes two functional domains:

- **User domain** — registration, retrieval, login, and password recovery.
- **Expense domain** — expense creation, retrieval by date range with sorting and pagination, and retrieval by identifier.

Front-end applications, mobile clients, reporting dashboards, and analytics are out of scope for this release.

### 1.3 Definitions and Abbreviations

| Term         | Meaning                                                |
| ------------ | ------------------------------------------------------ |
| JWT          | JSON Web Token, used for stateless authentication      |
| Bearer token | Token passed in the `Authorization` HTTP header        |
| Pagination   | Returning a bounded subset of a result set             |
| ISO 8601     | Date/time format standard, e.g. `2026-08-01T14:30:00Z` |

---

## 2. Technology Stack

| Layer             | Technology                                                                |
| ----------------- | ------------------------------------------------------------------------- |
| Web framework     | FastAPI                                                                   |
| Data validation   | Pydantic models (request and response schemas)                            |
| Authentication    | JWT — signed tokens, verified via a FastAPI dependency                    |
| Password storage  | One-way hash (bcrypt or Argon2) — plaintext passwords are never persisted |
| Server            | ASGI (Uvicorn)                                                            |
| API documentation | Auto-generated OpenAPI / Swagger UI                                       |

---

## 3. Data Models

All request and response bodies are defined as Pydantic models. Validation is declarative and enforced before any handler logic executes.

### 3.1 User Model

| Field              | Type              | Required         | Validation                                                                                |
| ------------------ | ----------------- | ---------------- | ----------------------------------------------------------------------------------------- |
| `user_id`          | integer / UUID    | System-generated | Unique; not accepted on registration                                                      |
| `name`             | string            | Yes              | 2–50 characters; alphabetic and spaces only                                               |
| `mobile`           | string            | Yes              | Exactly 10 digits (or E.164 with country code); unique                                    |
| `email`            | string (EmailStr) | Yes              | Valid email format; unique across users                                                   |
| `password`         | string            | Yes              | Min 8 characters; at least one uppercase, one lowercase, one digit, one special character |
| `confirm_password` | string            | Yes              | Must exactly match `password`                                                             |
| `user_type`        | string (enum)     | No               | `admin`, `member`; defaults to `member`                                                   |
| `status`           | string (enum)     | No               | `active`, `inactive`; defaults to `active`                                                |

### 3.2 Expense Model

| Field              | Type           | Required         | Validation                                            |
| ------------------ | -------------- | ---------------- | ----------------------------------------------------- |
| `expense_id`       | integer / UUID | System-generated | Unique                                                |
| `user_id`          | integer / UUID | Yes              | Must reference an existing user; derived from the JWT |
| `expense_category` | string / enum  | Yes              | Non-empty; restricted to a defined category list      |
| `expense_name`     | string         | Yes              | 1–100 characters                                      |
| `amount`           | decimal        | Yes              | Greater than 0; maximum 2 decimal places              |
| `date_time`        | datetime       | Yes              | Valid ISO 8601 datetime; not a future date            |

### 3.3 Cross-Field Validation Rules

- `confirm_password` must equal `password` — enforced by a Pydantic model-level validator.
- Password fields are write-only and must never appear in any response payload.
- On expense creation, `user_id` is taken from the authenticated token rather than trusted from the request body.

---

## 4. Authentication and Authorization

### 4.1 Token Issuance

A JWT is issued on successful login. The token payload carries the user identifier, issue time, and expiry.

### 4.2 Token Transmission

Clients send the token on every protected request:

```
Authorization: Bearer <token>
```

### 4.3 Protected vs. Public Endpoints

| Endpoint                       | Authentication |
| ------------------------------ | -------------- |
| `/user/register_user`          | Public         |
| `/user/login_user`             | Public         |
| `/user/forgot_password`        | Public         |
| `/user/fetch_user_by_id`       | Required       |
| `/user/fetch_all_users`        | Required       |
| `/user/deactivate_user`        | Required       |
| `/expense/create_expense`      | Required       |
| `/expense/fetch_expense`       | Required       |
| `/expense/fetch_expense_by_id` | Required       |

### 4.4 Authorization Rules

- Token validity — signature, expiry, and structure — is verified through a reusable FastAPI dependency applied to all protected routes.
- A user may only read or modify their own records. Requests for another user's data return `403 Forbidden`.
- Expired or malformed tokens return `401 Unauthorized`.

---

## 5. API Specification

### 5.1 `POST /user/register_user`

Registers a new user account, or updates an existing one.

**Request body**

```json
{
  "name": "Priya Sharma",
  "mobile": "9876543210",
  "email": "priya@example.com",
  "password": "Str0ng@Pass",
  "confirm_password": "Str0ng@Pass",
  "user_type": "member",
  "status": "active"
}
```

`user_type` and `status` are optional; omitting them defaults to `member` and `active` respectively. Either field being present with a value outside its enum (e.g. `"owner"` or `"banned"`) is rejected with `422`.

**Behaviour**

- Validates all fields against the User model.
- **New user** — if no user with the given `email` exists, one is created. Rejects a `mobile` already belonging to another user. Hashes the password before persisting. Returns `201`.
- **Existing user** — if a user with the given `email` already exists, that record is updated in place instead of creating a duplicate. Returns `200`.
  - `password`, `confirm_password`, and `email` are immutable: any values supplied for them are silently ignored — neither applied nor validated against each other — and the stored values are left unchanged. Since `email` also doubles as the lookup key, there is no way through this endpoint to rename an existing account; a different `email` simply does not match it and is handled as a new registration (subject to the usual `mobile` uniqueness check).
  - `name`, `mobile`, `user_type`, and `status` update normally. A `mobile` already belonging to a *different* user is rejected with `409` and the update does not apply.
- `user_type` and `status` are not echoed back in the response; fetch the record via `/user/fetch_all_users` (5.8) to confirm the effective values.

**Responses**

| Code | Condition                                    | Body                                                                                 |
| ---- | --------------------------------------------- | ------------------------------------------------------------------------------------ |
| 201  | New user registered                           | `{ "status": "success", "message": "User registered successfully", "user_id": 101 }` |
| 200  | Existing user's record updated                | `{ "status": "success", "message": "User updated successfully", "user_id": 101 }`    |
| 400  | Passwords do not match (new user only)        | `{ "status": "failure", "message": "Password and confirm password do not match" }`   |
| 409  | `mobile` already registered to another user   | `{ "status": "failure", "message": "Mobile number already registered" }`             |
| 422  | Field validation failure                      | Standard validation error detail                                                     |

---

### 5.2 `GET /user/fetch_user_by_id`

Returns the profile of a single user.

**Input** — `user_id` supplied as a path or query parameter.

**Responses**

| Code | Condition                        | Body                                                                                               |
| ---- | -------------------------------- | -------------------------------------------------------------------------------------------------- |
| 200  | User found                       | `{ "user_id": 101, "name": "Priya Sharma", "mobile": "9876543210", "email": "priya@example.com" }` |
| 401  | Missing or invalid token         | Error detail                                                                                       |
| 403  | Requesting another user's record | Error detail                                                                                       |
| 404  | No user matches the identifier   | `{ "status": "failure", "message": "User not found" }`                                             |

Password fields are excluded from the response. `user_type` and `status` are also excluded here (and from 5.3's `user` object) - they are only surfaced in the admin listing at 5.8.

---

### 5.3 `POST /user/login_user`

Authenticates a user and issues a JWT.

**Request body**

```json
{
  "email": "priya@example.com",
  "password": "Str0ng@Pass"
}
```

**Behaviour**

- Only users with `status = active` may log in. A user with `status = inactive` is rejected even when the email and password are correct.
- The account-status check runs only after the credentials have been verified — checking it first would let an unauthenticated caller learn whether an email is registered and active without knowing the password, turning the check into an enumeration oracle.

**Responses**

| Code | Condition           | Body                                                              |
| ---- | ------------------- | ----------------------------------------------------------------- |
| 200  | Credentials valid and account active | User details plus `access_token` and `token_type`   |
| 401  | Credentials invalid | `{ "status": "failure", "message": "Invalid email or password" }` |
| 403  | Credentials valid but account is inactive | `{ "status": "failure", "message": "Account is inactive" }` |
| 422  | Malformed input     | Validation error detail                                           |

The 401 failure message is deliberately generic so it does not reveal whether the email exists. The 403 message is specific because it is only reachable once the credentials have already been confirmed correct, so nothing new is disclosed to an attacker.

---

### 5.4 `POST /user/forgot_password`

Resets a user's password using their registered mobile number.

**Request body**

```json
{
  "mobile": "9876543210",
  "password": "N3w@Password",
  "confirm_password": "N3w@Password"
}
```

**Responses**

| Code | Condition                    | Body                                                                               |
| ---- | ---------------------------- | ---------------------------------------------------------------------------------- |
| 200  | Password updated             | `{ "status": "success", "message": "Password updated successfully" }`              |
| 400  | Passwords do not match       | `{ "status": "failure", "message": "Password and confirm password do not match" }` |
| 404  | Mobile number not registered | `{ "status": "failure", "message": "Mobile number not found" }`                    |

> **Note:** Mobile number alone is a weak identity proof. An OTP verification step before the reset is recommended as a follow-up enhancement.

---

### 5.5 `POST /expense/create_expense`

Records a new expense against the authenticated user.

**Request body**

```json
{
  "expense_category": "Food",
  "expense_name": "Team lunch",
  "amount": 1250.5,
  "date_time": "2026-08-01T13:45:00Z"
}
```

**Behaviour**

- All fields validated by the Expense Pydantic model.
- `user_id` is resolved from the JWT, not the request body.
- `expense_id` is generated by the system.

**Responses**

| Code | Condition                | Body                                                                                     |
| ---- | ------------------------ | ---------------------------------------------------------------------------------------- |
| 201  | Expense created          | `{ "status": "success", "message": "Expense created successfully", "expense_id": 5001 }` |
| 400  | Business rule violation  | Failure message                                                                          |
| 401  | Unauthenticated          | Error detail                                                                             |
| 422  | Field validation failure | Validation error detail                                                                  |

---

### 5.6 `POST /expense/fetch_expense`

Returns expenses for the authenticated user within a date range, sorted and paginated.

The query criteria are sent as a JSON request body rather than as query parameters, so the endpoint is a `POST` even though it only reads. A body on a `GET` is not reliably transmitted by Swagger UI or by most HTTP clients.

**Request body**

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

The values above are illustrative, not defaults.

**Fields**

| Field        | Type                   | Required | Allowed values                                                     | Default     |
| ------------ | ---------------------- | -------- | ------------------------------------------------------------------ | ----------- |
| `from_date`  | ISO 8601 UTC datetime  | Yes      | —                                                                  | —           |
| `to_date`    | ISO 8601 UTC datetime  | Yes      | Must be ≥ `from_date`                                              | —           |
| `sort_by`    | string (enum)          | No       | `expense_category`, `expense_name`, `amount`, `date_time`          | `date_time` |
| `sort_order` | string (enum)          | No       | `ASC`, `DESC`                                                      | `DESC`      |
| `page_size`  | positive integer       | No       | 1–100                                                              | `10`        |
| `page`       | positive integer       | No       | ≥ 1                                                                | `1`         |

**Date-range semantics**

- Both bounds are **inclusive instants**: the query matches `from_date <= date_time <= to_date`.
- `to_date` is inclusive of exactly the instant supplied. To cover a whole day, pass `2026-08-06T23:59:59Z`; passing the bare date `2026-08-06` means `00:00:00Z` on that day and will exclude anything later.
- A value without a timezone offset is interpreted as UTC. A value with an offset is converted to UTC, so `2026-08-06T17:30:00+05:30` and `2026-08-06T12:00:00Z` select the same instant.
- `from_date == to_date` is a valid single-instant range.
- Enum values are matched exactly: `sort_by` is lower case, `sort_order` is upper case. `asc` and `DATE_TIME` are both rejected with a 422.

**Response — 200**

```json
{
  "status": "success",
  "page": 1,
  "page_size": 10,
  "total_records": 42,
  "total_pages": 5,
  "data": [
    {
      "expense_id": 5001,
      "expense_category": "Food",
      "expense_name": "Team lunch",
      "amount": 1250.5,
      "date_time": "2026-08-01T13:45:00Z"
    }
  ]
}
```

**Other responses**

| Code | Condition                                                                       |
| ---- | ------------------------------------------------------------------------------- |
| 400  | `to_date` earlier than `from_date`                                              |
| 401  | Unauthenticated                                                                 |
| 422  | Missing bound, invalid sort field or order, out-of-range page, or bad datetime  |

An inverted range is a **400**, not a 422: it is a business rule, so it is enforced in the service layer after schema parsing rather than by a model validator.

An empty range returns `200` with an empty `data` array rather than a 404.

---

### 5.7 `GET /expense/fetch_expense_by_id`

Returns a single expense record.

**Input** — `expense_id` supplied as a path or query parameter.

**Responses**

| Code | Condition                             | Body                                                      |
| ---- | ------------------------------------- | --------------------------------------------------------- |
| 200  | Expense found and owned by the caller | Full expense object                                       |
| 401  | Unauthenticated                       | Error detail                                              |
| 403  | Expense belongs to another user       | Error detail                                              |
| 404  | No expense matches the identifier     | `{ "status": "failure", "message": "Expense not found" }` |

---

### 5.8 `POST /user/fetch_all_users`

Returns every registered user, sorted and paginated. Optionally filtered by a free-text `search`.

The query criteria are sent as a JSON request body rather than as query parameters, for the same reason as 5.6 (a body on `GET` is not reliably transmitted).

**Request body**

```json
{
  "sort_by": "name",
  "sort_order": "ASC",
  "page_size": 10,
  "page": 1,
  "search": "priya"
}
```

The values above are illustrative, not defaults.

**Fields**

| Field        | Type              | Required | Allowed values          | Default |
| ------------ | ----------------- | -------- | ------------------------ | ------- |
| `sort_by`    | string (enum)     | No       | `name`, `email`, `mobile` | `name`  |
| `sort_order` | string (enum)     | No       | `ASC`, `DESC`             | `ASC`   |
| `page_size`  | positive integer  | No       | 1–100                     | `10`    |
| `page`       | positive integer  | No       | ≥ 1                       | `1`     |
| `search`     | string or `null`  | No       | any string                | `null`  |

Enum values are matched exactly: `sort_by` is lower case, `sort_order` is upper case. `Name` and `asc` are both rejected with a 422.

`search` matches a single string across five fields - `name`, `mobile`, `email`, `user_type`, and `status` - and a user is included if the string matches *any one* of them (OR, not AND). Matching is partial (substring) and case-insensitive: `"pri"` matches a name of `"Priya Sharma"`, and `"ADMIN"` matches a `user_type` of `admin`. If `search` is absent, `null`, or an empty/blank string, every user is returned, exactly as if it had not been sent. Pagination and sorting apply to the filtered set - `total_records`/`total_pages` reflect matches only, not the full table.

**Response — 200**

```json
{
  "status": "success",
  "page": 1,
  "page_size": 10,
  "total_records": 42,
  "total_pages": 5,
  "data": [
    {
      "user_id": 101,
      "name": "Priya Sharma",
      "mobile": "9876543210",
      "email": "priya@example.com",
      "user_type": "member",
      "status": "active"
    }
  ]
}
```

`user_id`, `name`, `email`, `mobile`, `user_type`, and `status` are returned per user - this is the one listing that surfaces `user_type`/`status`, since it is the admin-facing view (5.2's single-profile lookup and 5.3's login response omit them). `password` and `confirm_password` are never present in the response.

**Other responses**

| Code | Condition                                            |
| ---- | ----------------------------------------------------- |
| 401  | Missing, expired, or invalid token                     |
| 422  | Invalid sort field, invalid sort order, or bad page/page_size |

---

### 5.9 `POST /user/deactivate_user`

Activates or deactivates a user account. The requested `status` selects the outcome. Restricted to admin users.

**Request body**

```json
{
  "user_id": 101,
  "status": "inactive"
}
```

**Fields**

| Field     | Type          | Required | Allowed values      |
| --------- | ------------- | -------- | -------------------- |
| `user_id` | integer       | Yes      | Must reference an existing user |
| `status`  | string (enum) | Yes      | `active`, `inactive` |

**Behaviour**

- Requires a Bearer token (SRS 4.2).
- Only an admin user (`user_type: "admin"`) may call this; a non-admin requester returns `403 Forbidden`, regardless of whose account is targeted.
- `status: "active"` sets the target user's `status` to `active`. `status: "inactive"` sets it to `inactive`.
- Any `status` value outside `active`/`inactive` is rejected with `422` before the handler runs.
- Setting a user to the status it already has is idempotent and still returns `200`.

**Responses**

| Code | Condition                        | Body                                                                    |
| ---- | --------------------------------- | ------------------------------------------------------------------------ |
| 200  | Account activated                 | `{ "status": "success", "message": "User activated successfully" }`      |
| 200  | Account deactivated               | `{ "status": "success", "message": "User deactivated successfully" }`    |
| 401  | Missing or invalid token          | `{ "status": "failure", "message": "Not authenticated" }`                |
| 403  | Requester is not an admin         | `{ "status": "failure", "message": "Only admin users can activate or deactivate accounts" }` |
| 404  | No user matches the identifier    | `{ "status": "failure", "message": "User not found" }`                   |
| 422  | `status` outside `active`/`inactive`, or missing/invalid `user_id` | Standard validation error detail |

---

## 6. Standard Response Format

### 6.1 Success Envelope

```json
{
  "status": "success",
  "message": "Operation completed",
  "data": {}
}
```

### 6.2 Failure Envelope

```json
{
  "status": "failure",
  "message": "Description of what went wrong",
  "errors": []
}
```

### 6.3 HTTP Status Code Usage

| Code | Meaning                                     |
| ---- | ------------------------------------------- |
| 200  | Request succeeded                           |
| 201  | Resource created                            |
| 400  | Business rule violation                     |
| 401  | Missing, expired, or invalid token          |
| 403  | Authenticated but not permitted, or valid credentials for an inactive account |
| 404  | Resource not found                          |
| 409  | Conflict — mobile number already registered to another user |
| 422  | Schema validation failure (FastAPI default) |
| 500  | Unhandled server error                      |

---

## 7. Non-Functional Requirements

| Area           | Requirement                                                                                                                              |
| -------------- | ---------------------------------------------------------------------------------------------------------------------------------------- |
| Security       | Passwords hashed at rest; JWT secret held in environment configuration, never in source control; HTTPS enforced in deployed environments |
| Validation     | Every endpoint input passes through a Pydantic model; no unvalidated data reaches business logic                                         |
| Documentation  | OpenAPI schema published automatically at `/docs` and `/redoc`                                                                           |
| Error handling | Global exception handler returning the standard failure envelope; internal stack traces never exposed to clients                         |
| Logging        | Request-level logging with correlation identifiers; passwords and tokens excluded from log output                                        |
| Performance    | Date-range queries indexed on `user_id` and `date_time`; pagination applied at the database layer, not in application memory             |

---

## 8. Assumptions and Open Items

**Assumptions**

- One user owns many expenses; expenses are never shared between users.
- A single relational datastore backs both domains.
- Currency is uniform across the system for this release.

**Open items for clarification**

1. Should password reset require OTP verification on the registered mobile number?
2. Is the expense category list fixed, or user-definable?
3. Are update and delete operations on expenses required in a later phase?
4. What is the intended JWT expiry window, and is a refresh-token flow needed?
5. Is multi-currency support anticipated?

---

## 9. Suggested Project Structure

```
app/
├── main.py                 # FastAPI application entry point
├── core/
│   ├── config.py           # Environment configuration
│   └── security.py         # JWT creation, verification, password hashing
├── models/                 # Database models
├── schemas/
│   ├── user.py             # Pydantic user schemas
│   └── expense.py          # Pydantic expense schemas
├── routers/
│   ├── user.py             # /user endpoints
│   └── expense.py          # /expense endpoints
├── services/               # Business logic
└── dependencies.py         # Shared dependencies, including auth guard
```
