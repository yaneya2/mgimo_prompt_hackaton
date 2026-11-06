from contextlib import asynccontextmanager
import logging
from tempfile import TemporaryFile

from fastapi import FastAPI

from app.api.health import router
from app.config import Settings

logger = logging.getLogger("uvicorn.error")


def create_app(settings: Settings | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        try:
            config.data_dir.mkdir(parents=True, exist_ok=True)
            with TemporaryFile(dir=config.data_dir) as probe:
                probe.write(b"storage-check")
                probe.flush()
        except OSError as exc:
            raise RuntimeError("APP_DATA_DIR must be an accessible writable directory") from exc
        logger.info("Strategy backend started")
        yield

    application = FastAPI(title="Strategy backend", version="0.1.0", lifespan=lifespan)
    application.state.settings = config
    application.include_router(router)
    return application
