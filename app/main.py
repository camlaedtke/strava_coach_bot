"""
app/main.py — FastAPI application entrypoint.

This file creates the FastAPI app instance and registers top-level routes.
As the project grows, routes will move into routers/ and this file will
stay thin — just wiring things together.
"""

from fastapi import FastAPI

# FastAPI() creates the application object. Every route, middleware, and
# dependency in the project gets registered onto this object. The title
# and description show up in the auto-generated docs at /docs.
app = FastAPI(
    title="Strava Coach Bot",
    description="AI cycling coach powered by Claude and Strava",
    version="0.1.0",
)


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
