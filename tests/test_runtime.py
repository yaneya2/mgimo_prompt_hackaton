"""A8 acceptance: real persistence, mocked providers, no paid/external requests."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import select
from pathlib import Path
import subprocess
import sys
import time

from fastapi.testclient import TestClient
import httpx
import pytest

from app.config import Settings
from app.llm import ModelError, OpenRouterAdapter
from app.main import create_app
from app.storage import Storage
from app.storage.runtime import LimitReached, RunPolicy
from app.storage.validation import validate
from app.tools.control import ToolFailure
from app.workflow import Orchestrator
from app.workflow.demo import build_stages, demo_stage
from scripts.try_workflow import demo_brief, LIMITS
from test_api import get, post, tick

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = dict(type='object', properties={'answer': {'type': 'string'}}, required=['answer'], additionalProperties=False)


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as value:
        yield value


def start(client, **limits):
    brief = demo_brief('Private product data')
    brief['budget'] = dict(amount='50000', currency='RUB')
    project = post(client, '/projects', dict(name='A8', brief=brief), status=201)
    run = post(client, '/projects/' + project['id'] + '/runs', dict(brief_version=1, limits=dict(LIMITS, **limits)))
    return project, run


def registry(handler):
    value = build_stages()
    return replace(value, stages=dict(value.stages, brief=replace(value.stages['brief'], handler=handler)))


def settings(**changes):
    return Settings(_env_file=None, model_provider='openrouter', model_name='test/model', model_api_key='private-key', **changes)


def model_response(status=200, cost='0.1'):
    usage = {} if cost is None else dict(cost=cost, prompt_tokens=10, completion_tokens=2)
    return httpx.Response(status, json=dict(id='gen_synthetic', model='test/model', usage=usage,
        choices=[dict(finish_reason='stop', message=dict(content='{"answer":"synthetic"}'))]))


def invoke(client, request, handler, *, config=None, text='private-input', prompt='p1', schema_version='s1'):
    with httpx.Client(transport=httpx.MockTransport(handler)) as transport:
        with OpenRouterAdapter(config or settings(), client.app.state.storage, client=transport, sleep=lambda _: None) as model:
            return model.generate(messages=[dict(role='user', content=text)], schema=SCHEMA,
                run_id=request['run_id'], stage=request['stage'], prompt_version=prompt, schema_version=schema_version)


def report(client, rid):
    value = get(client, '/runs/' + rid + '/summary')
    validate('RunSummary', value)
    for event in get(client, '/runs/' + rid + '/events'):
        validate('RunEvent', event)
    return value


def test_cache_hit_uses_no_quota_then_changed_input_hits_model_limit(client):
    _, run = start(client, max_model_calls=1)
    calls = []
    def transport(request):
        calls.append(request)
        return model_response()
    def brief(request, context):
        assert invoke(client, request, transport).data == invoke(client, request, transport).data
        invoke(client, request, transport, text='different input')
        pytest.fail('Quota must stop the third request')
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert summary['status'] == 'incomplete' and summary['stop_reason']['code'] == 'MODEL_CALL_LIMIT'
    assert len(calls) == summary['model_calls'] == 1
    assert summary['usage']['totals']['cached_events'] == 1
    assert summary['usage']['totals']['cost_by_currency']['USD']['known_total'] == '0.1'
    safe = json.dumps(summary) + json.dumps(get(client, '/runs/' + run['id'] + '/events'))
    assert 'private-input' not in safe and 'private-key' not in safe


def test_cache_persists_across_pause_and_new_storage_instance(client):
    _, run = start(client, max_model_calls=1)
    calls, first = [], [True]
    def transport(request):
        calls.append(request)
        return model_response()
    def brief(request, context):
        invoke(client, request, transport)
        if first[0]:
            first[0] = False
            active = get(client, '/runs/' + run['id'])
            post(client, '/runs/' + run['id'] + '/pause', dict(expected_revision=active['revision']))
            context.check_pause()
        return demo_stage(request, context)
    stages = registry(brief)
    tick(client, stages)
    paused = get(client, '/runs/' + run['id'])
    assert paused['status'] == 'paused' and paused['checkpoint_id'] is None
    client.app.state.storage = Storage(client.app.state.settings.data_dir)
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=paused['revision']))
    tick(client, stages)
    summary = report(client, run['id'])
    assert summary['status'] == 'waiting_for_input' and len(calls) == 1
    assert summary['usage']['totals']['cached_events'] == 1


@pytest.mark.parametrize('difference', ['text', 'prompt', 'schema', 'model', 'disabled', 'expired'])
def test_model_cache_is_not_reused_for_changed_key_or_disabled_or_expired(client, difference):
    _, run = start(client, cache_ttl_seconds=0 if difference == 'disabled' else 3600)
    calls = []
    def transport(request):
        calls.append(request)
        response = model_response()
        body = json.loads(response.content)
        body['model'] = json.loads(request.content)['model']
        return httpx.Response(200, json=body)
    def brief(request, context):
        invoke(client, request, transport)
        if difference == 'expired':
            with client.app.state.storage.transaction(write=True) as db:
                db.execute('UPDATE response_cache SET created_at = ?', ((datetime.now(timezone.utc) - timedelta(hours=2)).isoformat(),))
        kwargs = {'text': 'changed'} if difference == 'text' else {'prompt': 'p2'} if difference == 'prompt' else \
                 {'schema_version': 's2'} if difference == 'schema' else {'config': settings(model_max_tokens=1000)} if difference == 'model' else {}
        if difference == 'model':
            kwargs['config'].model_name = 'test/other'
        invoke(client, request, transport, **kwargs)
        return demo_stage(request, context)
    tick(client, registry(brief))
    assert len(calls) == 2 and report(client, run['id'])['usage']['totals']['cached_events'] == 0


@pytest.mark.parametrize('cost,expected_remaining,estimated', [('0.6','0.4',False),(None,'0.4',True)])
def test_retries_reserve_budget_and_paid_or_unknown_error_is_not_free(client, cost, expected_remaining, estimated):
    _, run = start(client, budget=dict(amount='1', currency='USD'))
    calls = []
    def transport(request):
        calls.append(request)
        return model_response(503, cost)
    def brief(request, context):
        invoke(client, request, transport, config=settings(model_call_cost_reserve='0.6'))
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert len(calls) == 1 and summary['stop_reason']['code'] == 'BUDGET_LIMIT'
    assert summary['budget']['remaining_estimate']['amount'] == expected_remaining
    assert summary['budget']['estimated'] == estimated
    assert summary['usage']['totals']['errors'] == 1
    assert summary['usage']['totals']['unknown_cost_events'] == (1 if cost is None else 0)


def test_budget_without_cost_reserve_does_not_call_provider(client):
    _, run = start(client, budget=dict(amount='1', currency='USD'))
    def brief(request, context):
        invoke(client, request, lambda _: pytest.fail('No cost reserve'))
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert summary['stop_reason']['code'] == 'COST_RESERVE_REQUIRED' and summary['model_calls'] == 0


def test_reported_cost_above_reserve_is_visible_and_stops_run(client):
    _, run = start(client, budget=dict(amount='1', currency='USD'))
    def brief(request, context):
        invoke(client, request, lambda _: model_response(cost='1.2'), config=settings(model_call_cost_reserve='0.5'))
        return demo_stage(request, context)
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert summary['status'] == 'incomplete' and summary['stop_reason']['code'] == 'BUDGET_LIMIT'
    assert summary['budget']['allocated']['amount'] == '1.2' and summary['budget']['remaining_estimate']['amount'] == '0'


@pytest.mark.parametrize('response_kind', ['success', 'timeout'])
def test_remaining_time_is_passed_to_adapter_and_timeout_survives_resume(client, response_kind):
    _, run = start(client, timeout_seconds=1)
    calls = []
    def transport(request):
        calls.append(request)
        assert 0 < request.extensions['timeout']['read'] <= 1
        time.sleep(1.05)
        if response_kind == 'timeout':
            raise httpx.ReadTimeout('private transport timeout')
        return model_response()
    def brief(request, context):
        invoke(client, request, transport)
        return demo_stage(request, context)
    stages = registry(brief)
    tick(client, stages)
    summary = report(client, run['id'])
    assert summary['stop_reason']['code'] == 'ACTIVE_TIME_LIMIT' and summary['remaining_seconds'] == 0
    event = get(client, '/runs/' + run['id'] + '/usage')[0]
    assert event['context']['parameters']['timeout_seconds'] == calls[0].extensions['timeout']['read']
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=summary['run_revision']))
    tick(client, stages)
    assert len(calls) == 1 and report(client, run['id'])['stop_reason']['code'] == 'ACTIVE_TIME_LIMIT'


def test_step_limit_stops_next_handler_and_is_not_reset_by_revision(client):
    project, run = start(client, max_steps=2)
    called = []
    def handler(request, context):
        called.append(request['stage'])
        return demo_stage(request, context)
    base = build_stages()
    stages = replace(base, stages={key: replace(spec, handler=handler) for key, spec in base.stages.items()})
    tick(client, stages)
    summary = report(client, run['id'])
    assert called == ['brief','research'] and summary['stop_reason']['code'] == 'STEP_LIMIT'
    brief = get(client, '/projects/' + project['id'] + '/brief')['input']
    post(client, '/projects/' + project['id'] + '/brief-versions', dict(expected_brief_version=1, brief=brief), status=201)
    paused = get(client, '/runs/' + run['id'])
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=paused['revision']))
    tick(client, stages)
    assert called == ['brief','research'] and report(client, run['id'])['stop_reason']['code'] == 'STEP_LIMIT'


def test_tool_cache_and_retry_use_same_search_quota(client, monkeypatch):
    monkeypatch.setattr('app.tools.control.time.sleep', lambda _: None)
    _, run = start(client, max_search_calls=2)
    calls = []
    def transport(timeout):
        assert 0 < timeout <= 30
        calls.append(timeout)
        if len(calls) == 1:
            raise ToolFailure('UNAVAILABLE')
        return {'sources': ['synthetic']}
    def brief(request, context):
        kwargs = dict(name='search', provider='synthetic', arguments={'query':'private query'}, invoke=transport,
                      max_retries=1, cache_ttl_seconds=60)
        assert context.tool_call(**kwargs).data == context.tool_call(**kwargs).data
        context.tool_call(**dict(kwargs, arguments={'query':'another query'}))
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert len(calls) == summary['search_calls'] == 2
    assert summary['stop_reason']['code'] == 'SEARCH_CALL_LIMIT'
    assert summary['usage']['totals']['retries'] == summary['usage']['totals']['cached_events'] == 1


def test_invalid_tool_response_is_not_retried_or_leaked(client):
    _, run = start(client)
    calls = []
    def transport(timeout):
        calls.append(timeout)
        return ['private response']
    def brief(request, context):
        context.tool_call(name='search', provider='synthetic', arguments={}, invoke=transport, max_retries=3)
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert summary['status'] == 'failed' and len(calls) == 1
    assert 'private response' not in json.dumps(get(client, '/runs/' + run['id']))


def test_v3_migration_preserves_history_and_adds_runtime_tables(tmp_path):
    store = Storage(tmp_path)
    project = store.create_project('Migration', demo_brief())
    run = store.create_run(project['id'], LIMITS)
    with store.transaction(write=True) as db:
        for table in ('external_calls', 'response_cache', 'run_events'):
            db.execute('DROP TABLE ' + table)
        db.execute('PRAGMA user_version=3')
    restored = Storage(tmp_path)
    assert restored.get_run(run['id']) == run
    assert restored.run_summary(run['id'])['model_calls'] == 0
    with restored.transaction() as db:
        assert db.execute('PRAGMA user_version').fetchone()[0] == 4


def test_reservation_survives_sigkill_and_does_not_allow_another_attempt(client):
    _, run = start(client, max_model_calls=1, budget=dict(amount='1', currency='USD'))
    code = '''
import sys, time
from app.storage import Storage
from app.storage.runtime import RunPolicy
from app.workflow import Orchestrator
from app.workflow.demo import build_stages
store = Storage(sys.argv[1])
with store.worker() as worker:
    run = worker.claim_run(sys.argv[2])
    request, spec = Orchestrator(store, build_stages())._request(run)
    job = worker.begin_step(run['id'], expected_revision=run['revision'])
    policy = RunPolicy(store, request, job, dict(feedback=[], repairs_used=0))
    policy.reserve('model', 30, dict(amount='0.6', currency='USD'))
    print('reserved', flush=True)
    time.sleep(60)
'''
    process = subprocess.Popen([sys.executable, '-E', '-c', code, str(client.app.state.settings.data_dir), run['id']],
                               cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        assert select.select([process.stdout], [], [], 10)[0], 'Child did not reserve a call'
        assert process.stdout.readline().strip() == 'reserved'
        process.kill()
        process.wait(timeout=10)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=10)
    storage = Storage(client.app.state.settings.data_dir)
    storage.recover_if_idle()
    summary = storage.run_summary(run['id'])
    assert summary['status'] == 'paused' and summary['model_calls'] == 1
    assert summary['budget']['unrecorded_attempts'] == 1
    assert summary['accounting_complete'] is False
    assert summary['budget']['remaining_estimate']['amount'] == '0.4' and summary['budget']['estimated']
    assert summary['usage']['totals']['events'] == 0
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=summary['run_revision']))
    def brief(request, context):
        invoke(client, request, lambda _: pytest.fail('Reserved attempt must still consume quota'),
               config=settings(model_call_cost_reserve='0.1'))
    tick(client, registry(brief))
    assert report(client, run['id'])['stop_reason']['code'] == 'MODEL_CALL_LIMIT'


def test_concurrent_reservations_cannot_oversubscribe_quota(client):
    _, run = start(client, max_model_calls=1)
    storage = client.app.state.storage
    with storage.worker() as worker:
        active = worker.claim_run(run['id'])
        request, _ = Orchestrator(storage, build_stages())._request(active)
        job = worker.begin_step(run['id'], expected_revision=active['revision'])
        policy = RunPolicy(storage, request, job, dict(feedback=[], repairs_used=0))
        def reserve(_):
            try:
                return policy.reserve('model', 30)['call_id']
            except LimitReached as error:
                return error.code
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(reserve, range(2)))
    assert sum(r.startswith('call_') for r in results) == 1 and 'MODEL_CALL_LIMIT' in results
    assert report(client, run['id'])['model_calls'] == 1


def test_corrupt_cache_is_discarded_before_next_call(client):
    _, run = start(client)
    calls = []
    def transport(request):
        calls.append(request)
        return model_response()
    def brief(request, context):
        invoke(client, request, transport)
        with client.app.state.storage.transaction(write=True) as db:
            db.execute('UPDATE response_cache SET payload = ?', ('{"private":"broken-cache"}',))
        assert invoke(client, request, transport).data == {'answer':'synthetic'}
        return demo_stage(request, context)
    tick(client, registry(brief))
    assert len(calls) == 2
    events = get(client, '/runs/' + run['id'] + '/events')
    assert any(e['kind'] == 'cache_invalidated' for e in events) and 'broken-cache' not in json.dumps(events)


def test_unknown_legacy_cost_blocks_budget_and_mixed_currency_is_not_converted(client):
    from app.llm.accounting import UsageRecorder
    _, run = start(client, budget=dict(amount='1', currency='USD'))
    UsageRecorder(client.app.state.storage).record_tool(run_id=run['id'], stage='brief', provider='synthetic',
                                                       tool='legacy', duration_ms=1)
    def brief(request, context):
        pytest.fail('Unknown previous spending must stop the next stage')
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert summary['stop_reason']['code'] == 'UNKNOWN_PREVIOUS_COST' and summary['budget']['remaining_estimate'] is None
    _, other = start(client, budget=dict(amount='1', currency='USD'))
    UsageRecorder(client.app.state.storage).record_tool(run_id=other['id'], stage='brief', provider='synthetic',
        tool='legacy', duration_ms=1, cost=dict(amount='1', currency='EUR'))
    tick(client, registry(brief))
    summary = report(client, other['id'])
    assert summary['stop_reason']['code'] == 'CURRENCY_MISMATCH' and summary['budget']['remaining_estimate'] is None


def test_paid_tool_error_is_accounted_before_budget_blocks_retry(client, monkeypatch):
    monkeypatch.setattr('app.tools.control.time.sleep', lambda _: None)
    _, run = start(client, budget=dict(amount='1', currency='USD'))
    calls = []
    def transport(timeout):
        calls.append(timeout)
        raise ToolFailure('UNAVAILABLE', cost=dict(amount='0.6', currency='USD'))
    def brief(request, context):
        context.tool_call(name='search', provider='synthetic', arguments={}, invoke=transport,
                          maximum_cost=dict(amount='0.6', currency='USD'), max_retries=2)
    tick(client, registry(brief))
    summary = report(client, run['id'])
    assert len(calls) == 1 and summary['stop_reason']['code'] == 'BUDGET_LIMIT'
    assert summary['usage']['totals']['cost_by_currency']['USD']['known_total'] == '0.6'
    assert summary['budget']['remaining_estimate']['amount'] == '0.4'


def test_human_wait_does_not_consume_active_time(client):
    _, run = start(client)
    tick(client)
    first = report(client, run['id'])
    assert first['status'] == 'waiting_for_input'
    time.sleep(0.1)
    second = report(client, run['id'])
    assert first['active_seconds'] == second['active_seconds'] and first['remaining_seconds'] == second['remaining_seconds']


def test_durable_adapter_without_worker_context_cannot_bypass_reservations(client):
    _, run = start(client)
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail('Uncontrolled call'))) as transport:
        with OpenRouterAdapter(settings(), client.app.state.storage, client=transport) as model:
            with pytest.raises(ModelError, match='execution context'):
                model.generate(messages=[dict(role='user', content='private')], schema=SCHEMA, run_id=run['id'], stage='brief')
    assert report(client, run['id'])['model_calls'] == 0


def test_new_brief_invalidates_model_response_cache_even_with_identical_prompt(client):
    project, run = start(client)
    calls = []
    def transport(request):
        calls.append(request)
        return model_response()
    def brief(request, context):
        invoke(client, request, transport)
        return demo_stage(request, context)
    stages = registry(brief)
    tick(client, stages)
    current = get(client, '/projects/' + project['id'] + '/brief')['input']
    current['budget']['amount'] = '12000'
    post(client, '/projects/' + project['id'] + '/brief-versions', dict(expected_brief_version=1, brief=current), status=201)
    paused = get(client, '/runs/' + run['id'])
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=paused['revision']))
    tick(client, stages)
    assert len(calls) == 2 and report(client, run['id'])['usage']['totals']['cached_events'] == 0
