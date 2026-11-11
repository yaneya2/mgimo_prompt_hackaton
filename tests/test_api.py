"""A6 acceptance through HTTP; worker calls are explicitly synthetic."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from threading import Event
from uuid import uuid4

from fastapi.testclient import TestClient
from jsonschema import Draft202012Validator, FormatChecker
import pytest

from app.config import Settings
from app.main import create_app
from app.storage import Storage
from app.storage.validation import validate, validate_snapshot
from app.worker import WorkerService
from app.workflow.demo import build_stages, demo_stage
from scripts.try_workflow import LIMITS, demo_brief

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        yield client


def post(client, path, body, *, key=None, status=202):
    response = client.post('/api/v1' + path, json=body, headers={'Idempotency-Key': key or uuid4().hex})
    assert response.status_code == status, response.text
    return response.json()


def get(client, path):
    response = client.get('/api/v1' + path)
    assert response.status_code == 200, response.text
    return response.json()


def start(client, *, missing_budget=False):
    brief = demo_brief('Новый продукт: учёт материалов мастерской')
    if not missing_budget:
        brief['budget'] = {'amount': '50000', 'currency': 'RUB'}
    project = post(client, '/projects', dict(name='Проверка HTTP', brief=brief), status=201)
    run = post(client, '/projects/' + project['id'] + '/runs', dict(brief_version=1, limits=LIMITS))
    return project, run


def tick(client, registry=None):
    WorkerService(client.app.state.storage, registry or build_stages()).run(once=True)


def select(client, run_id):
    snapshot = get(client, '/runs/' + run_id + '/snapshot')
    option = snapshot['data']['options'][0]
    return post(client, '/runs/' + run_id + '/selection', dict(expected_revision=snapshot['run']['revision'],
        option_id=option['id'], option_version=option['version'], rationale='Выбор человека через HTTP'))


def test_full_http_flow_questions_selection_exports_and_restart(client):
    project, run = start(client, missing_budget=True)
    pid, rid = project['id'], run['id']
    assert get(client, '/projects') == [project]
    assert get(client, '/projects/' + pid + '/brief')['version'] == 1
    assert get(client, '/projects/' + pid + '/runs') == [run]
    tick(client)
    first = get(client, '/runs/' + rid + '/snapshot')
    assert first['run']['status'] == 'waiting_for_input' and first['questions']
    body = dict(expected_revision=first['run']['revision'], answers=[dict(question_id=first['questions'][0]['id'], value='50000 RUB')])
    answered = post(client, '/runs/' + rid + '/answers', body, key='answer')
    tick(client)
    assert post(client, '/runs/' + rid + '/answers', body, key='answer') == answered
    before_choice = get(client, '/runs/' + rid + '/snapshot')
    assert before_choice['run']['stage'] == 'selection' and not before_choice['data']['decisions']
    select(client, rid)
    tick(client)
    final = get(client, '/runs/' + rid + '/snapshot')
    validate_snapshot(final)
    assert final['run']['status'] == 'completed' and final['run']['quality'] == 'draft'
    assert get(client, '/runs/' + rid + '/artifacts') == final['artifacts']
    assert get(client, '/runs/' + rid + '/usage') == []
    body = dict(run_id=rid, expected_revision=final['run']['revision'], format='markdown', allow_draft=False)
    assert post(client, '/projects/' + pid + '/exports', body, status=409)['error']['code'] == 'INVALID_STATE'
    body['allow_draft'] = True
    export = post(client, '/projects/' + pid + '/exports', body, key='export', status=201)
    validate('Export', export)
    download = client.get(export['download_url'])
    assert download.status_code == 200 and 'ЧЕРНОВИК' in download.text and 'СИНТЕТИЧЕСКИЙ' in download.text
    assert 'attachment;' in download.headers['Content-Disposition']
    with TestClient(create_app(client.app.state.settings)) as restarted:
        assert get(restarted, '/runs/' + rid + '/snapshot') == final
        assert post(restarted, '/projects/' + pid + '/exports', body, key='export', status=201) == export
        assert restarted.get(export['download_url']).content == download.content


def test_replays_are_atomic_scoped_and_survive_restart(client):
    body = dict(name='One project', brief=demo_brief())
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: post(client, '/projects', body, key='same', status=201), range(2)))
    assert results[0] == results[1] and len(get(client, '/projects')) == 1
    assert post(client, '/projects', dict(body, name='Different'), key='same', status=409)['error']['code'] == 'VERSION_CONFLICT'
    pid = results[0]['id']
    run_body = dict(brief_version=1, limits=LIMITS)
    run = post(client, '/projects/' + pid + '/runs', run_body, key='same')
    assert post(client, '/projects/' + pid + '/runs', run_body, key='same') == run
    assert post(client, '/projects/' + pid + '/runs', run_body, key='other', status=409)['error']['code'] == 'RUN_ALREADY_ACTIVE'
    with TestClient(create_app(client.app.state.settings)) as restarted:
        assert post(restarted, '/projects', body, key='same', status=201) == results[0]


def test_failure_before_replay_record_rolls_back_effects(client):
    storage = client.app.state.storage
    with storage.transaction(write=True) as db:
        db.execute("CREATE TRIGGER fail_command BEFORE INSERT ON commands BEGIN SELECT RAISE(ABORT, 'private diagnostic'); END")
    body = dict(name='Atomic', brief=demo_brief())
    response = post(client, '/projects', body, key='atomic', status=503)
    assert 'private diagnostic' not in json.dumps(response)
    assert get(client, '/projects') == []
    with storage.transaction(write=True) as db:
        db.execute('DROP TRIGGER fail_command')
    post(client, '/projects', body, key='atomic', status=201)
    assert len(get(client, '/projects')) == 1


@pytest.mark.parametrize('raw', ['{"private":"secret-123"}', '{"name":"a","name":"b"}', '{"name":NaN}', '{"name":1e309}', '[]', '{'])
def test_invalid_json_has_safe_contract_error(client, raw):
    response = client.post('/api/v1/projects', content=raw, headers={'Idempotency-Key':'invalid','Content-Type':'application/json'})
    assert response.status_code == 422
    validate('ErrorResponse', response.json())
    assert 'secret-123' not in response.text


def test_missing_headers_invalid_ids_not_found_and_request_bound(client, tmp_path):
    assert client.post('/api/v1/projects', json={}).status_code == 422
    assert client.post('/api/v1/projects', content='{}', headers={'Idempotency-Key':'k'}).status_code == 422
    for path, status in [('/runs/bad!',422),('/runs/run_missing',404),('/missing',404),('/exports/export_missing/download',404)]:
        response = client.get('/api/v1' + path)
        assert response.status_code == status
        validate('ErrorResponse', response.json())
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path/'small',api_max_request_bytes=1024))) as small:
        response = small.post('/api/v1/projects', content='x'*1025, headers={'Idempotency-Key':'k','Content-Type':'application/json'})
        assert response.status_code == 422 and 'xxxxx' not in response.text


def test_pause_during_stage_commits_result_without_starting_next_stage(client):
    _, run = start(client)
    rid = run['id']
    entered, finish = Event(), Event()
    calls = []
    def blocking(request, context):
        calls.append(request['stage'])
        entered.set()
        assert finish.wait(10)
        return demo_stage(request, context)
    registry = build_stages()
    registry = replace(registry, stages=dict(registry.stages, brief=replace(registry.stages['brief'],handler=blocking)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(tick, client, registry)
        try:
            assert entered.wait(5)
            current = get(client, '/runs/' + rid)
            pause_body = dict(expected_revision=current['revision'])
            requested = post(client, '/runs/' + rid + '/pause', pause_body, key='pause')
            assert requested['status'] == 'pause_requested'
        finally:
            finish.set()
        future.result(timeout=10)
    paused = get(client, '/runs/' + rid)
    assert paused['status'] == 'paused' and paused['stage'] == 'research'
    assert calls == ['brief'] and len(client.app.state.storage.list_jobs(rid)) == 1
    assert post(client, '/runs/' + rid + '/pause', pause_body, key='pause') == requested
    post(client, '/runs/' + rid + '/resume', dict(expected_revision=paused['revision']))
    tick(client)
    assert get(client, '/runs/' + rid)['stage'] == 'selection'


def test_pause_waiting_then_resume_restores_questions_without_calling_agent(client):
    _, run = start(client, missing_budget=True)
    rid = run['id']
    tick(client)
    waiting = get(client, '/runs/' + rid + '/snapshot')
    paused = post(client, '/runs/' + rid + '/pause', dict(expected_revision=waiting['run']['revision']))
    post(client, '/runs/' + rid + '/resume', dict(expected_revision=paused['revision']))
    registry = build_stages()
    def forbidden(*_):
        pytest.fail('Unanswered question must be restored without generation')
    registry = replace(registry, stages={key: replace(spec,handler=forbidden) for key,spec in registry.stages.items()})
    tick(client,registry)
    restored = get(client, '/runs/' + rid + '/snapshot')
    assert restored['questions'] == waiting['questions']
    assert restored['run']['status'] == 'waiting_for_input'


def test_revise_stopped_run_preserves_history_and_recomputes_without_version_collisions(client):
    project, run = start(client)
    pid, rid = project['id'], run['id']
    tick(client)
    old = get(client, '/runs/' + rid + '/snapshot')
    export = post(client, '/projects/' + pid + '/exports', dict(run_id=rid,expected_revision=old['run']['revision'],format='markdown',allow_draft=True),status=201)
    old_document = client.get(export['download_url']).content
    brief = deepcopy(old['brief']['input'])
    brief['budget']['amount']='12000'
    brief['constraints'].append('Запрещена платная реклама')
    revised = post(client, '/projects/' + pid + '/brief-versions',dict(expected_brief_version=1,brief=brief),status=201)
    assert revised['version'] == 2
    stopped = get(client, '/runs/' + rid + '/snapshot')
    validate_snapshot(stopped)
    assert stopped['run']['status']=='paused' and stopped['run']['stage']=='brief'
    assert all(a['freshness']=='stale' for a in stopped['artifacts'])
    assert all(not values for values in stopped['data'].values())
    assert client.app.state.storage.get_brief(pid,1)['input']==old['brief']['input']
    post(client,'/runs/'+rid+'/resume',dict(expected_revision=stopped['run']['revision']))
    tick(client)
    second = get(client, '/runs/' + rid + '/snapshot')
    assert second['run']['stage']=='selection' and second['run']['status']=='waiting_for_input'
    assert all(item['brief_version']==2 for items in second['data'].values() for item in items)
    assert second['data']['sources'][0]['version']==2
    select(client,rid)
    tick(client)
    final=get(client,'/runs/'+rid+'/snapshot')
    validate_snapshot(final)
    assert final['run']['status']=='completed'
    assert client.get(export['download_url']).content==old_document
    current=post(client,'/projects/'+pid+'/exports',dict(run_id=rid,expected_revision=final['run']['revision'],format='markdown',allow_draft=True),status=201)
    document=client.get(current['download_url']).text
    assert '12000' in document and '50000' not in document and 'Запрещена платная реклама' in document


def test_exclusion_is_versioned_and_removes_source_from_recalculation(client):
    project, run = start(client)
    rid=run['id']
    tick(client)
    before=get(client,'/runs/'+rid+'/snapshot')
    source=before['data']['sources'][0]
    body=dict(expected_revision=before['run']['revision'],source_id=source['id'],source_version=source['version'],reason='Не использовать этот источник')
    assert post(client,'/runs/'+rid+'/exclude-source',dict(body,source_version=99),status=409)['error']['code']=='VERSION_CONFLICT'
    excluded=post(client,'/runs/'+rid+'/exclude-source',body,key='exclude')
    assert excluded['status']=='paused' and excluded['brief_version']==2
    assert source['id'] in get(client,'/projects/'+project['id']+'/brief')['input']['excluded_source_ids']
    assert post(client,'/runs/'+rid+'/exclude-source',body,key='exclude')==excluded
    post(client,'/runs/'+rid+'/resume',dict(expected_revision=excluded['revision']))
    tick(client)
    after=get(client,'/runs/'+rid+'/snapshot')
    assert after['run']['stage']=='selection'
    assert source['id'] not in {s['id'] for s in after['data']['sources']}


def test_html_export_escapes_content_and_renderer_failure_is_atomic(client,tmp_path):
    project,run=start(client)
    rid=run['id']
    post(client,'/runs/'+rid+'/pause',dict(expected_revision=run['revision']))
    brief=demo_brief('<script>alert("private")</script>')
    post(client,'/projects/'+project['id']+'/brief-versions',dict(expected_brief_version=1,brief=brief),status=201)
    current=get(client,'/runs/'+rid)
    exported=post(client,'/projects/'+project['id']+'/exports',dict(run_id=rid,expected_revision=current['revision'],format='html',allow_draft=True),status=201)
    response=client.get(exported['download_url'])
    assert '<script>' not in response.text and '&lt;script&gt;' in response.text
    assert "default-src 'none'" in response.headers['Content-Security-Policy']
    def broken(*_):
        raise RuntimeError('private-renderer-secret')
    with TestClient(create_app(Settings(_env_file=None,data_dir=tmp_path/'broken'),renderer=broken)) as broken_client:
        project,run=start(broken_client)
        error=post(broken_client,'/projects/'+project['id']+'/exports',dict(run_id=run['id'],expected_revision=run['revision'],format='html',allow_draft=True),status=500)
        assert 'private-renderer-secret' not in json.dumps(error)
        with broken_client.app.state.storage.transaction() as db:
            assert db.execute('SELECT COUNT(*) FROM exports').fetchone()[0]==0


def test_served_openapi_matches_all_contract_routes_and_resolves_refs(client):
    contract=json.loads((ROOT/'contracts/openapi.json').read_text())
    document=client.get('/openapi.json').json()
    for path,operations in contract['paths'].items():
        for method,operation in operations.items():
            assert document['paths']['/api/v1'+path][method]['operationId']==operation['operationId']
    def walk(value):
        if isinstance(value,dict):
            if '$ref' in value:
                assert value['$ref'].startswith('#/components/schemas/')
                assert value['$ref'].split('/')[-1] in document['components']['schemas']
            for item in value.values(): walk(item)
        elif isinstance(value,list):
            for item in value: walk(item)
    walk(document)
    schema=document['components']['schemas']['CreateProject']
    schema=dict(schema,components=document['components'])
    Draft202012Validator(schema,format_checker=FormatChecker()).validate(dict(name='x',brief=demo_brief()))


def test_v2_migration_preserves_runs_and_adds_command_storage(tmp_path):
    store=Storage(tmp_path)
    project=store.create_project('Migration',demo_brief())
    run=store.create_run(project['id'],LIMITS)
    with store.transaction(write=True) as db:
        db.execute('DROP TABLE commands')
        db.execute('DROP TABLE exports')
        for table in ('external_calls', 'response_cache', 'run_events'):
            db.execute('DROP TABLE ' + table)
        db.execute('PRAGMA user_version=2')
    store=Storage(tmp_path)
    assert store.get_run(run['id'])==run
    assert store.command('migration','key',{},lambda: {'saved':True})=={'saved':True}
    assert store.command('migration','key',{},lambda: pytest.fail('Must replay'))=={'saved':True}


def test_pause_between_queue_poll_and_claim_does_not_stop_worker(client, monkeypatch):
    from app.storage.worker import WorkerSession
    _, run = start(client)
    original = WorkerSession.claim_run

    def pause_first(worker, run_id):
        post(client, '/runs/' + run_id + '/pause', dict(expected_revision=run['revision']))
        return original(worker, run_id)

    monkeypatch.setattr(WorkerSession, 'claim_run', pause_first)
    tick(client)
    assert get(client, '/runs/' + run['id'])['status'] == 'paused'
    assert client.app.state.storage.list_jobs(run['id']) == []


def test_control_rejects_stale_versions_and_active_brief_changes(client):
    project, run = start(client)
    pid, rid = project['id'], run['id']
    brief = get(client, '/projects/' + pid + '/brief')['input']
    assert post(client, '/projects/' + pid + '/brief-versions',
                dict(expected_brief_version=1, brief=brief), status=409)['error']['code'] == 'INVALID_STATE'
    assert post(client, '/projects/' + pid + '/runs',
                dict(brief_version=2, limits=LIMITS), status=409)['error']['code'] == 'VERSION_CONFLICT'
    tick(client)
    assert post(client, '/runs/' + rid + '/pause', dict(expected_revision=1),
                status=409)['error']['code'] == 'VERSION_CONFLICT'
    snapshot = get(client, '/runs/' + rid + '/snapshot')
    option = snapshot['data']['options'][0]
    assert post(client, '/runs/' + rid + '/selection',
                dict(expected_revision=snapshot['run']['revision'], option_id=option['id'], option_version=99,
                     rationale='Stale client'), status=409)['error']['code'] == 'VERSION_CONFLICT'


@pytest.mark.parametrize('retryable', [True, False])
def test_execution_errors_are_readable_and_resume_respects_retryability(client, retryable):
    _, run = start(client)
    rid = run['id']
    registry = build_stages()

    def broken(*_):
        raise TimeoutError('private diagnostic') if retryable else RuntimeError('private diagnostic')

    registry = replace(registry, stages=dict(registry.stages, brief=replace(registry.stages['brief'], handler=broken)))
    tick(client, registry)
    failed = get(client, '/runs/' + rid)
    assert failed['status'] == 'failed' and failed['error']['error']['retryable'] == retryable
    assert 'private diagnostic' not in json.dumps(failed)
    response = post(client, '/runs/' + rid + '/resume', dict(expected_revision=failed['revision']),
                    status=202 if retryable else 409)
    if retryable:
        assert response['status'] == 'queued'
        tick(client)
        assert get(client, '/runs/' + rid)['stage'] == 'selection'


def test_resume_does_not_reset_exhausted_repair_counter(client):
    _, run = start(client)
    rid = run['id']
    registry = build_stages()
    registry = replace(registry, stages=dict(registry.stages, brief=replace(registry.stages['brief'], handler=lambda *_: {})))
    tick(client, registry)
    stopped = get(client, '/runs/' + rid)
    assert stopped['status'] == 'incomplete'
    storage = client.app.state.storage
    before = len(storage.list_jobs(rid))
    post(client, '/runs/' + rid + '/resume', dict(expected_revision=stopped['revision']))
    tick(client, registry)
    again = get(client, '/runs/' + rid)
    assert again['status'] == 'incomplete' and len(storage.list_jobs(rid)) == before + 1
    assert storage.get_checkpoint(again['checkpoint_id'])['graph_state']['repairs_used'] == LIMITS['max_repair_cycles']


def test_revised_brief_does_not_reuse_old_answers(client):
    project, run = start(client, missing_budget=True)
    pid, rid = project['id'], run['id']
    tick(client)
    waiting = get(client, '/runs/' + rid + '/snapshot')
    post(client, '/runs/' + rid + '/answers', dict(expected_revision=waiting['run']['revision'],
         answers=[dict(question_id=waiting['questions'][0]['id'], value='50000 RUB')]))
    tick(client)
    brief = deepcopy(waiting['brief']['input'])
    brief['constraints'].append('Пересогласовать бюджет')
    post(client, '/projects/' + pid + '/brief-versions', dict(expected_brief_version=1, brief=brief), status=201)
    stopped = get(client, '/runs/' + rid)
    post(client, '/runs/' + rid + '/resume', dict(expected_revision=stopped['revision']))
    tick(client)
    current = get(client, '/runs/' + rid + '/snapshot')
    assert current['run']['status'] == 'waiting_for_input' and current['run']['stage'] == 'brief'
    assert current['questions'] and current['run']['brief_version'] == 2


def test_recalculation_rebases_evidence_and_artifact_refs_together(client):
    project, run = start(client)
    pid, rid = project['id'], run['id']
    registry = build_stages()

    def brief_with_evidence(request, context):
        output = demo_stage(request, context)
        source = output['data']['sources'][0]
        claim = dict(id='claim_brief', version=1, project_id=pid, brief_version=request['brief']['version'],
                     text=source['content_excerpt'], kind='user_provided', verification='supported', limitations=[],
                     evidence=[dict(source_id=source['id'], source_version=source['version'], quote=source['content_excerpt'],
                                    locator=source['locator'], relation='supports')])
        output['data']['claims'] = [claim]
        output['artifacts'][0]['entities'].append(dict(kind='claim', id=claim['id'], version=1))
        return output

    registry = replace(registry, stages=dict(registry.stages, brief=replace(registry.stages['brief'], handler=brief_with_evidence)))
    tick(client, registry)
    old = get(client, '/runs/' + rid + '/snapshot')
    assert old['run']['stage'] == 'selection'
    post(client, '/projects/' + pid + '/brief-versions',
         dict(expected_brief_version=1, brief=old['brief']['input']), status=201)
    stopped = get(client, '/runs/' + rid)
    post(client, '/runs/' + rid + '/resume', dict(expected_revision=stopped['revision']))
    tick(client, registry)
    current = get(client, '/runs/' + rid + '/snapshot')
    validate_snapshot(current)
    assert current['run']['stage'] == 'selection'
    claim = next(c for c in current['data']['claims'] if c['id'] == 'claim_brief')
    assert claim['version'] == 2 and claim['evidence'][0]['source_version'] == 2
    assert client.app.state.storage.get_entity(pid, 'claim', 'claim_brief', 1)['brief_version'] == 1
