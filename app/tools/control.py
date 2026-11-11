"""One accounting and reservation owner for a synchronous external tool attempt."""
from dataclasses import dataclass
import json
import math
import time

from jsonschema import Draft202012Validator, FormatChecker

from app.execution import PauseRequested, before_external_action, current_policy, reserve_external
from app.llm.accounting import UsageRecorder
from app.llm.base import check_local_refs
from app.storage.database import encode, new_id, now
from app.storage.runtime import LimitReached


class ToolFailure(Exception):
    MESSAGES = dict(RATE_LIMITED='Инструмент ограничил частоту запросов.', UNAVAILABLE='Инструмент временно недоступен.',
                    TIMEOUT='Истекло время ожидания инструмента.', INVALID_RESPONSE='Ответ инструмента не прошёл проверку.',
                    TOOL_FAILED='Инструмент не выполнил операцию.')

    def __init__(self, code, *, cost=None, cost_basis=None):
        self.code = code
        self.retryable = code in ('RATE_LIMITED', 'UNAVAILABLE')
        self.cost, self.cost_basis = cost, cost_basis or ('reported' if cost is not None else 'unknown')
        super().__init__(self.MESSAGES[code])


@dataclass(frozen=True)
class ToolResponse:
    """Transport payload with cost actually returned by the service, when available."""
    data: dict
    cost: dict | None = None
    cost_basis: str = 'reported'


@dataclass(frozen=True)
class ToolResult:
    data: dict
    usage_ids: list[str]


def tool_call(*, name, provider, arguments, invoke, schema=None, version='v1', timeout_seconds=30,
              maximum_cost=None, max_retries=0, cache_ttl_seconds=0):
    """invoke(timeout_seconds) performs one attempt and returns plain JSON, without recording Usage.

    Cost is unknown unless reported independently; maximum_cost is an operator-supplied reserve,
    not an invented tariff. Only explicit transient ToolFailure codes are retried.
    """
    policy = current_policy()
    if policy is None:
        raise RuntimeError('Controlled tools require an active worker')
    if (type(max_retries) is not int or not 0 <= max_retries <= 3
            or type(cache_ttl_seconds) is not int or not 0 <= cache_ttl_seconds <= 86400
            or not isinstance(timeout_seconds, (int, float)) or not math.isfinite(timeout_seconds) or timeout_seconds <= 0
            or not isinstance(name, str) or not name.strip() or not isinstance(provider, str) or not provider.strip()
            or not isinstance(version, str) or not version.strip()):
        raise ValueError('Invalid tool limits')
    schema = {'type': 'object'} if schema is None else schema
    Draft202012Validator.check_schema(schema)
    check_local_refs(schema)
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    before_external_action()
    key = policy.cache_key(dict(kind='tool', provider=provider, name=name, version=version, arguments=arguments, schema=schema))
    cached = policy.cached(key, cache_ttl_seconds) if cache_ttl_seconds else None
    if cached is not None:
        try:
            validator.validate(cached['data'])
            source = next(u for u in policy.storage.list_usage(policy.run_id) if u['id'] == cached['source_usage_id'])
            if source['cached'] or source['outcome'] != 'success' or source['tool'] != name or source['provider'] != provider:
                raise ValueError('Invalid cache origin')
        except Exception:
            policy.invalidate_cache(key)
        else:
            currency = policy.limits['budget']['currency'] if policy.limits['budget'] else (source['cost'] or {}).get('currency', 'USD')
            event = dict(id=new_id('usage'), run_id=policy.run_id, call_id=new_id('call'), attempt=1,
                         stage=policy.stage, provider=provider, model=None, tool=name, started_at=now(), duration_ms=0,
                         input_tokens=0, output_tokens=0, cost=dict(amount='0', currency=currency), cost_basis='reported',
                         pricing_version=None, outcome='success', cached=True, cache_origin_id=source['id'])
            policy.storage.record(event)
            return ToolResult(cached['data'], [event['id']])
    ids = []
    for attempt in range(1, max_retries + 2):
        permit = reserve_external(policy.run_id, 'search', timeout_seconds, maximum_cost)
        started = time.monotonic()
        failure, result, cost, basis = None, None, None, 'unknown'
        try:
            raw = invoke(permit['timeout_seconds'])
            if isinstance(raw, ToolResponse):
                cost, basis = raw.cost, raw.cost_basis if raw.cost is not None else 'unknown'
                raw = raw.data
            result = json.loads(encode(raw))
            if next(validator.iter_errors(result), None) is not None:
                raise ToolFailure('INVALID_RESPONSE')
        except (PauseRequested, LimitReached) as error:
            failure = error
        except ToolFailure as error:
            failure = error
            if error.cost is not None:
                cost, basis = error.cost, error.cost_basis
        except TimeoutError:
            failure = ToolFailure('TIMEOUT')
        except Exception:
            failure = ToolFailure('TOOL_FAILED')
        usage = UsageRecorder(policy.storage).record_tool(run_id=policy.run_id, stage=policy.stage,
            provider=provider, tool=name, duration_ms=int((time.monotonic() - started) * 1000),
            attempt=attempt, call_id=permit['call_id'], outcome='error' if failure else 'success', cost=cost, cost_basis=basis)
        ids.append(usage['id'])
        if failure is None:
            policy.save_cache(key, dict(data=result, source_usage_id=usage['id']), cache_ttl_seconds)
            return ToolResult(result, ids)
        if not isinstance(failure, ToolFailure) or not failure.retryable or attempt > max_retries:
            raise failure from None
        before_external_action()
        time.sleep(min(2 ** (attempt - 1), 2))
    raise AssertionError('Unreachable')
