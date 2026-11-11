"""A1 HTTP operations. JSON Schema remains the single contract definition."""
from copy import deepcopy
import json
from pathlib import Path
import re
import sqlite3

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response
from starlette.concurrency import run_in_threadpool

from app.llm.base import strict_json
from app.storage.commands import Commands
from app.storage.database import new_id
from app.storage.validation import Conflict, NotFound, StorageError, validator

ROOT = Path(__file__).resolve().parents[2]
CONTRACT = json.loads((ROOT / "contracts/openapi.json").read_text())
DEFINITIONS = json.loads((ROOT / "contracts/schema.json").read_text())["$defs"]
IDENTITY = re.compile(r"^[a-z][a-z0-9_-]{1,127}$")
router = APIRouter(prefix="/api/v1")


class APIError(Exception):
    def __init__(self, status, code, message, *, retryable=False, fields=None):
        self.status, self.code, self.message = status, code, message
        self.retryable, self.fields = retryable, fields or []


def error_response(request, error):
    run_id = request.path_params.get("run_id")
    if not run_id or not IDENTITY.fullmatch(run_id):
        run_id = None
    body = dict(error=dict(code=error.code, message=error.message, retryable=error.retryable,
                           request_id=new_id("request"), run_id=run_id, field_errors=error.fields))
    return JSONResponse(body, status_code=error.status)


async def http_error(request, error):
    code = "NOT_FOUND" if error.status_code == 404 else "VALIDATION_ERROR"
    return error_response(request, APIError(error.status_code, code, "Маршрут или метод запроса недоступен."))


def dispatch(request, operation, body):
    storage = request.app.state.storage
    commands = Commands(storage, request.app.state.renderer)
    project_id, run_id = request.path_params.get("project_id"), request.path_params.get("run_id")
    actions = {
        "createProject": lambda: storage.create_project(body["name"], body["brief"]),
        "listProjects": storage.list_projects,
        "getCurrentBrief": lambda: storage.get_brief(project_id),
        "reviseBrief": lambda: commands.revise_brief(project_id, body),
        "createRun": lambda: storage.create_run(project_id, body["limits"], brief_version=body["brief_version"]),
        "listRuns": lambda: storage.list_runs(project_id),
        "getRun": lambda: storage.get_run(run_id),
        "getSnapshot": lambda: storage.get_snapshot(run_id),
        "listArtifacts": lambda: storage.get_snapshot(run_id)["artifacts"],
        "listUsage": lambda: storage.list_usage(run_id),
        "pauseRun": lambda: commands.pause(run_id, body),
        "resumeRun": lambda: commands.resume(run_id, body),
        "answersRun": lambda: storage.submit_answers(run_id, body["answers"], expected_revision=body["expected_revision"]),
        "selectionRun": lambda: storage.submit_selection(run_id, body["option_id"], body["option_version"], body["rationale"],
                                                        expected_revision=body["expected_revision"]),
        "exclude_sourceRun": lambda: commands.exclude_source(run_id, body),
        "createExport": lambda: commands.create_export(project_id, body),
        "downloadExport": lambda: commands.download_export(request.path_params["export_id"]),
    }
    if request.method == "POST":
        scope = operation + ":" + json.dumps(request.path_params, sort_keys=True)
        return storage.command(scope, request.headers["Idempotency-Key"], body, actions[operation])
    return actions[operation]()


