"""Run the vault server: `python -m app`.

Binds to 0.0.0.0:8000 by default so the vault is reachable from other
devices on the local network (phone, tablet). Override with LOCK_HOST
and LOCK_PORT, e.g. `LOCK_HOST=127.0.0.1 python -m app` to make it
loopback-only again.
"""

import os

import uvicorn


def main() -> None:
    host = os.environ.get("LOCK_HOST", "0.0.0.0")
    port = int(os.environ.get("LOCK_PORT", "8000"))
    uvicorn.run("app.main:app", host=host, port=port)


if __name__ == "__main__":
    main()
