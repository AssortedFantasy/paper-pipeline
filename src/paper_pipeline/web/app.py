"""FastAPI application factory. Routes implemented by WP-4.1."""

from fastapi import FastAPI


def create_app() -> FastAPI:
    """Create the web application. Wiring of services happens here."""
    app = FastAPI(title="Paper Pipeline")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    return app
