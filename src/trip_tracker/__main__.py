"""`python -m trip_tracker` → uvicorn server."""

from __future__ import annotations

import uvicorn


def main() -> None:
    uvicorn.run(
        "trip_tracker.app:create_app",
        host="0.0.0.0",  # noqa: S104  # nosec B104 - bound inside container; reverse proxy enforces auth
        port=8000,
        factory=True,
        proxy_headers=True,
        forwarded_allow_ips="*",
        log_config=None,  # we own logging
    )


if __name__ == "__main__":
    main()
