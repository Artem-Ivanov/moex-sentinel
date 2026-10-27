"""Container entrypoint that serves the already migrated HTTP application."""

import uvicorn


def main() -> None:
    uvicorn.run(
        "moex_sentinel.api.app:app",
        host="0.0.0.0",
        port=8000,
        workers=1,
        access_log=False,
    )


if __name__ == "__main__":
    main()
