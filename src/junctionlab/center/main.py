"""jl-center: run the central management system.

    jl-center --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import argparse
import logging

import uvicorn

from ..netutil import run as netrun
from .app import create_app


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--log-level", default="info")
    args = ap.parse_args()
    logging.basicConfig(level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    app = create_app()
    server = uvicorn.Server(uvicorn.Config(app, host=args.host, port=args.port, log_level=args.log_level))
    netrun(server.serve())


if __name__ == "__main__":
    main()
