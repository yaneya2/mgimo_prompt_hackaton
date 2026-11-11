"""B7 invalidation floor and conservative reuse of confirmed stages within one Run."""
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json

from app.storage.database import encode, now
from app.storage.validation import COLLECTIONS

STAGES = ('brief', 'research', 'options', 'communications', 'review', 'selection', 'report')
POLICY = 'b7-v1'
FIRST_AFFECTED = dict(product='brief', assumptions='brief', unknowns='brief',
                      audience='research', goal='research', excluded_source_ids='research',
                      budget='options', horizon='options', constraints='options')


def plan_reuse(db, snapshot, brief):
    changed = sorted(key for key in brief['input'] if snapshot['brief']['input'].get(key) != brief['input'][key])
    cutoff = min((STAGES.index(FIRST_AFFECTED.get(key, 'brief')) for key in changed), default=5)
    current = {(a['id'], a['version']) for a in snapshot['artifacts'] if a['freshness'] == 'current'}
    candidates = {}
    # Only successful outputs still represented by current artifacts are eligible.
    for row in db.execute('SELECT payload, result_json FROM checkpoints WHERE run_id = ? ORDER BY rowid',
                          (snapshot['run']['id'],)):
        checkpoint, result = json.loads(row[0]), json.loads(row[1])
        stage = checkpoint['stage']
        if (stage in ('brief', 'selection', 'report') or STAGES.index(stage) >= cutoff
                or checkpoint['brief_version'] != snapshot['brief']['version']
                or result['outcome'] != 'success' or not checkpoint['graph_state'].get('reuse_key')
                or not result['artifacts']
                or any((a['id'], a['version']) not in current for a in result['artifacts'])):
            continue
        candidates[stage] = checkpoint['id']
    return dict(policy=POLICY, changed_fields=changed, first_affected_stage=STAGES[cutoff], candidates=candidates)


def input_key(request, spec, context):
    if spec.reuse_brief_fields is None:
        return None  # Unknown dependencies never opt in implicitly.
    data = deepcopy({key: request['input_data'][key] for key in spec.reuse_collections})
    for collection, items in data.items():
        items.sort(key=lambda item: item['id'])
        for item in items:
            item.pop('version', None)
            item.pop('brief_version', None)
            # A regenerated user-brief wrapper can have a new timestamp, not new content.
            if collection == 'sources' and item['kind'] == 'user_text':
                item.pop('retrieved_at', None)
            for evidence in item.get('evidence', []):
                evidence.pop('source_version', None)
            if 'target' in item:
                item['target'].pop('version', None)
    artifacts = [dict(id=a['id'], stage=a['stage'], prompt_version=a['prompt_version'], model_id=a['model_id'],
                      summary=a['summary'], entities=[(r['kind'], r['id']) for r in a['entities']],
                      depends_on=[d['artifact_id'] for d in a['depends_on']])
                 for a in request['input_artifacts'] if a['freshness'] == 'current']
    value = dict(policy=POLICY, stage=request['stage'], prompt=spec.prompt_version, model=spec.model_id,
                 fields={key: request['brief']['input'][key] for key in spec.reuse_brief_fields},
                 declarations=[spec.reuse_brief_fields, spec.reuse_collections, spec.reuse_max_age_seconds], data=data,
                 artifacts=sorted(artifacts, key=lambda a: a['id']), inputs=context.inputs,
                 feedback=context.feedback, repairs_used=context.repairs_used)
    return hashlib.sha256(encode(value).encode()).hexdigest()


def reuse_result(storage, request, spec, context, state):
    checkpoint_id = state.get('reuse_plan', {}).get('candidates', {}).get(request['stage'])
    if not checkpoint_id:
        return None
    checkpoint = storage.get_checkpoint(checkpoint_id)
    origin_time = checkpoint['graph_state'].get('reuse_origin_created_at', checkpoint['created_at'])
    times = [origin_time] + [s['retrieved_at'] for s in checkpoint['data']['sources']
                           if s['kind'] != 'user_text' and s['verification'] == 'read']
    current_time = datetime.now(timezone.utc)
    if any(not 0 <= (current_time - datetime.fromisoformat(stamp)).total_seconds() <= spec.reuse_max_age_seconds for stamp in times):
        return None
    key = input_key(request, spec, context)
    if key is None or checkpoint['graph_state'].get('reuse_key') != key:
        return None
    result = deepcopy(storage.get_step_result(checkpoint_id))
    result.update(brief_version=request['brief']['version'], usage_ids=[])
    versions = {(kind, item['id']): item['version'] for kind, collection in COLLECTIONS.items()
                for item in request['input_data'][collection]}
    parents = {a['id']: a['version'] for a in request['input_artifacts'] if a['freshness'] == 'current'}
    with storage.transaction() as db:
        for kind, collection in COLLECTIONS.items():
            for item in result['data'][collection]:
                latest = db.execute('SELECT MAX(version) FROM entities WHERE project_id = ? AND kind = ? AND id = ?',
                                    (request['project_id'], kind, item['id'])).fetchone()[0]
                item.update(version=latest + 1, brief_version=request['brief']['version'])
                versions[kind, item['id']] = item['version']
        for artifact in result['artifacts']:
            previous = dict(artifact_id=artifact['id'], version=artifact['version'], brief_version=artifact['brief_version'])
            latest = db.execute('SELECT MAX(version) FROM artifacts WHERE project_id = ? AND id = ?',
                                (request['project_id'], artifact['id'])).fetchone()[0]
            artifact.update(version=latest + 1, brief_version=request['brief']['version'], created_at=now(),
                            freshness='current', reused_from=previous)
            parents[artifact['id']] = artifact['version']
    try:
        for collection in ('claims', 'issues'):
            for item in result['data'][collection]:
                for evidence in item['evidence']:
                    evidence['source_version'] = versions['source', evidence['source_id']]
        refs = [issue['target'] for issue in result['data']['issues']]
        refs += [ref for artifact in result['artifacts'] for ref in artifact['entities']]
        for ref in refs:
            ref['version'] = versions[ref['kind'], ref['id']]
        for artifact in result['artifacts']:
            for dependency in artifact['depends_on']:
                dependency['version'] = parents[dependency['artifact_id']]
    except KeyError:
        return None  # Changed or absent dependencies require a real stage execution.
    state['reuse_origin_created_at'] = origin_time
    return result
