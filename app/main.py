"""FastAPI application entry point.

Run locally with:  uvicorn app.main:app --reload
"""

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app import __version__
from app.core.config import Settings, get_settings
from app.core.exceptions import register_exception_handlers
from app.core.logging import RequestIdMiddleware, setup_logging
from app.db.session import init_db
from app.routers import chatbot as chatbot_router
from app.routers import expense as expense_router
from app.routers import user as user_router

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
Backend REST services for the Expense Tracker application.

* **User domain** - registration, login, token refresh, password recovery, profile lookup.
* **Expense domain** - create, list by date range with sorting and pagination,
  fetch by id, update, delete.
* **Chatbot domain** - LangGraph-backed conversational queries over a user's
  expenses and profile, streamed via SSE, with persisted history.

All protected endpoints expect `Authorization: Bearer <access_token>`. The
chatbot endpoints are the one exception - they take `user_id` directly in
the request body (see `.claude/specs/chatbot.md`).
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = get_settings()
    setup_logging(settings.log_level)
    logger.info("Starting %s v%s (env=%s)", settings.app_name, __version__, settings.app_env)
    if settings.is_development and settings.jwt_secret_key == "change-me-this-is-not-a-real-secret":
        logger.warning(
            "JWT_SECRET_KEY is still the placeholder value. Fine for local development, "
            "but the app will refuse to start with it once APP_ENV is not 'development'."
        )
    init_db()
    yield
    logger.info("Shutting down %s", settings.app_name)


def create_app() -> FastAPI:
    settings = get_settings()

    app = FastAPI(
        title=settings.app_name,
        description=API_DESCRIPTION,
        version=__version__,
        lifespan=lifespan,
        docs_url="/docs",
        redoc_url="/redoc",
    )

    app.add_middleware(RequestIdMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    register_exception_handlers(app)

    app.include_router(user_router.router)
    app.include_router(expense_router.router)
    app.include_router(chatbot_router.router)

    @app.get("/health", tags=["meta"], summary="Liveness probe")
    def health() -> dict[str, str]:
        return {"status": "success", "message": "Service is healthy"}

    return app


app = create_app()
