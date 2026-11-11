"""A7: persisted control between calls and conservative versioned reuse."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from threading import Event

from fastapi.testclient import TestClient
import pytest

from app.config import Settings
from app.llm.accounting import UsageRecorder
from app.main import create_app
from app.worker import WorkerService
from app.storage.validation import validate_snapshot
from app.workflow.demo import build_stages, demo_stage
from test_api import get, post, select, start, tick


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as value:
        yield value


def counted_registry(counter):
    registry = build_stages()

    def handler(request, context):
        counter[request['stage']] += 1
        return demo_stage(request, context)

    return replace(registry, stages={key: replace(spec, handler=handler) for key, spec in registry.stages.items()})


def revise(client, project, run_id, **changes):
    snapshot = get(client, '/runs/' + run_id + '/snapshot')
    brief = deepcopy(snapshot['brief']['input'])
    brief.update(changes)
    post(client, '/projects/' + project['id'] + '/brief-versions',
         dict(expected_brief_version=snapshot['brief']['version'], brief=brief), status=201)
    paused = get(client, '/runs/' + run_id)
    assert paused['status'] == 'paused'
    post(client, '/runs/' + run_id + '/resume', dict(expected_revision=paused['revision']))


def current_artifact(snapshot, stage):
    return next(a for a in snapshot['artifacts'] if a['stage'] == stage and a['freshness'] == 'current')


def test_budget_change_reuses_research_but_recomputes_options_and_keeps_archive(client):
    counts = Counter()
    registry = counted_registry(counts)
    project, run = start(client)
    rid = run['id']
    tick(client, registry)
    old = get(client, '/runs/' + rid + '/snapshot')
    research = current_artifact(old, 'research')
    old_claim = old['data']['claims'][0]
    old_export = post(client, '/projects/' + project['id'] + '/exports',
                      dict(run_id=rid, expected_revision=old['run']['revision'], format='markdown', allow_draft=True), status=201)
    document = client.get(old_export['download_url']).content
    revise(client, project, rid, budget=dict(amount='12000', currency='RUB'), constraints=['Запрещена реклама'])
    tick(client, registry)
    new = get(client, '/runs/' + rid + '/snapshot')
    validate_snapshot(new)
    assert counts['brief'] == counts['options'] == counts['communications'] == counts['review'] == 2
    assert counts['research'] == 1
    reused = current_artifact(new, 'research')
    assert reused['reused_from'] == dict(artifact_id=research['id'], version=research['version'], brief_version=1)
    assert reused['version'] == 2 and reused['brief_version'] == 2
    assert new['data']['claims'][0]['version'] == 2 and new['data']['claims'][0]['brief_version'] == 2
    assert client.app.state.storage.get_entity(project['id'], 'claim', old_claim['id'], 1) == old_claim
    assert not new['data']['decisions'] and new['run']['stage'] == 'selection'
    assert client.get(old_export['download_url']).content == document
    select(client, rid)
    tick(client, registry)
    final = get(client, '/runs/' + rid + '/snapshot')
    assert final['run']['status'] == 'completed' and final['usage'] == []
    exported = post(client, '/projects/' + project['id'] + '/exports',
                    dict(run_id=rid, expected_revision=final['run']['revision'], format='markdown', allow_draft=True), status=201)
    text = client.get(exported['download_url']).text
    assert '12000' in text and '50000' not in text


@pytest.mark.parametrize('change', [dict(product='Другой продукт'), dict(audience=['Другой сегмент']),
                                   dict(goal='Другая цель'), dict(assumptions=['Новая гипотеза'])])
def test_changed_research_question_recomputes_research(client, change):
    counts = Counter()
    registry = counted_registry(counts)
    project, run = start(client)
    tick(client, registry)
    revise(client, project, run['id'], **change)
    tick(client, registry)
    assert counts['research'] == 2
    snapshot = get(client, '/runs/' + run['id'] + '/snapshot')
    assert 'reused_from' not in current_artifact(snapshot, 'research')


@pytest.mark.parametrize('mode', ['unknown', 'budget_dependent'])
def test_undeclared_or_changed_stage_dependency_prevents_reuse(client, mode):
    counts = Counter()
    registry = counted_registry(counts)
    spec = registry.stages['research']
    spec = replace(spec, reuse_brief_fields=None, reuse_collections=None) if mode == 'unknown' else replace(
        spec, reuse_brief_fields=spec.reuse_brief_fields + ('budget',))
    registry = replace(registry, stages=dict(registry.stages, research=spec))
    project, run = start(client)
    tick(client, registry)
    revise(client, project, run['id'], budget=dict(amount='12000', currency='RUB'))
    tick(client, registry)
    assert counts['research'] == 2


def test_old_answer_in_source_prevents_reuse(client):
    counts = Counter()
    registry = counted_registry(counts)
    project, run = start(client, missing_budget=True)
    rid = run['id']
    tick(client, registry)
    first = get(client, '/runs/' + rid + '/snapshot')
    post(client, '/runs/' + rid + '/answers', dict(expected_revision=first['run']['revision'],
         answers=[dict(question_id=first['questions'][0]['id'], value='50000 RUB')]))
    tick(client, registry)
    revise(client, project, rid, budget=dict(amount='12000', currency='RUB'))
    tick(client, registry)
    assert counts['research'] == 2
    assert '50000 RUB' not in str(get(client, '/runs/' + rid + '/snapshot')['data'])


def test_repeated_budget_revisions_keep_consecutive_versions_and_new_origins(client):
    counts = Counter()
    registry = counted_registry(counts)
    project, run = start(client)
    tick(client, registry)
    for version, amount in [(2, '12000'), (3, '9000')]:
        revise(client, project, run['id'], budget=dict(amount=amount, currency='RUB'))
        tick(client, registry)
        snapshot = get(client, '/runs/' + run['id'] + '/snapshot')
        validate_snapshot(snapshot)
        artifact = current_artifact(snapshot, 'research')
        assert artifact['version'] == artifact['brief_version'] == version
        assert artifact['reused_from']['brief_version'] == version - 1
    assert counts['research'] == 1


@pytest.mark.parametrize('mode', ['api', 'worker_stop'])
def test_pause_before_second_tool_action_preserves_checkpoint_and_usage(client, mode):
    project, run = start(client)
    rid = run['id']
    entered, finish = Event(), Event()
    stop = Event()
    actions = []
    registry = build_stages()

    def research(request, context):
        for action in ('search', 'extract'):
            context.check_pause()
            actions.append(action)
            if action == 'search' and len(actions) == 1:
                entered.set()
                assert finish.wait(10)
            UsageRecorder(client.app.state.storage).record_tool(run_id=rid, stage='research',
                provider='synthetic', tool=action, duration_ms=1, outcome='success')
        return demo_stage(request, context)

    registry = replace(registry, stages=dict(registry.stages, research=replace(registry.stages['research'], handler=research)))
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(WorkerService(client.app.state.storage, registry).run, once=True, stop=stop)
        try:
            assert entered.wait(5)
            active = get(client, '/runs/' + rid)
            checkpoint = active['checkpoint_id']
            if mode == 'api':
                post(client, '/runs/' + rid + '/pause', dict(expected_revision=active['revision']))
            else:
                stop.set()
        finally:
            finish.set()
        future.result(timeout=10)
    stopped = get(client, '/runs/' + rid + '/snapshot')
    assert actions == ['search'] and len(stopped['usage']) == 1
    assert stopped['run']['status'] == 'paused' and stopped['run']['stage'] == 'research'
    assert stopped['run']['checkpoint_id'] == checkpoint and stopped['run']['error'] is None
    assert client.app.state.storage.list_jobs(rid)[-1]['state'] == 'interrupted'
    post(client, '/runs/' + rid + '/resume', dict(expected_revision=stopped['run']['revision']))
    tick(client, registry)
    assert actions == ['search', 'search', 'extract']  # The unfinished step is repeated, not its confirmed predecessors.
    assert get(client, '/runs/' + rid)['stage'] == 'selection'


def test_old_manifest_can_resume_but_changed_model_cannot(client):
    registry = build_stages()
    old = deepcopy(registry.manifest())
    old['stages']['research'].pop('reuse_brief_fields')
    old['stages']['research'].pop('reuse_collections')
    old['stages']['research'].pop('reuse_max_age_seconds')
    assert registry.accepts_manifest(old)
    old['stages']['research']['model_id'] = 'another-model'
    assert not registry.accepts_manifest(old)


def test_reuse_declarations_are_validated():
    registry = build_stages()
    with pytest.raises(ValueError):
        replace(registry, stages=dict(registry.stages, research=replace(registry.stages['research'], reuse_brief_fields=('bogus',))))


def research_with_evidence(client, calls, *, age_seconds=0):
    registry = build_stages()

    def handler(request, context):
        calls.append(request['brief']['version'])
        output = demo_stage(request, context)
        if 'source_external' in request['brief']['input']['excluded_source_ids']:
            return output
        source = dict(id='source_external', version=1, project_id=request['project_id'], brief_version=request['brief']['version'],
                      kind='web', title='Синтетический источник', url='https://example.org/', publisher=None, published_at=None,
                      retrieved_at=(datetime.now(timezone.utc) - timedelta(seconds=age_seconds)).isoformat(),
                      content_excerpt='Тестовая цитата.', locator='Строка 1', verification='read', origin_note='Синтетическая проверка')
        output['data']['sources'] = [source]
        claim = output['data']['claims'][0]
        evidence = dict(source_id=source['id'], source_version=1, quote=source['content_excerpt'], locator=source['locator'], relation='supports')
        claim.update(kind='external_fact', verification='supported', evidence=[evidence])
        issue = dict(id='issue_synthetic', version=1, project_id=request['project_id'], brief_version=request['brief']['version'],
                     code='SYNTHETIC', severity='info', target=dict(kind='claim', id=claim['id'], version=claim['version']),
                     explanation='Проверка ссылок', evidence=[deepcopy(evidence)], suggested_fix='Проверено', return_to='research', status='resolved')
        output['data']['issues'] = [issue]
        output['artifacts'][0]['entities'] += [dict(kind='source', id=source['id'], version=1), dict(kind='issue', id=issue['id'], version=1)]
        usage = UsageRecorder(client.app.state.storage).record_tool(run_id=request['run_id'], stage='research',
                    provider='synthetic', tool='read_source', duration_ms=1, outcome='success',
                    cost=dict(amount='0', currency='USD'), cost_basis='reported')
        output['usage_ids'] = [usage['id']]
        return output

    return replace(registry, stages=dict(registry.stages, research=replace(registry.stages['research'], handler=handler)))


def test_reuse_updates_all_exact_evidence_refs_without_rebilling_or_refreshing_read_time(client):
    calls = []
    registry = research_with_evidence(client, calls)
    project, run = start(client)
    tick(client, registry)
    old = get(client, '/runs/' + run['id'] + '/snapshot')
    source = next(s for s in old['data']['sources'] if s['id'] == 'source_external')
    revise(client, project, run['id'], budget=dict(amount='12000', currency='RUB'))
    tick(client, registry)
    current = get(client, '/runs/' + run['id'] + '/snapshot')
    validate_snapshot(current)
    assert calls == [1] and current['usage'] == old['usage']
    copied = next(s for s in current['data']['sources'] if s['id'] == source['id'])
    assert copied['version'] == 2 and copied['retrieved_at'] == source['retrieved_at']
    assert current['data']['claims'][0]['evidence'][0]['source_version'] == 2
    assert current['data']['issues'][0]['target']['version'] == 2
    assert current['data']['issues'][0]['evidence'][0]['source_version'] == 2


def test_source_exclusion_forces_research_and_removes_old_evidence(client):
    calls = []
    registry = research_with_evidence(client, calls)
    _, run = start(client)
    tick(client, registry)
    old = get(client, '/runs/' + run['id'] + '/snapshot')
    paused = post(client, '/runs/' + run['id'] + '/exclude-source',
                  dict(expected_revision=old['run']['revision'], source_id='source_external', source_version=1, reason='Исключить'))
    post(client, '/runs/' + run['id'] + '/resume', dict(expected_revision=paused['revision']))
    tick(client, registry)
    current = get(client, '/runs/' + run['id'] + '/snapshot')
    assert calls == [1, 2] and current['run']['stage'] == 'selection'
    assert all(s['id'] != 'source_external' for s in current['data']['sources'])
    assert not current['data']['claims'][0]['evidence']


def test_expired_source_is_not_reused(client):
    calls = []
    registry = research_with_evidence(client, calls, age_seconds=7200)
    project, run = start(client)
    tick(client, registry)
    revise(client, project, run['id'], budget=dict(amount='12000', currency='RUB'))
    tick(client, registry)
    assert calls == [1, 2]
