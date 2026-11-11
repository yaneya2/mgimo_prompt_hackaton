"""Non-streaming OpenRouter adapter. Public errors never include remote bodies."""
import time
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from uuid import uuid4

import httpx
from jsonschema.exceptions import ValidationError

from app.config import Settings
from app.execution import before_external_action
from app.llm.usage import UsageSink

from app.llm.base import ModelError, ModelResult, strict_json, validate_request
from app.llm.accounting import UsageRecorder, call_context, pricing_from_settings

def status_error(status):
    codes = {400: ("INVALID_REQUEST", "Model or schema parameters are unsupported"),
             401: ("AUTHENTICATION_ERROR", "OpenRouter key is invalid"),
             402: ("INSUFFICIENT_CREDITS", "OpenRouter credits are insufficient"),
             403: ("ACCESS_DENIED", "OpenRouter denied this request"),
             404: ("MODEL_NOT_FOUND", "Requested model or endpoint is unavailable"),
             408: ("TIMEOUT", "Provider timed out"),
             422: ("INVALID_REQUEST", "OpenRouter rejected request parameters"),
             429: ("RATE_LIMITED", "OpenRouter rate limit reached")}
    code, message = codes.get(status, ("PROVIDER_ERROR", "OpenRouter request failed"))
    return ModelError(code, message, retryable=status in {429, 502, 503, 504})


def retry_delay(header, attempt):
    if not header:
        return min(2 ** (attempt - 1), 10)
    try:
        seconds = float(header)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(header) - datetime.now(timezone.utc)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    # Do not retry earlier than the provider requested or block indefinitely.
    return max(0, seconds) if 0 <= seconds <= 10 else None


def usage_event(body, *, run_id, stage, model, attempt, started_at, duration_ms, outcome):
    usage = body.get("usage") if isinstance(body, dict) else None
    usage = usage if isinstance(usage, dict) else {}
    def count(name):
        value = usage.get(name)
        return value if type(value) is int and value >= 0 else None
    cost, basis = None, "unknown"
    try:
        raw = Decimal(str(usage.get("cost")))
        if raw.is_finite() and 0 <= raw <= Decimal("1000000"):
            rounded = raw.quantize(Decimal("0.000001"), rounding=ROUND_HALF_UP)
            cost = {"amount": format(rounded, "f"), "currency": "USD"}
            basis = "reported" if raw == rounded else "estimated"
    except (InvalidOperation, ValueError, TypeError):
        pass
    return dict(id="usage_" + uuid4().hex, run_id=run_id, call_id="call_" + uuid4().hex,
                attempt=attempt, stage=stage, provider="openrouter", model=model, tool=None,
                started_at=started_at, duration_ms=duration_ms, input_tokens=count("prompt_tokens"),
                output_tokens=count("completion_tokens"), cost=cost, cost_basis=basis,
                pricing_version=None, outcome=outcome, cached=False)


class OpenRouterAdapter:
    def __init__(self, settings: Settings, sink: UsageSink, *, client=None, sleep=time.sleep):
        if settings.model_provider != "openrouter" or not settings.model_name or not settings.model_api_key:
            raise ModelError("CONFIGURATION_ERROR", "Set APP_MODEL_PROVIDER=openrouter, APP_MODEL_NAME and APP_MODEL_API_KEY")
        self.settings = settings
        self.sink = sink
        self.sleep = sleep
        self._owned = client is None
        try:
            self.recorder = UsageRecorder(sink, pricing_from_settings(settings))
        except ValueError as error:
            raise ModelError("CONFIGURATION_ERROR", str(error)) from None
        self.client = client if client is not None else httpx.Client(follow_redirects=False)

    def close(self):
        if self._owned:
            self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def generate(self, *, messages: list[dict], schema: dict, run_id: str, stage: str,
                 prompt_version: str | None = None, schema_version: str | None = None) -> ModelResult:
        validator = validate_request(messages, schema, run_id, stage)
        try:
            context = call_context(messages, schema, prompt_version=prompt_version, schema_version=schema_version,
                                   model_requested=self.settings.model_name,
                                   parameters={"timeout_seconds": self.settings.model_timeout_seconds,
                                               "max_tokens": self.settings.model_max_tokens,
                                               "max_retries": self.settings.model_max_retries})
        except (ValueError, TypeError) as error:
            raise ModelError("INVALID_REQUEST", "Cannot encode call metadata or version labels") from None
        payload = {"model": self.settings.model_name, "messages": messages, "stream": False,
                   "max_tokens": self.settings.model_max_tokens,
                   "provider": {"require_parameters": True, "allow_fallbacks": False},
                   "response_format": {"type": "json_schema", "json_schema": {"name": "agent_result", "strict": True, "schema": schema}}}
        ids = []
        for attempt in range(1, self.settings.model_max_retries + 2):
            before_external_action(run_id)
            start = time.monotonic()
            stamp = datetime.now(timezone.utc).isoformat()
            body, error, result, response = {}, None, None, None
            try:
                response = self.client.post("https://openrouter.ai/api/v1/chat/completions",
                    headers={"Authorization": "Bearer " + self.settings.model_api_key.get_secret_value()},
                    json=payload, timeout=self.settings.model_timeout_seconds)
                try:
                    body = strict_json(response.text)
                except (ValueError, TypeError):
                    body = {}
                if response.status_code != 200:
                    error = status_error(response.status_code)
                elif not isinstance(body, dict):
                    error = ModelError("INVALID_RESPONSE", "Provider returned an invalid envelope")
                elif body.get("error"):
                    embedded = body["error"]
                    code = embedded.get("code") if isinstance(embedded, dict) else None
                    error = status_error(code if isinstance(code, int) else 500)
                else:
                    choice = body["choices"][0]
                    message = choice["message"]
                    if message.get("refusal") or choice.get("finish_reason") == "content_filter":
                        error = ModelError("MODEL_REFUSAL", "Model declined the request")
                    elif choice.get("finish_reason") != "stop":
                        error = ModelError("INCOMPLETE_RESPONSE", "Model did not complete the answer")
                    else:
                        result = strict_json(message["content"])
                        validator.validate(result)
            except httpx.TimeoutException:
                error = ModelError("TIMEOUT", "OpenRouter request timed out; billing status may be unknown")
            except httpx.RequestError:
                error = ModelError("NETWORK_ERROR", "OpenRouter could not be reached")
            except (KeyError, IndexError, AttributeError, TypeError, ValueError, RecursionError, ValidationError):
                error = ModelError("INVALID_RESPONSE", "Model output does not match the requested JSON schema")
            model = body.get("model") if isinstance(body, dict) else None
            model = model if isinstance(model, str) and model else self.settings.model_name
            event = usage_event(body, run_id=run_id, stage=stage, model=model, attempt=attempt,
                                started_at=stamp, duration_ms=max(0, int((time.monotonic()-start)*1000)),
                                outcome="error" if error else "success")
            ids.append(event["id"])
            generation = body.get("id") if isinstance(body, dict) else None
            generation = generation if isinstance(generation, str) and generation else None
            try:
                self.recorder.record_model(event, context, generation)
            except OSError:
                raise ModelError("USAGE_RECORDING_ERROR", "Call completed but usage could not be saved; do not retry automatically", usage_ids=ids) from None
            if error is None:
                return ModelResult(result, model, generation, ids)
            error.usage_ids = ids.copy()
            delay = retry_delay(response.headers.get("Retry-After"), attempt) if response is not None else None
            if not error.retryable or attempt > self.settings.model_max_retries or delay is None:
                raise error from None
            before_external_action(run_id)
            self.sleep(delay)
        raise AssertionError("Unreachable")
