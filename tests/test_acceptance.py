"""A9 regressions and disclosure checks through the persisted workflow/API."""
from copy import deepcopy
import json
import sys

import httpx
import pytest
from pydantic import SecretStr

from app.config import Settings
from app.execution import execution_control
from app.llm import CodexAdapter, ModelError, OpenRouterAdapter
from app.storage.runtime import RunPolicy
from app.workflow import Orchestrator
from app.workflow.demo import build_stages, demo_stage
from app.workflow.results import result_for
from test_api import get, post, tick
from test_runtime import client, invoke, model_response, registry, report, settings, start, SCHEMA


def test_brief_revision_preserves_run_repair_budget(client):
    project, run = start(client, max_repair_cycles=1)
    attempts = []

    def invalid(request, context):
        attempts.append(context.repairs_used)
        return {}

    stages = registry(invalid)
    tick(client, stages)
    assert attempts == [0, 1]
    brief = get(client, '/projects/' + project['id'] + '/brief')['input']
    brief['constraints'].append('Уточнённое ограничение')
    post(client, '/projects/' + project['id'] + '/brief-versions',
         dict(expected_brief_version=1, brief=brief), status=201)
    paused = get(client, '/runs/' + run['id'])
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=paused['revision']))
    tick(client, stages)
    assert attempts == [0, 1, 1]  # Re-evaluate the changed brief, with no extra automatic repair.
    assert report(client, run['id'])['stop_reason']['code'] == 'REPAIR_LIMIT_EXCEEDED'
    snapshot = get(client, '/runs/' + run['id'] + '/snapshot')
    assert snapshot['brief']['version'] == 2 and snapshot['run']['status'] == 'incomplete'


@pytest.mark.parametrize('provider', ['openrouter', 'codex'])
def test_wrong_model_stage_is_rejected_before_external_attempt(client, provider):
    _, run = start(client)
    storage = client.app.state.storage
    attempts = []

    def unexpected(*args, **kwargs):
        attempts.append(True)
        raise RuntimeError('The stage must be checked before reaching the transport')

    with storage.worker() as worker:
        active = worker.claim_run(run['id'])
        request, _ = Orchestrator(storage, build_stages())._request(active)
        job = worker.begin_step(run['id'], expected_revision=active['revision'])
        policy = RunPolicy(storage, request, job, dict(feedback=[], repairs_used=0))
        with httpx.Client(transport=httpx.MockTransport(unexpected)) as transport:
            model = (OpenRouterAdapter(settings(), storage, client=transport) if provider == 'openrouter' else
                     CodexAdapter(Settings(_env_file=None, codex_command=sys.executable), storage, runner=unexpected))
            with model, execution_control(run['id'], lambda: None, policy):
                with pytest.raises(ModelError) as error:
                    model.generate(messages=[dict(role='user', content='synthetic')], schema=SCHEMA,
                                   run_id=run['id'], stage='research')
                assert error.value.code == 'INVALID_STATE'
    assert attempts == []
    summary = report(client, run['id'])
    assert summary['model_calls'] == summary['usage']['totals']['events'] == 0


@pytest.mark.parametrize('kind', ['model', 'tool'])
def test_new_human_answer_invalidates_cache_even_with_same_external_arguments(client, kind):
    _, run = start(client)
    calls, requests = [], []

    def external(argument):
        calls.append(True)
        return model_response() if kind == 'model' else dict(answer='synthetic')

    def brief(request, context):
        requests.append(deepcopy(request))
        if kind == 'model':
            invoke(client, request, external)
        else:
            context.tool_call(name='search', provider='synthetic', arguments={'query': 'same'},
                              invoke=external, cache_ttl_seconds=60)
        if not context.inputs:
            return result_for(request, outcome='needs_input', questions=[dict(
                id='question_confirm', stage='brief', text='Уточните ограничения', required=True, options=[])])
        return demo_stage(request, context)

    stages = registry(brief)
    tick(client, stages)
    waiting = get(client, '/runs/' + run['id'])
    assert waiting['status'] == 'waiting_for_input'
    post(client, '/runs/' + run['id'] + '/answers', dict(expected_revision=waiting['revision'],
         answers=[dict(question_id='question_confirm', value='Новое ограничение')]))
    tick(client, stages)
    assert requests[0] == requests[1]  # The answer is in StageContext.inputs, outside StageRequest.
    assert len(calls) == 2
    assert report(client, run['id'])['usage']['totals']['cached_events'] == 0


@pytest.mark.parametrize('status', [200, 503])
def test_provider_secrets_and_raw_content_do_not_reach_public_outputs(client, caplog, status):
    project, run = start(client)
    markers = ['A9-credential-canary', 'A9-private-prompt', 'A9-private-provider-body']
    config = settings(model_max_retries=0)
    config.model_api_key = SecretStr(markers[0])

    def external(request):
        assert request.headers['Authorization'] == 'Bearer ' + markers[0]
        return httpx.Response(status, json=dict(model='test/model', id='gen_synthetic',
            usage=dict(cost='0.1'), error={'message': markers[2]} if status != 200 else None,
            choices=[dict(finish_reason='stop', message=dict(content=json.dumps({'answer': markers[2]})))]))

    def brief(request, context):
        invoke(client, request, external, config=config, text=markers[1])
        return demo_stage(request, context)

    tick(client, registry(brief))
    rid = run['id']
    current = get(client, '/runs/' + rid)
    assert current['status'] == ('waiting_for_input' if status == 200 else 'failed')
    public = [caplog.text, repr(config)]
    for endpoint in ('', '/snapshot', '/usage', '/summary', '/events'):
        public.append(json.dumps(get(client, '/runs/' + rid + endpoint)))
    for format in ('markdown', 'html'):
        exported = post(client, '/projects/' + project['id'] + '/exports', dict(
            run_id=rid, expected_revision=current['revision'], format=format, allow_draft=True), status=201)
        public.append(client.get(exported['download_url']).text)
    assert all(marker not in text for marker in markers for text in public)
