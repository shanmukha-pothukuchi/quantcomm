"""Run the FloodAlpha web app:  .venv/bin/python -m floodalpha.web [--port 8000] [--reload]"""
from __future__ import annotations

import argparse
import logging

import uvicorn

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8000


def main() -> None:
    ap = argparse.ArgumentParser(description="Serve the FloodAlpha landing page, dashboard and stocks page.")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--reload", action="store_true", help="restart on code changes (development)")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    uvicorn.run("floodalpha.web.server:app", host=args.host, port=args.port, reload=args.reload)


if __name__ == "__main__":
    main()
