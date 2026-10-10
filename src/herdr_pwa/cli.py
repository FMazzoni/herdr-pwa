"""Command-line entry point."""

import argparse

import uvicorn
from fastapi import FastAPI

from herdr_pwa.settings import Settings


def create_app() -> FastAPI:
    """Build the web application."""
    app = FastAPI()

    @app.get("/healthz")
    def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        return {"status": "ok"}

    return app


def main(argv: list[str] | None = None) -> None:
    """Parse arguments and run the chosen command."""
    parser = argparse.ArgumentParser(prog="herdr-pwa")
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve")
    serve.add_argument("--port", type=int)
    args = parser.parse_args(argv)
    settings = Settings()
    port: int | None = args.port  # pyright: ignore[reportAny]
    uvicorn.run(create_app(), host=settings.host, port=port or settings.port)
