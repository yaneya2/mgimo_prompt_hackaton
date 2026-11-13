"""Static UI and read-only views over the shared platform."""
from pathlib import Path
import json
from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.trustedhost import TrustedHostMiddleware
from app.llm.summary import aggregate_usage


def install_ui(app, config):
    root = Path(__file__).resolve().parents[2]
    app.mount('/assets', StaticFiles(directory=root/'app/ui/static'), name='assets')
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=['localhost','127.0.0.1','[::1]','testserver'])

    @app.middleware('http')
    async def headers(request, call_next):
        origin = request.headers.get('origin')
        if request.method == 'POST' and origin and origin.rstrip('/') != str(request.base_url).rstrip('/'):
            return JSONResponse({'error': {'code': 'VALIDATION_ERROR', 'message': 'Используйте локальный интерфейс.'}}, status_code=403)
        response = await call_next(request)
        if request.url.path == '/' or request.url.path.startswith('/assets/'):
            response.headers['Content-Security-Policy'] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'"
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.get('/', include_in_schema=False)
    def index():
        return FileResponse(root/'app/ui/static/index.html')

    @app.get('/api/v1/capabilities', include_in_schema=False)
    def capabilities():
        return dict(stage_factory=config.stage_factory, synthetic=config.stage_factory=='demo', worker='separate-process', exports=['markdown','html'])

    @app.get('/api/v1/fixtures/{name}', include_in_schema=False)
    def fixture(name: str):
        if name not in {'success','needs_input','conflicting_sources','unsupported_promise','search_error','stale_result'}:
            raise HTTPException(404)
        return json.loads((root/'contracts/examples'/(name+'.json')).read_text())

    @app.get('/api/v1/runs/{run_id}/usage-summary', include_in_schema=False)
    def usage_summary(request: Request, run_id: str):
        return aggregate_usage(request.app.state.storage.list_usage(run_id), run_id)
