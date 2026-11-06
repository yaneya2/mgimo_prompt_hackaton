"""Validate contract fixtures and important negative cases; no API calls."""
import copy
import json
from pathlib import Path
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1] / 'contracts'
schema = json.loads((ROOT / 'schema.json').read_text())
defs = schema['$defs']

def validate(name, value):
    Draft202012Validator({'$defs': defs, '$ref': '#/$defs/' + name}, format_checker=FormatChecker()).validate(value)

def check_snapshot(s):
    validate('Snapshot', s)
    data = s['data']
    names = {'capability': 'capabilities', 'source': 'sources', 'claim': 'claims', 'option': 'options', 'message': 'messages', 'experiment': 'experiments', 'decision': 'decisions', 'issue': 'issues'}
    indexes = {kind: {v['id']: v for v in data[collection]} for kind, collection in names.items()}
    for kind, collection in names.items():
        assert len(indexes[kind]) == len(data[collection]), 'Duplicate entity ID'
        for value in data[collection]:
            assert value['project_id'] == s['project']['id'], 'Cross-project entity'
    assert s['run']['project_id'] == s['brief']['project_id'] == s['project']['id']
    assert s['run']['brief_version'] == s['brief']['version']
    def resolve(kind, id, version=None):
        assert id in indexes[kind], 'Unknown ' + kind + ': ' + id
        item = indexes[kind][id]
        assert version is None or item['version'] == version, 'Wrong reference version'
        return item
    def evidence(item):
        source = resolve('source', item['source_id'], item['source_version'])
        assert source['verification'] == 'read', 'Unread evidence'
        assert item['quote'] in source['content_excerpt'], 'Missing quote in saved excerpt'
    for claim in data['claims']:
        for ev in claim['evidence']:
            evidence(ev)
        if claim['kind'] == 'external_fact' and claim['verification'] == 'supported':
            assert any(ev['relation'] == 'supports' for ev in claim['evidence']), 'Fact without support'
    for capability in data['capabilities']:
        for id in capability['source_ids']:
            resolve('source', id)
    for option in data['options']:
        for id in option['capability_ids']:
            resolve('capability', id)
        for id in option['claim_ids']:
            resolve('claim', id)
    for message in data['messages']:
        resolve('option', message['option_id'])
        for id in message['claim_ids']:
            resolve('claim', id)
        for id in message['capability_ids']:
            capability = resolve('capability', id)
            if message['status'] == 'allowed':
                assert capability['status'] == 'available', 'Unavailable promise'
    for experiment in data['experiments']:
        resolve('option', experiment['option_id'])
        for id in experiment['message_ids']:
            message = resolve('message', id)
            assert message['option_id'] == experiment['option_id']
    for decision in data['decisions']:
        resolve('option', decision['option_id'])
    for issue in data['issues']:
        target = issue['target']
        resolve(target['kind'], target['id'], target['version'])
        for ev in issue['evidence']:
            evidence(ev)
    artifacts = {(a['id'], a['version']): a for a in s['artifacts']}
    def visit(key, ancestors):
        assert key in artifacts, 'Missing artifact dependency'
        assert key not in ancestors, 'Cyclic artifacts'
        for dep in artifacts[key]['depends_on']:
            visit((dep['artifact_id'], dep['version']), ancestors | {key})
    for artifact in s['artifacts']:
        visit((artifact['id'], artifact['version']), set())
        assert artifact['project_id'] == s['project']['id']
        for target in artifact['entities']:
            item = resolve(target['kind'], target['id'], target['version'])
            if artifact['freshness'] == 'current':
                assert item['brief_version'] == artifact['brief_version'] == s['run']['brief_version']
    if s['run']['quality'] == 'ready':
        assert not any(a['freshness'] == 'stale' for a in s['artifacts']), 'Ready with stale data'
        assert not any(m['status'] == 'blocked' for m in data['messages']), 'Ready with blocked promise'
        assert not any(i['severity'] == 'blocking' and i['status'] != 'resolved' for i in data['issues']), 'Ready with blocking issue'

for name in defs:
    Draft202012Validator.check_schema({'$defs': defs, '$ref': '#/$defs/' + name})
special = {'stage_request': 'StageRequest', 'stage_result': 'StageResult', 'api_error': 'ErrorResponse'}
fixtures = {}
for path in sorted((ROOT / 'examples').glob('*.json')):
    value = json.loads(path.read_text())
    fixtures[path.stem] = value
    if path.stem in special:
        validate(special[path.stem], value)
    else:
        check_snapshot(value)

api = json.loads((ROOT / 'openapi.json').read_text())
operations = set()
def walk(node):
    if isinstance(node, dict):
        if '$ref' in node:
            prefix = './schema.json#/$defs/'
            assert node['$ref'].startswith(prefix)
            assert node['$ref'][len(prefix):] in defs
        for value in node.values():
            walk(value)
    elif isinstance(node, list):
        for value in node:
            walk(value)
walk(api)
for path, methods in api['paths'].items():
    for method, op in methods.items():
        assert op['operationId'] not in operations
        operations.add(op['operationId'])
        assert {p['name'] for p in op['parameters'] if p['in'] == 'path'} == {p[1:-1] for p in path.split('/') if p.startswith('{')}
        if method == 'post':
            assert any(p['name'] == 'Idempotency-Key' and p['required'] for p in op['parameters'])

cases = []
bad = copy.deepcopy(fixtures['success']); bad['run']['status'] = 'finished'; cases.append(('unknown status', 'Snapshot', bad))
bad = copy.deepcopy(fixtures['success']); bad['brief']['input']['extra'] = True; cases.append(('unknown field', 'Snapshot', bad))
bad = copy.deepcopy(fixtures['success']); bad['data']['claims'][0]['evidence'][0]['source_id'] = 'source_missing'; cases.append(('dangling evidence', 'Snapshot', bad))
bad = copy.deepcopy(fixtures['success']); bad['data']['claims'][0].update(kind='external_fact', evidence=[]); cases.append(('unsupported fact', 'Snapshot', bad))
bad = copy.deepcopy(fixtures['stale_result']); bad['run']['quality'] = 'ready'; cases.append(('stale ready result', 'Snapshot', bad))
bad = copy.deepcopy(fixtures['unsupported_promise']); bad['run']['quality'] = 'ready'; cases.append(('blocked ready result', 'Snapshot', bad))
bad = copy.deepcopy(fixtures['stage_result']); bad['questions'] = []; cases.append(('empty clarification', 'StageResult', bad))
bad = copy.deepcopy(fixtures['stage_result']); bad['outcome'] = 'failed'; cases.append(('failure without error', 'StageResult', bad))
for label, name, value in cases:
    try:
        check_snapshot(value) if name == 'Snapshot' else validate(name, value)
    except (AssertionError, __import__('jsonschema').ValidationError):
        continue
    raise AssertionError('Negative case accepted: ' + label)
print(f'PASS: {len(defs)} schemas; {len(fixtures)} examples; {len(operations)} API operations and references; {len(cases)} negative cases.')
print('Checks cover structure and fixture integrity, not factual truth or a running API.')
