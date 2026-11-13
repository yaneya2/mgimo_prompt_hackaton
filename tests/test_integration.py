"""Shared platform seams introduced when joining the three participants."""
from dataclasses import replace
from pathlib import Path
import pytest
from app.agents.limits import CallGate
from app.execution import execution_control, PauseRequested
from app.tools.research import ResearchTools
from test_agents import request, config, Sink, CONTEXT, real_handlers_with_fixture
from app.storage import Storage
from app.worker import WorkerService
from scripts.try_workflow import demo_brief, LIMITS
from app.storage.commands import Commands
from app.reports.render import render_snapshot


def test_search_checks_platform_pause_before_transport(tmp_path):
    req = request()
    sink = Sink()
    settings = config(tmp_path, tavily_api_key='test-key')
    tools = ResearchTools(settings, sink, CallGate(tmp_path), req)
    def paused():
        raise PauseRequested()
    try:
        with execution_control(req['run_id'], paused):
            with pytest.raises(PauseRequested):
                tools.search('test')
        assert sink.events == []
    finally:
        tools.close()


def test_same_filename_materials_have_distinct_exclusion_keys(tmp_path):
    settings = config(tmp_path, research_materials_dir=tmp_path/'materials')
    for folder in ('product', 'market'):
        p = settings.research_materials_dir/folder/'card.md'
        p.parent.mkdir(parents=True)
        p.write_text(folder)
    tools = ResearchTools(settings, Sink(), CallGate(tmp_path), request())
    try:
        a, b = [tools.read_file(f'{folder}/card.md') for folder in ('product','market')]
        assert a['title'] == b['title']
        assert a['id'] != b['id']
        assert a['origin_note'].splitlines()[0] != b['origin_note'].splitlines()[0]
    finally:
        tools.close()


def test_real_agents_revise_and_export_through_atomic_platform(tmp_path):
    store = Storage(tmp_path/'state')
    brief = demo_brief(); brief['budget'] = {'amount':'50000','currency':'RUB'}
    project = store.create_project('Integrated agents', brief)
    run = store.create_run(project['id'], dict(LIMITS, timeout_seconds=600))
    registry, model = real_handlers_with_fixture(tmp_path, store)
    WorkerService(store, registry).run(once=True)
    assert store.get_run(run['id'])['stage'] == 'selection'
    commands = Commands(store, render_snapshot)
    brief = dict(brief, budget={'amount':'25000','currency':'RUB'})
    body = dict(expected_brief_version=1, brief=brief)
    def interrupted():
        commands.revise_brief(project['id'], body)
        raise OSError('simulated interruption before receipt')
    with pytest.raises(OSError):
        store.command('revise', 'same-key', body, interrupted)
    assert store.get_brief(project['id'])['version'] == 1
    result = store.command('revise', 'same-key', body, lambda: commands.revise_brief(project['id'], body))
    assert store.command('revise', 'same-key', body, lambda: pytest.fail('repeated command')) == result
    current = store.get_run(run['id'])
    commands.resume(run['id'], dict(expected_revision=current['revision']))
    WorkerService(store, registry).run(once=True)
    snap = store.get_snapshot(run['id'])
    assert snap['run']['stage'] == 'selection', snap['run']
    option = snap['data']['options'][0]
    store.submit_selection(run['id'], option['id'], option['version'], 'Integration test', expected_revision=snap['run']['revision'])
    WorkerService(store, registry).run(once=True)
    final = store.get_run(run['id'])
    assert final['status'] == 'completed', final
    export = commands.create_export(project['id'], dict(run_id=run['id'], expected_revision=final['revision'], format='html', allow_draft=True))
    _, text = commands.download_export(export['id'])
    assert '25000' in text and 'Integration test' in text


def test_local_file_reads_do_not_consume_search_quota(tmp_path):
    from app.llm.accounting import UsageRecorder
    store = Storage(tmp_path/'state')
    project = store.create_project('Quota test', demo_brief())
    run = store.create_run(project['id'], LIMITS)
    recorder = UsageRecorder(store)
    for _ in range(6):
        recorder.record_tool(run_id=run['id'], stage='research', provider='local', tool='read_file', duration_ms=0,
                             cost={'amount':'0','currency':'USD'}, cost_basis='reported')
    for _ in range(4):
        recorder.record_tool(run_id=run['id'], stage='research', provider='tavily', tool='search', duration_ms=0)
    summary = store.run_summary(run['id'])
    assert summary['search_calls'] == 4
    assert summary['usage']['totals']['events'] == 10


def test_unlimited_time_retains_call_quota(tmp_path):
    from app.config import Settings
    from app.storage.runtime import RunPolicy
    assert Settings(_env_file=None, model_timeout_seconds=0).model_timeout_seconds is None
    store = Storage(tmp_path/'state')
    project = store.create_project('Unlimited time', demo_brief())
    run = store.create_run(project['id'], dict(LIMITS, timeout_seconds=0))
    assert store.run_summary(run['id'])['remaining_seconds'] is None
