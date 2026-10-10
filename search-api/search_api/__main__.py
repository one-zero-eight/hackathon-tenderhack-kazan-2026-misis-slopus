import uvicorn

from search_api.config import settings


def main() -> None:
    uvicorn.run(
        "search_api.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
    )


if __name__ == "__main__":
    main()
