"""Reuse A1 schemas without allowing a model to own platform metadata."""
from copy import deepcopy
import hashlib
import json
import re
from pathlib import Path

from app.storage.validation import COLLECTIONS

ROOT = Path(__file__).resolve().parents[2]
DEFINITIONS = json.loads((ROOT / 'contracts/schema.json').read_text())['$defs']
KINDS = {'capabilities': 'Capability', 'claims': 'Claim', 'options': 'StrategyOption',
         'messages': 'Message', 'experiments': 'Experiment', 'issues': 'ReviewIssue'}
OWNED = {'brief': ['capabilities'], 'research': ['claims'], 'options': ['options'],
         'communications': ['messages', 'experiments'], 'review': ['issues']}
META = {'version', 'project_id', 'brief_version'}


def response_schema(stage):
    properties = {'summary': {'type': 'string', 'minLength': 1},
                  'questions': {'type': 'array', 'items': {'$ref': '#/$defs/Question'}}}
    for collection in OWNED[stage]:
        schema = deepcopy(DEFINITIONS[KINDS[collection]])
        schema['required'] = [key for key in schema['required'] if key not in META]
        schema['properties'] = {key: value for key, value in schema['properties'].items() if key not in META}
        properties[collection] = {'type': 'array', 'items': schema, 'maxItems': 3 if collection == 'options' else 30}
        if collection == 'options':
            properties[collection]['minItems'] = 3
    schema = {'type': 'object', 'additionalProperties': False, 'required': list(properties),
              'properties': properties}
    needed = {}
    def collect(node):
        if isinstance(node, dict):
            if '$ref' in node:
                name = node['$ref'].split('/')[-1]
                if name not in needed:
                    needed[name] = DEFINITIONS[name]
                    collect(needed[name])
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)
    collect(schema)
    schema['$defs'] = needed
    return schema


def identity(label):
    return 'agent_' + hashlib.sha256(label.encode()).hexdigest()[:28]


def stamp(request, collection, value):
    value = deepcopy(value)
    old = next((item for item in request['input_data'][collection] if item['id'] == value['id']), None)
    value.update(project_id=request['project_id'], brief_version=request['brief']['version'])
    if old and all(old.get(key) == item for key, item in value.items()):
        value['version'] = old['version']
    else:
        value['version'] = old['version'] + 1 if old else 1
    return value


def ref(collection, value):
    kind = next(kind for kind, name in COLLECTIONS.items() if name == collection)
    return dict(kind=kind, id=value['id'], version=value['version'])


def namespace_payload(request, payload):
    """A1 versions are project-scoped while new A5 runs start empty: namespace new IDs by run."""
    payload = deepcopy(payload)
    def mapped(collection, value):
        if any(item['id'] == value for item in request['input_data'][collection]):
            return value
        return identity(request['run_id'] + ':' + collection + ':' + value)
    aliases = {item['id']: mapped(collection, item['id'])
               for collection in OWNED[request['stage']] for item in payload[collection]}
    if aliases:
        tokens = re.compile(r'(?<![a-z0-9_-])(' + '|'.join(re.escape(key) for key in sorted(aliases, key=len, reverse=True)) + r')(?![a-z0-9_-])')
        payload['summary'] = tokens.sub(lambda match: aliases[match.group(0)], payload['summary'])
    for collection in OWNED[request['stage']]:
        for item in payload[collection]:
            item['id'] = mapped(collection, item['id'])
            for field, target in (('option_id', 'options'),):
                if field in item:
                    item[field] = mapped(target, item[field])
            for field, target in (('capability_ids', 'capabilities'), ('claim_ids', 'claims'), ('message_ids', 'messages')):
                if field in item:
                    item[field] = [mapped(target, value) for value in item[field]]
            if 'target' in item:
                target = item['target']
                target['id'] = mapped(COLLECTIONS[target['kind']], target['id'])
    return payload
