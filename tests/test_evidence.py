"""Submission metrics preserve uncertainty, source data and immutable export files."""
import csv
import hashlib
import json
from pathlib import Path

import pytest

from app.llm.accounting import UsageRecorder
from app.storage import Storage, StorageError
from app.storage.evidence import run_evidence
from app.storage.runtime import RunPolicy
from app.workflow import Orchestrator
from app.workflow.demo import build_stages
from scripts.export_run import export_run
from test_storage import store


def test_export_keeps_paid_errors_unknown_costs_and_currencies_separate(store, tmp_path):
    recorder = UsageRecorder(store)
    recorder.record_tool(run_id='run_demo', stage='brief', provider='=unsafe()', tool='search', duration_ms=12,
                         cost={'amount':'0.12','currency':'USD'}, outcome='error')
    recorder.record_tool(run_id='run_demo', stage='brief', provider='fixture', tool='search', duration_ms=3,
                         cost={'amount':'10','currency':'RUB'}, cost_basis='estimated', attempt=2)
    recorder.record_tool(run_id='run_demo', stage='brief', provider='fixture', tool='search', duration_ms=5)
    before = store.database_path.read_bytes()
    folder = export_run(store.data_dir, 'run_demo', tmp_path / 'bundle')
    data = json.loads((folder / 'metrics.json').read_text())
    totals = data['summary']['usage']['totals']
    assert totals['errors'] == totals['retries'] == totals['unknown_cost_events'] == 1
    assert totals['cost_by_currency']['USD']['reported'] == '0.12'
    assert totals['cost_by_currency']['RUB']['estimated'] == '10'
    assert not data['summary']['accounting_complete'] and data['execution_revision'] is None
    assert data['synthetic'] is None  # A run with no executed registry is not asserted to be real or synthetic.
    with (folder / 'usage.csv').open(newline='') as stream:
        rows = list(csv.DictReader(stream))
    assert rows[0]['provider'] == "'=unsafe()" and rows[2]['cost_amount'] == ''
    manifest = json.loads((folder / 'manifest.json').read_text())
    for name, value in manifest['files'].items():
        assert hashlib.sha256((folder / name).read_bytes()).hexdigest() == value['sha256']
    assert store.database_path.read_bytes() == before
    with pytest.raises(FileExistsError):
        export_run(store.data_dir, 'run_demo', folder)
    assert (folder / 'metrics.json').read_text() == json.dumps(data, ensure_ascii=False, indent=2) + '\n'


def test_unrecorded_reservation_and_active_job_are_exported_without_recovery(store):
    with store.worker() as worker:
        run = worker.claim_run('run_demo')
        request, _ = Orchestrator(store, build_stages())._request(run)
        job = worker.begin_step(run['id'], expected_revision=run['revision'])
        RunPolicy(store, request, job, dict(feedback=[], repairs_used=0)).reserve('model', 10)
    before = store.get_run('run_demo')
    evidence = run_evidence(Storage(store.data_dir, readonly=True), 'run_demo')
    assert evidence['summary']['model_calls'] == 1
    assert evidence['summary']['budget']['unrecorded_attempts'] == 1
    assert evidence['usage'] == [] and not evidence['summary']['accounting_complete']
    assert store.get_run('run_demo') == before and before['status'] == 'running'


def test_export_omits_brief_and_error_body(store):
    with store.transaction(write=True) as db:
        run = store._run(db, 'run_demo')
        run.update(status='failed', error={'error': dict(code='INTERNAL_ERROR', message='private-error-canary',
                   retryable=False, request_id='request_fixture', run_id='run_demo', field_errors=[])})
        store._write_run(db, run)
    evidence = run_evidence(Storage(store.data_dir, readonly=True), 'run_demo')
    encoded = json.dumps(evidence, ensure_ascii=False)
    assert 'private-error-canary' not in encoded
    assert store.get_brief('project_demo')['input']['product'] not in encoded
    assert evidence['run']['error'] == {'code': 'INTERNAL_ERROR', 'retryable': False}


def test_readonly_export_does_not_create_or_migrate_data(tmp_path, store):
    with pytest.raises(StorageError):
        Storage(tmp_path / 'missing', readonly=True)
    assert not (tmp_path / 'missing').exists()
    with store.transaction(write=True) as db:
        db.execute('PRAGMA user_version=3')
    before = store.database_path.read_bytes()
    with pytest.raises(StorageError):
        Storage(store.data_dir, readonly=True)
    assert store.database_path.read_bytes() == before


def test_export_failure_never_publishes_partial_bundle(store, tmp_path, monkeypatch):
    def broken():
        raise OSError('private file error')
    monkeypatch.setattr('scripts.export_run.environment', broken)
    with pytest.raises(OSError):
        export_run(store.data_dir, 'run_demo', tmp_path / 'bundle')
    assert not (tmp_path / 'bundle').exists()
    assert not list(tmp_path.glob('.export-*'))


def test_readonly_storage_rejects_mutations(store):
    readonly = Storage(store.data_dir, readonly=True)
    with pytest.raises(StorageError):
        with readonly.transaction(write=True):
            pytest.fail('No write transaction may start')
    with readonly.transaction() as db:
        import sqlite3
        with pytest.raises(sqlite3.OperationalError):
            db.execute('DELETE FROM usage')