def endpoint(spec, method):
    operation = spec["operationId"]
    success_status = int(next(status for status in spec["responses"] if status.startswith("2")))
    schema_name = (spec["requestBody"]["content"]["application/json"]["schema"]["$ref"].split("/")[-1]
                   if method == "post" else None)

    async def handle(request: Request):
        try:
            for name, value in request.path_params.items():
                if not IDENTITY.fullmatch(value):
                    raise APIError(422, "VALIDATION_ERROR", "Неверный идентификатор.",
                                   fields=[dict(path="/path/" + name, message="Неверный формат ID.")])
            body = None
            if schema_name:
                key = request.headers.get("Idempotency-Key", "")
                if not key.strip():
                    raise APIError(422, "VALIDATION_ERROR", "Требуется Idempotency-Key.")
                if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                    raise APIError(422, "VALIDATION_ERROR", "Требуется Content-Type: application/json.")
                raw = bytearray()
                async for chunk in request.stream():
                    raw.extend(chunk)
                    if len(raw) > request.app.state.settings.api_max_request_bytes:
                        raise APIError(422, "VALIDATION_ERROR", "Тело запроса превышает допустимый размер.")
                try:
                    body = strict_json(raw.decode("utf-8"))
                except (UnicodeError, ValueError, RecursionError):
                    raise APIError(422, "VALIDATION_ERROR", "Требуется корректный JSON без повторных ключей и бесконечных чисел.") from None
                errors = []
                for error in validator(schema_name).iter_errors(body):
                    # Do not interpolate jsonschema.message: it may include private input values.
                    errors.append(dict(path="/body" + "".join("/" + str(p) for p in error.absolute_path),
                                       message="Поле не соответствует контракту запроса."))
                    if len(errors) == 10:
                        break
                if errors:
                    raise APIError(422, "VALIDATION_ERROR", "Проверьте поля запроса.", fields=errors)
            result = await run_in_threadpool(dispatch, request, operation, body)
            if operation == "downloadExport":
                metadata, content = result
                extension = "md" if metadata["format"] == "markdown" else "html"
                return Response(content, media_type="text/markdown" if extension == "md" else "text/html", headers={
                    "Content-Disposition": f'attachment; filename="{metadata["id"]}.{extension}"',
                    "X-Content-Type-Options": "nosniff", "Content-Security-Policy": "sandbox; default-src 'none'"})
            return JSONResponse(result, status_code=success_status)
        except APIError as error:
            return error_response(request, error)
        except NotFound:
            return error_response(request, APIError(404, "NOT_FOUND", "Запрошенный объект не найден."))
        except Conflict as error:
            messages = {"VERSION_CONFLICT": "Конфликт версии или ключа повтора. Обновите состояние и проверьте команду.",
                        "RUN_ALREADY_ACTIVE": "У проекта уже есть незавершённый запуск.",
                        "INVALID_STATE": "Команда недоступна в текущем состоянии. Проверьте статус и ожидаемый ввод."}
            return error_response(request, APIError(409, error.code, messages[error.code]))
        except StorageError:
            return error_response(request, APIError(422 if schema_name else 500,
                                  "VALIDATION_ERROR" if schema_name else "INTERNAL_ERROR", "Не удалось проверить данные операции."))
        except (sqlite3.Error, OSError):
            return error_response(request, APIError(503, "UPSTREAM_UNAVAILABLE", "Хранилище временно недоступно. Повторите запрос с тем же ключом.", retryable=True))
        except Exception:
            return error_response(request, APIError(500, "INTERNAL_ERROR", "Операция не выполнена из-за внутренней ошибки."))

    return handle


for path, operations in CONTRACT["paths"].items():
    for method, spec in operations.items():
        router.add_api_route(path, endpoint(spec, method), methods=[method.upper()], name=spec["operationId"], operation_id=spec["operationId"])


def openapi_document(health_document):
    """Inline the bundled schemas so Swagger needs no external file resolution."""
    document = deepcopy(CONTRACT)
    document["info"] = dict(title="Strategy API", version="1.0.0", description="Local platform API; agents execute in the separate worker.")
    document["servers"] = [{"url": "/"}]
    document["paths"] = {"/api/v1" + path: spec for path, spec in document["paths"].items()}
    document["paths"]["/health"] = health_document["paths"]["/health"]
    document["components"] = {"schemas": deepcopy({**DEFINITIONS, **health_document.get("components", {}).get("schemas", {})})}
    def replace_refs(value):
        if isinstance(value, dict):
            if "$ref" in value:
                value["$ref"] = value["$ref"].replace("./schema.json#/$defs/", "#/components/schemas/").replace("#/$defs/", "#/components/schemas/")
            for item in value.values():
                replace_refs(item)
        elif isinstance(value, list):
            for item in value:
                replace_refs(item)
    replace_refs(document)
    return document
