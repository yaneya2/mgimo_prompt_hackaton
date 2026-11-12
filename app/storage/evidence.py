"""One consistent technical observation, excluding briefs, answers and provider bodies."""
import json

from app.storage.database import now
from app.storage.runtime import summary_from_connection


def run_evidence(storage, run_id):
    with storage.transaction() as db:
        run = storage._run(db, run_id)
        summary = summary_from_connection(storage, db, run_id)
        if summary['stop_reason']:
            summary['stop_reason'] = {'code': summary['stop_reason']['code']}
        checkpoint = storage._payload(db, 'SELECT payload FROM checkpoints WHERE id = ?',
                                      (run['checkpoint_id'],)) if run['checkpoint_id'] else {}
        state = checkpoint.get('graph_state', {})
        events = [dict(row) for row in db.execute('SELECT * FROM run_events WHERE run_id = ? ORDER BY rowid', (run_id,))]
        inputs = [dict(row) for row in db.execute(
            'SELECT id, kind, created_at FROM run_inputs WHERE run_id = ? ORDER BY rowid', (run_id,))]
        jobs = [dict(row) for row in db.execute(
            'SELECT id, stage, state, created_at, finished_at FROM jobs WHERE run_id = ? ORDER BY rowid', (run_id,))]
        usage = []
        for value in storage._usage(db, run_id):
            record = {key: value.get(key) for key in (
                'id', 'call_id', 'attempt', 'stage', 'provider', 'model', 'tool', 'started_at', 'duration_ms',
                'input_tokens', 'output_tokens', 'cost', 'cost_basis', 'pricing_version', 'outcome', 'cached', 'cache_origin_id')}
            context = value.get('context', {})
            record['versions'] = {key: context.get(key) for key in ('prompt_version', 'schema_version', 'prompt_hash', 'schema_hash')}
            usage.append(record)
        error = run.get('error')
        metadata = {key: run[key] for key in ('id', 'project_id', 'brief_version', 'revision', 'status', 'stage', 'quality', 'created_at', 'updated_at')}
        metadata['error'] = {key: error['error'][key] for key in ('code', 'retryable')} if error else None
        return dict(format_version='1.0.0', captured_at=now(), run=metadata,
                    execution_revision=None, synthetic=state.get('registry', {}).get('synthetic'),
                    registry=state.get('registry'), summary=summary, usage=usage, events=events,
                    jobs=jobs, recorded_inputs=inputs, repairs_used=state.get('repairs_used', 0))
