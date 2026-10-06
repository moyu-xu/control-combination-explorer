from __future__ import annotations

import os
import secrets
import socket
import threading
import time
import webbrowser
from datetime import UTC, datetime
from pathlib import Path

import uvicorn


def debug_log(message: str) -> None:
    path = os.environ.get("CCE_DEBUG_LOG")
    if not path:
        return
    with Path(path).open("a", encoding="utf-8") as stream:
        stream.write(f"{datetime.now(UTC).isoformat()} {message}\n")


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def main() -> None:
    debug_log("launcher started")
    from .main import app

    debug_log("application imported")
    configured_port = os.environ.get("CCE_PORT")
    port = int(configured_port) if configured_port else free_port()
    debug_log(f"port selected: {port}")
    token = os.environ.get("CCE_TOKEN") or secrets.token_urlsafe(32)
    os.environ["LOCAL_APP_TOKEN"] = token
    debug_log("token configured")
    config = uvicorn.Config(
        app,
        host="127.0.0.1",
        port=port,
        log_level="warning",
        log_config=None,
        access_log=False,
        loop="asyncio",
        http="h11",
        ws="websockets-sansio",
    )
    debug_log("uvicorn config created")
    server = uvicorn.Server(config)
    debug_log("uvicorn server created")
    app.state.shutdown_callback = lambda: setattr(server, "should_exit", True)
    debug_log("shutdown callback configured")

    def open_browser() -> None:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=0.25):
                    webbrowser.open(f"http://127.0.0.1:{port}/?token={token}")
                    return
            except OSError:
                time.sleep(0.1)

    if os.environ.get("CCE_NO_BROWSER") != "1":
        threading.Thread(target=open_browser, daemon=True).start()
    debug_log(f"server starting on {port}")
    server.run()
    debug_log("server stopped")


if __name__ == "__main__":
    main()
