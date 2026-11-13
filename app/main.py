from contextlib import asynccontextmanager
import logging
from tempfile import TemporaryFile

from fastapi import FastAPI

from app.api.health import router
from app.api.business import router as business_router, Commands
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from pathlib import Path
from app.config import Settings
from app.storage import Storage

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
        application.state.storage = Storage(config.data_dir)
        application.state.commands = Commands(application.state.storage)
        recovered = application.state.storage.recover_if_idle()
        if recovered:
            logger.info("Recovered %d interrupted run(s) as paused", len(recovered))
        logger.info("Strategy backend started")
        yield

    application = FastAPI(title="Strategy backend", version="0.1.0", lifespan=lifespan)
    application.state.settings = config
    application.include_router(router)
    application.include_router(business_router)
    application.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost','127.0.0.1','[::1]','testserver'])
    ui = Path(__file__).resolve().parent / 'ui' / 'static'
    application.mount('/assets', StaticFiles(directory=ui), name='assets')

    @application.middleware('http')
    async def local_headers(request, call_next):
        response = await call_next(request)
        if request.url.path == '/' or request.url.path.startswith('/assets/'):
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @application.get('/', include_in_schema=False)
    def interface():
        return FileResponse(ui / 'index.html')

    @application.get('/api/v1/capabilities')
    def capabilities():
        return dict(stage_factory=config.stage_factory, synthetic=config.stage_factory=='demo',
                    worker='separate-process', exports=['markdown','html'])

    @application.get('/api/v1/fixtures/{name}')
    def fixture(name: str):
        from fastapi import HTTPException
        import json
        allowed={'success','needs_input','conflicting_sources','unsupported_promise','search_error','stale_result'}
        if name not in allowed: raise HTTPException(404)
        return json.loads((Path(__file__).resolve().parents[1]/'contracts/examples'/(name+'.json')).read_text())
    return application
