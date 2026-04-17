"""
app/main.py — FastAPI application entrypoint.

This file creates the FastAPI app instance and registers top-level routes.
As the project grows, routes will move into routers/ and this file will
stay thin — just wiring things together.
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.routers import telegram as telegram_router
from app.routers import strava as strava_router


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Manage startup and shutdown tasks for the FastAPI application.

    asynccontextmanager turns this generator into a context manager. FastAPI
    runs everything BEFORE the `yield` at startup and everything AFTER at
    shutdown — similar to a try/finally block.

    We don't need to do anything at startup: the Supabase client uses a lazy
    singleton (initialized on first use), so there's nothing to set up here.

    At shutdown we close the Supabase HTTP connection pool to avoid "unclosed
    connection" warnings when the process exits. It's a cleanup courtesy.

    Note: the supabase import is inside the function rather than at the top of
    the file. This avoids a potential circular import as the project grows —
    if any module that main.py imports at the top level also imports from
    main.py, Python would see a circular dependency at import time. Deferring
    the import to inside the function body means it only resolves at runtime,
    when all modules are fully loaded.
    """
    # --- startup (nothing to do) ---
    yield
    # --- shutdown: close all service HTTP connection pools ---
    from app.services import supabase as supabase_service
    from app.services import strava as strava_service
    await supabase_service.close()
    await strava_service.close()


# FastAPI() creates the application object. Every route, middleware, and
# dependency in the project gets registered onto this object. The title
# and description show up in the auto-generated docs at /docs.
#
# lifespan= wires up our startup/shutdown handler above. This replaces the
# older @app.on_event("startup") / @app.on_event("shutdown") decorators,
# which are deprecated in recent FastAPI versions.
app = FastAPI(
    title="Strava Coach Bot",
    description="AI cycling coach powered by Claude and Strava",
    version="0.1.0",
    lifespan=lifespan,
)

# include_router registers all routes defined in telegram_router.router
# under the "/telegram" prefix. So the webhook handler defined as
# @router.post("/webhook") becomes POST /telegram/webhook in the app.
app.include_router(telegram_router.router, prefix="/telegram", tags=["telegram"])
app.include_router(strava_router.router, prefix="/strava", tags=["strava"])


# @app.get("/health") is a decorator that registers the function below as
# the handler for HTTP GET requests to the path "/health".
#
# Decorators in Python wrap a function — here FastAPI uses them to build
# an internal route table mapping (method, path) → handler function.
@app.get("/health")
async def health_check() -> dict:
    """
    Health check endpoint.

    Returns a simple JSON response to confirm the server is running.
    Used by load balancers and deployment platforms to verify the app is alive.
    """
    # FastAPI automatically serializes a Python dict to a JSON response.
    # The Content-Type header will be set to application/json for us.
    return {"status": "ok"}
