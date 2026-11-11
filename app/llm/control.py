"""Worker-scoped validated model response cache. Standalone probes remain uncached."""
from functools import wraps

from app.execution import before_external_action, current_policy
from app.llm.accounting import call_context
from app.llm.base import ModelError, ModelResult, validate_request
from app.storage.database import new_id, now


def maximum_call_cost(settings):
    amount = settings.model_call_cost_reserve
    return dict(amount=format(amount, 'f'), currency=settings.model_cost_currency) if amount is not None else None


def controlled_model(generate):
    @wraps(generate)
    def wrapped(self, *, messages, schema, run_id, stage, prompt_version=None, schema_version=None):
        policy = current_policy(run_id)
        args = dict(messages=messages, schema=schema, run_id=run_id, stage=stage,
                    prompt_version=prompt_version, schema_version=schema_version)
        if policy is None:
            if getattr(self.sink, 'requires_execution_control', False):
                raise ModelError('INVALID_STATE', 'Persistent workflow calls require the active worker execution context')
            return generate(self, **args)
        before_external_action(run_id)
        validator = validate_request(messages, schema, run_id, stage)
        parameters = dict(timeout_seconds=self.settings.model_timeout_seconds,
                          max_tokens=self.settings.model_max_tokens if self.settings.model_provider == 'openrouter' else None,
                          max_retries=self.settings.model_max_retries if self.settings.model_provider == 'openrouter' else 0)
        context = call_context(messages, schema, prompt_version=prompt_version, schema_version=schema_version,
                               model_requested=self.settings.model_name, parameters=parameters)
        key = policy.cache_key(dict(kind='model', provider=self.settings.model_provider, context=context))
        # An unknown default model cannot form an explicit model-version cache key.
        cached = policy.cached(key) if self.settings.model_name else None
        if cached is not None:
            try:
                validator.validate(cached['data'])
                source = next(u for u in policy.storage.list_usage(run_id) if u['id'] == cached['source_usage_id'])
                if (source['outcome'] != 'success' or source['cached'] or source['model'] != cached['model']
                        or source['provider'] != self.settings.model_provider or source['stage'] != stage or source['tool'] is not None):
                    raise ValueError('Invalid cache provenance')
            except Exception:
                policy.invalidate_cache(key)
            else:
                currency = policy.limits['budget']['currency'] if policy.limits['budget'] else (source['cost'] or {}).get('currency', 'USD')
                event = dict(id=new_id('usage'), run_id=run_id, call_id=new_id('call'), attempt=1, stage=stage,
                             provider=source['provider'], model=cached['model'], tool=None, started_at=now(), duration_ms=0,
                             input_tokens=0, output_tokens=0, cost=dict(amount='0', currency=currency), cost_basis='reported',
                             pricing_version=None, outcome='success', cached=True, context=context, generation_id=None,
                             cache_origin_id=source['id'])
                policy.storage.record(event)
                return ModelResult(cached['data'], cached['model'], None, [event['id']])
        result = generate(self, **args)
        if self.settings.model_name:
            policy.save_cache(key, dict(data=result.data, model=result.model, source_usage_id=result.usage_ids[-1]))
        return result
    return wrapped
