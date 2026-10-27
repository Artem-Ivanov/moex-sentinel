"""Start the standalone Analytics HTTP service."""

import uvicorn


def main() -> None:
    uvicorn.run("market_analytics.app:create_app", factory=True, host="0.0.0.0", port=8001)


if __name__ == "__main__":
    main()
