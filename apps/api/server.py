"""启动 Word 与飞书共用的 CCiteheck HTTP 服务。"""

from __future__ import annotations

import argparse
from pathlib import Path

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=3000)
    parser.add_argument("--cert", type=Path)
    parser.add_argument("--key", type=Path)
    args = parser.parse_args()

    if (args.cert is None) != (args.key is None):
        parser.error("--cert and --key must be provided together")

    uvicorn.run(
        "apps.api.app:app",
        host=args.host,
        port=args.port,
        ssl_certfile=str(args.cert) if args.cert else None,
        ssl_keyfile=str(args.key) if args.key else None,
    )


if __name__ == "__main__":
    main()
