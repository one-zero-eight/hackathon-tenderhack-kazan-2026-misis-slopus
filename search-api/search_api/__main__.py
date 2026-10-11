from copy import deepcopy

import uvicorn
from uvicorn.config import LOGGING_CONFIG

from search_api.config import settings


def main() -> None:
    log_config = deepcopy(LOGGING_CONFIG)
    for formatter in log_config["formatters"].values():
        formatter["fmt"] = "%(asctime)s.%(msecs)03d " + formatter["fmt"]
        formatter["datefmt"] = "%H:%M:%S"
    uvicorn.run(
        "search_api.main:app",
        host=settings.host,
        port=settings.port,
        reload=False,
        log_config=log_config,
    )


if __name__ == "__main__":
    main()
