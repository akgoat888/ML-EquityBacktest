"""AI Equities Desk — launch FastAPI backend + Flask frontend.

  python app.py

API:  http://127.0.0.1:8000/docs
UI:   http://127.0.0.1:5050
"""
from __future__ import annotations

import os
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

API_HOST = os.getenv("API_HOST", "127.0.0.1")
API_PORT = int(os.getenv("API_PORT", "8000"))
WEB_HOST = os.getenv("WEB_HOST", "127.0.0.1")
WEB_PORT = int(os.getenv("WEB_PORT", "5050"))
os.environ.setdefault("API_URL", f"http://{API_HOST}:{API_PORT}")


def run_api() -> None:
    import uvicorn

    uvicorn.run(
        "aieq.api:app",
        host=API_HOST,
        port=API_PORT,
        log_level="info",
        reload=False,
    )


def run_web() -> None:
    from web.server import app

    app.run(host=WEB_HOST, port=WEB_PORT, debug=False, use_reloader=False)


if __name__ == "__main__":
    from aieq.yahoo import configure
    from aieq.store import backend_name, ensure_schema

    configure()
    ensure_schema()
    api_thread = threading.Thread(target=run_api, name="fastapi", daemon=True)
    api_thread.start()
    time.sleep(0.8)
    print(f"Desk UI  http://{WEB_HOST}:{WEB_PORT}")
    print(f"API docs http://{API_HOST}:{API_PORT}/docs")
    print(f"Store    {backend_name()}")
    try:
        run_web()
    except KeyboardInterrupt:
        print("\nStopped.")
        os._exit(0)
