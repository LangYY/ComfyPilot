from __future__ import annotations

import argparse
import os

import uvicorn

import app_v2 as studio_app


app = studio_app.app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run ComfyPilot from the original project entrypoint.")
    parser.add_argument(
        "--host",
        default=os.environ.get("LTX_BATCH_HOST", os.environ.get("BATCH_STUDIO_V2_HOST", "127.0.0.1")),
        help="Host to bind. Defaults to 127.0.0.1.",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("LTX_BATCH_PORT", os.environ.get("BATCH_STUDIO_V2_PORT", "8000"))),
        help="Port to bind. Defaults to the original 8000 port.",
    )
    parser.add_argument(
        "--public",
        action="store_true",
        help="Bind to 0.0.0.0 so LAN/tunnel devices can reach this app.",
    )
    parser.add_argument(
        "--access-token",
        default=os.environ.get("BATCH_STUDIO_ACCESS_TOKEN", ""),
        help="Optional access token required for API and file endpoints.",
    )
    args = parser.parse_args()

    if args.public:
        args.host = "0.0.0.0"

    studio_app.ACCESS_TOKEN = str(args.access_token or "").strip()
    if args.host in {"0.0.0.0", "::"} and not studio_app.ACCESS_TOKEN:
        print("[WARN] Public bind is enabled without an access token. Use --access-token or BATCH_STUDIO_ACCESS_TOKEN.")

    uvicorn.run(app, host=args.host, port=args.port, reload=False)
