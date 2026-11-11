import sys

import uvicorn
from pydantic import ValidationError

from app.config import Settings
from app.main import create_app


def main() -> None:
    try:
        settings = Settings()
    except ValidationError as exc:
        print(f"Invalid application configuration:\n{exc}", file=sys.stderr)
        raise SystemExit(2) from None
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                log_level=settings.log_level, access_log=False)


if __name__ == "__main__":
    main()
