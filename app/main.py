from contextlib import asynccontextmanager
import logging
from tempfile import TemporaryFile

from fastapi import FastAPI

from app.api.health import router
from app.api.business import router as business_router, http_error, openapi_document
from app.config import Settings
from app.storage import Storage
from app.reports.render import render_snapshot
from starlette.exceptions import HTTPException

logger = logging.getLogger("uvicorn.error")


def create_app(settings: Settings | None = None, *, renderer=render_snapshot) -> FastAPI:
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
        application.state.storage = Storage(config.data_dir)
        recovered = application.state.storage.recover_if_idle()
        if recovered:
            logger.info("Recovered %d interrupted run(s) as paused", len(recovered))
        logger.info("Strategy backend started")
        yield

    application = FastAPI(title="Strategy backend", version="0.1.0", lifespan=lifespan)
    application.state.settings = config
    application.state.renderer = renderer
    application.include_router(router)
    health_document = application.openapi()
    application.include_router(business_router)
    application.add_exception_handler(HTTPException, http_error)
    application.openapi_schema = openapi_document(health_document)
    application.openapi = lambda: application.openapi_schema
    from app.ui import install_ui
    install_ui(application, config)
    return application
