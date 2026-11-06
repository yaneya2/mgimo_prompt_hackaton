"""Build the proposed v1 contracts and synthetic fixtures. No external services."""
import copy
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'contracts'
S = {'type': 'string', 'minLength': 1}
I = {'type': 'integer', 'minimum': 1}
N = {'type': 'integer', 'minimum': 0}
DT = {'type': 'string', 'format': 'date-time'}
ID = {'type': 'string', 'pattern': '^[a-z][a-z0-9_-]{1,127}$'}
STAGES = ['brief', 'research', 'options', 'communications', 'review', 'selection', 'report']
STATUS = ['queued', 'running', 'pause_requested', 'paused', 'waiting_for_input', 'completed', 'failed', 'incomplete']

def enum(*values):
    return {'type': 'string', 'enum': list(values)}

def ref(name):
    return {'$ref': '#/$defs/' + name}

def arr(item):
    return {'type': 'array', 'items': item}

def nullable(item):
    return {'anyOf': [item, {'type': 'null'}]}

def obj(**fields):
    return {'type': 'object', 'additionalProperties': False,
            'required': list(fields), 'properties': fields}

def entity(**fields):
    return obj(id=ID, version=I, project_id=ID, brief_version=I, **fields)

D = {}
D['Money'] = obj(amount={'type': 'string', 'pattern': r'^\d+(\.\d{1,6})?$'}, currency={'type': 'string', 'pattern': '^[A-Z]{3}$'})
D['EntityRef'] = obj(kind=enum('capability', 'source', 'claim', 'option', 'message', 'experiment', 'decision', 'issue'), id=ID, version=I)
D['Project'] = obj(id=ID, name=S, created_at=DT, current_brief_version=I)
D['BriefInput'] = obj(product=S, goal=S, audience=arr(S), horizon=S, budget=nullable(ref('Money')), constraints=arr(S), assumptions=arr(S), unknowns=arr(S), excluded_source_ids=arr(ID))
D['BriefVersion'] = obj(project_id=ID, version=I, created_at=DT, author=enum('user', 'agent'), input=ref('BriefInput'))
D['Capability'] = entity(name=S, status=enum('available', 'planned', 'hypothesis'), basis=S, source_ids=arr(ID))
D['Source'] = entity(kind=enum('web', 'user_text', 'user_file'), title=S, url=nullable({'type': 'string', 'format': 'uri'}), publisher=nullable(S), published_at=nullable(DT), retrieved_at=DT, content_excerpt=nullable(S), locator=nullable(S), verification=enum('read', 'unavailable'), origin_note=S)
D['Source']['allOf'] = [
    {'if': {'properties': {'kind': {'const': 'web'}}}, 'then': {'properties': {'url': {'type': 'string', 'format': 'uri'}}}},
    {'if': {'properties': {'verification': {'const': 'read'}}}, 'then': {'properties': {'content_excerpt': S, 'locator': S}}}
]
D['Evidence'] = obj(source_id=ID, source_version=I, quote=S, locator=S, relation=enum('supports', 'contradicts', 'context'))
D['Claim'] = entity(text=S, kind=enum('external_fact', 'user_provided', 'hypothesis', 'forecast'), verification=enum('supported', 'disputed', 'unverified'), evidence=arr(ref('Evidence')), limitations=arr(S))
D['StrategyOption'] = entity(name=S, segment=S, problem=S, value=S, priorities=arr(S), capability_ids=arr(ID), claim_ids=arr(ID), channels=arr(S), resource_assumptions=arr(S), objections=arr(S), failure_condition=S, rationale=S)
D['Message'] = entity(option_id=ID, text=S, capability_ids=arr(ID), claim_ids=arr(ID), channel=S, status=enum('allowed', 'hypothesis_only', 'blocked'), rationale=S)
D['Experiment'] = entity(option_id=ID, message_ids=arr(ID), hypothesis=S, method=S, metric=S, baseline=nullable(S), decision_threshold=S, threshold_basis=S, duration=S, owner=nullable(S), data_requirements=arr(S), stop_condition=S)
D['Decision'] = entity(option_id=ID, action=enum('select', 'reject', 'request_revision'), author=enum('user', 'agent'), rationale=S, created_at=DT)
D['ReviewIssue'] = entity(code=S, severity=enum('blocking', 'warning', 'info'), target=ref('EntityRef'), explanation=S, evidence=arr(ref('Evidence')), suggested_fix=S, return_to=enum(*STAGES), status=enum('open', 'resolved', 'accepted_limitation'))
D['Usage'] = obj(id=ID, run_id=ID, call_id=ID, attempt=I, stage=enum(*STAGES), provider=S, model=nullable(S), tool=nullable(S), started_at=DT, duration_ms=N, input_tokens=nullable(N), output_tokens=nullable(N), cost=nullable(ref('Money')), cost_basis=enum('reported', 'estimated', 'unknown'), pricing_version=nullable(S), outcome=enum('success', 'error'), cached={'type': 'boolean'})
D['Question'] = obj(id=ID, stage=enum(*STAGES), text=S, required={'type': 'boolean'}, options=arr(S))
D['ErrorResponse'] = obj(error=obj(code=enum('VALIDATION_ERROR', 'NOT_FOUND', 'VERSION_CONFLICT', 'INVALID_STATE', 'RUN_ALREADY_ACTIVE', 'UPSTREAM_UNAVAILABLE', 'BUDGET_EXCEEDED', 'INTERNAL_ERROR'), message=S, retryable={'type': 'boolean'}, request_id=ID, run_id=nullable(ID), field_errors=arr(obj(path=S, message=S))))
D['Run'] = obj(id=ID, project_id=ID, brief_version=I, revision=I, status=enum(*STATUS), stage=enum(*STAGES), quality=enum('draft', 'blocked', 'ready'), created_at=DT, updated_at=DT, checkpoint_id=nullable(ID), attempt=N, error=nullable(ref('ErrorResponse')))
D['Artifact'] = obj(id=ID, version=I, project_id=ID, run_id=ID, brief_version=I, schema_version={'const': '1.0.0'}, stage=enum(*STAGES), prompt_version=S, model_id=nullable(S), created_at=DT, freshness=enum('current', 'stale'), depends_on=arr(obj(artifact_id=ID, version=I)), entities=arr(ref('EntityRef')), summary=S)
collections = {'capabilities': 'Capability', 'sources': 'Source', 'claims': 'Claim', 'options': 'StrategyOption', 'messages': 'Message', 'experiments': 'Experiment', 'decisions': 'Decision', 'issues': 'ReviewIssue'}
D['DataBundle'] = obj(**{k: arr(ref(v)) for k, v in collections.items()})
D['Limits'] = obj(max_model_calls=I, max_search_calls=I, max_repair_cycles=N, timeout_seconds=I, budget=nullable(ref('Money')))
D['StageRequest'] = obj(schema_version={'const': '1.0.0'}, run_id=ID, project_id=ID, stage=enum(*STAGES), brief=ref('BriefVersion'), input_data=ref('DataBundle'), input_artifacts=arr(ref('Artifact')), limits=ref('Limits'), prompt_version=S, model_id=S)
D['StageResult'] = obj(schema_version={'const': '1.0.0'}, run_id=ID, project_id=ID, brief_version=I, stage=enum(*STAGES), outcome=enum('success', 'needs_input', 'needs_revision', 'incomplete', 'failed'), data=ref('DataBundle'), artifacts=arr(ref('Artifact')), questions=arr(ref('Question')), suggested_next_stage=nullable(enum(*STAGES)), usage_ids=arr(ID), error=nullable(ref('ErrorResponse')))
D['StageResult']['allOf'] = [
    {'if': {'properties': {'outcome': {'const': 'failed'}}}, 'then': {'properties': {'error': ref('ErrorResponse')}}, 'else': {'properties': {'error': {'type': 'null'}}}},
    {'if': {'properties': {'outcome': {'const': 'needs_input'}}}, 'then': {'properties': {'questions': {'minItems': 1}}}}
]
D['Snapshot'] = obj(schema_version={'const': '1.0.0'}, project=ref('Project'), brief=ref('BriefVersion'), run=ref('Run'), data=ref('DataBundle'), artifacts=arr(ref('Artifact')), questions=arr(ref('Question')), usage=arr(ref('Usage')))
D['CreateProject'] = obj(name=S, brief=ref('BriefInput'))
D['CreateRun'] = obj(brief_version=I, limits=ref('Limits'))
D['RunCommand'] = obj(expected_revision=I)
D['ReviseBrief'] = obj(expected_brief_version=I, brief=ref('BriefInput'))
D['AnswerQuestions'] = obj(expected_revision=I, answers=arr(obj(question_id=ID, value=S)))
D['ChooseOption'] = obj(expected_revision=I, option_id=ID, option_version=I, rationale=S)
D['ExcludeSource'] = obj(expected_revision=I, source_id=ID, source_version=I, reason=S)
D['ExportRequest'] = obj(run_id=ID, expected_revision=I, format=enum('markdown', 'html'), allow_draft={'type': 'boolean'})
D['Export'] = obj(id=ID, run_id=ID, run_revision=I, brief_version=I, quality=enum('draft', 'blocked', 'ready'), format=enum('markdown', 'html'), download_url={'type': 'string', 'format': 'uri-reference'})

def write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')

write(OUT / 'schema.json', {'$schema': 'https://json-schema.org/draft/2020-12/schema', 'title': 'Strategy system contracts 1.0.0 (proposed)', '$defs': D})

def api_ref(name):
    return {'$ref': './schema.json#/$defs/' + name}

def response(name, description):
    return {'description': description, 'content': {'application/json': {'schema': api_ref(name)}}}

paths = {}
def endpoint(path, method, operation, output, request=None, code='200', array=False):
    params = [{'name': p[1:-1], 'in': 'path', 'required': True, 'schema': ID} for p in path.split('/') if p.startswith('{')]
    if method == 'post':
        params.append({'name': 'Idempotency-Key', 'in': 'header', 'required': True, 'schema': S})
    success = response(output, 'Success')
    if array:
        success['content']['application/json']['schema'] = arr(api_ref(output))
    op = {'operationId': operation, 'parameters': params, 'responses': {code: success}}
    for status in ['404', '409', '422', '503']:
        op['responses'][status] = response('ErrorResponse', 'Structured error; see contracts/README.md')
    if request:
        op['requestBody'] = {'required': True, 'content': {'application/json': {'schema': api_ref(request)}}}
    paths.setdefault(path, {})[method] = op

endpoint('/projects', 'post', 'createProject', 'Project', 'CreateProject', '201')
endpoint('/projects', 'get', 'listProjects', 'Project', array=True)
endpoint('/projects/{project_id}/brief', 'get', 'getCurrentBrief', 'BriefVersion')
endpoint('/projects/{project_id}/brief-versions', 'post', 'reviseBrief', 'BriefVersion', 'ReviseBrief', '201')
endpoint('/projects/{project_id}/runs', 'post', 'createRun', 'Run', 'CreateRun', '202')
endpoint('/projects/{project_id}/runs', 'get', 'listRuns', 'Run', array=True)
endpoint('/runs/{run_id}', 'get', 'getRun', 'Run')
endpoint('/runs/{run_id}/snapshot', 'get', 'getSnapshot', 'Snapshot')
endpoint('/runs/{run_id}/artifacts', 'get', 'listArtifacts', 'Artifact', array=True)
endpoint('/runs/{run_id}/usage', 'get', 'listUsage', 'Usage', array=True)
for action, request in [('pause', 'RunCommand'), ('resume', 'RunCommand'), ('answers', 'AnswerQuestions'), ('selection', 'ChooseOption'), ('exclude-source', 'ExcludeSource')]:
    endpoint('/runs/{run_id}/' + action, 'post', action.replace('-', '_') + 'Run', 'Run', request, '202')
endpoint('/projects/{project_id}/exports', 'post', 'createExport', 'Export', 'ExportRequest', '201')
paths['/exports/{export_id}/download'] = {'get': {'operationId': 'downloadExport', 'parameters': [{'name': 'export_id', 'in': 'path', 'required': True, 'schema': ID}], 'responses': {'200': {'description': 'Rendered artifact', 'content': {'text/markdown': {'schema': {'type': 'string'}}, 'text/html': {'schema': {'type': 'string'}}}}, '404': response('ErrorResponse', 'Export not found')}}}
write(OUT / 'openapi.json', {'openapi': '3.1.0', 'info': {'title': 'Strategy API — proposed contract', 'version': '1.0.0', 'description': 'Specification only. Server implementation belongs to A6.'}, 'servers': [{'url': '/api/v1'}], 'paths': paths})

T = '2026-11-06T12:00:00Z'
def e(id, **fields):
    return dict(id=id, version=1, project_id='project_demo', brief_version=1, **fields)

brief = dict(product='Условный сервис расписания для преподавателей', goal='Выбрать первый сегмент и проверить спрос', audience=['Частные преподаватели'], horizon='Шесть недель', budget={'amount': '100000', 'currency': 'RUB'}, constraints=['Нет интеграции с мессенджерами'], assumptions=['Все данные примера синтетические'], unknowns=['Частота пропусков занятий'], excluded_source_ids=[])
data = {k: [] for k in collections}
data['capabilities'] = [e('cap_email', name='Напоминания по электронной почте', status='available', basis='Условная карточка продукта', source_ids=['source_user'])]
data['sources'] = [e('source_user', kind='user_text', title='Карточка условного продукта', url=None, publisher=None, published_at=None, retrieved_at=T, content_excerpt='Доступны напоминания по электронной почте.', locator='Строка 1', verification='read', origin_note='Синтетический пример, не результат исследования')]
evidence = dict(source_id='source_user', source_version=1, quote='Доступны напоминания по электронной почте.', locator='Строка 1', relation='supports')
data['claims'] = [e('claim_email', text='В продукте есть email-напоминания', kind='user_provided', verification='supported', evidence=[evidence], limitations=['Данные пользователя, независимо не проверены'])]
for num, segment, channel in [(1, 'Частные преподаватели', 'Профессиональное сообщество'), (2, 'Небольшие школы', 'Прямые интервью'), (3, 'Партнёры преподавателей', 'Партнёрские рекомендации')]:
    data['options'].append(e(f'option_{num}', name=f'Направление {num}', segment=segment, problem='Ручное напоминание о занятиях', value='Отправка email-напоминаний', priorities=['Проверить спрос в сегменте'], capability_ids=['cap_email'], claim_ids=['claim_email'], channels=[channel], resource_assumptions=['Ресурс на пилот требует уточнения'], objections=['Пользователи могут предпочитать мессенджеры'], failure_condition='В пилоте нет готовности пользоваться', rationale='Гипотеза направления, требует проверки'))
data['messages'] = [e('message_email', option_id='option_1', text='Отправляйте напоминания о занятиях по email', capability_ids=['cap_email'], claim_ids=['claim_email'], channel='Профессиональное сообщество', status='allowed', rationale='Соответствует условной карточке продукта')]
data['experiments'] = [e('experiment_pilot', option_id='option_1', message_ids=['message_email'], hypothesis='Преподавателям нужна автоматизация напоминаний', method='Интервью и пробный запуск', metric='Число участников, продолживших использование', baseline=None, decision_threshold='Не менее 3 из 5 участников хотят продолжить', threshold_basis='Проектное допущение для малого пилота, не статистический вывод', duration='Две недели', owner=None, data_requirements=['Согласие на участие', 'Факт повторного использования'], stop_condition='Сбой отправки или просьба участника остановить пилот')]
data['decisions'] = [e('decision_1', option_id='option_1', action='select', author='user', rationale='Начать с наиболее доступного сегмента', created_at=T)]
artifact = dict(id='artifact_report', version=1, project_id='project_demo', run_id='run_demo', brief_version=1, schema_version='1.0.0', stage='report', prompt_version='report-v1', model_id=None, created_at=T, freshness='current', depends_on=[], entities=[{'kind': 'option', 'id': 'option_1', 'version': 1}], summary='Синтетический пример отчёта: выбран пилот с частными преподавателями.')
base = dict(schema_version='1.0.0', project=dict(id='project_demo', name='Демонстрационный пример', created_at=T, current_brief_version=1), brief=dict(project_id='project_demo', version=1, created_at=T, author='user', input=brief), run=dict(id='run_demo', project_id='project_demo', brief_version=1, revision=1, status='completed', stage='report', quality='ready', created_at=T, updated_at=T, checkpoint_id='checkpoint_1', attempt=0, error=None), data=data, artifacts=[artifact], questions=[], usage=[])
fixtures = {'success': base}
missing = copy.deepcopy(base)
missing['run'].update(status='waiting_for_input', stage='brief', quality='draft')
missing['brief']['input']['budget'] = None
missing['brief']['input']['unknowns'].append('Бюджет запуска')
missing['data'] = {k: [] for k in collections}
missing['artifacts'] = []
missing['questions'] = [dict(id='question_budget', stage='brief', text='Каков бюджет запуска?', required=True, options=[])]
fixtures['needs_input'] = missing
conflict = copy.deepcopy(base)
conflict['run'].update(status='incomplete', stage='review', quality='blocked')
conflict['data']['sources'].append(e('source_other', kind='user_text', title='Другая версия карточки', url=None, publisher=None, published_at=None, retrieved_at=T, content_excerpt='Email-напоминания пока не реализованы.', locator='Строка 1', verification='read', origin_note='Синтетический конфликт'))
counter = dict(source_id='source_other', source_version=1, quote='Email-напоминания пока не реализованы.', locator='Строка 1', relation='contradicts')
conflict['data']['claims'][0]['verification'] = 'disputed'
conflict['data']['claims'][0]['evidence'].append(counter)
conflict['data']['messages'][0]['status'] = 'blocked'
conflict['data']['issues'] = [e('issue_conflict', code='CONFLICTING_EVIDENCE', severity='blocking', target={'kind': 'claim', 'id': 'claim_email', 'version': 1}, explanation='Карточки противоречат друг другу', evidence=[counter], suggested_fix='Уточнить статус функции', return_to='research', status='open')]
fixtures['conflicting_sources'] = conflict
blocked = copy.deepcopy(base)
blocked['run'].update(status='incomplete', stage='review', quality='blocked')
blocked['data']['capabilities'].append(e('cap_messenger', name='Напоминания в мессенджере', status='planned', basis='Условный план разработки', source_ids=[]))
blocked['data']['messages'][0].update(text='Получайте напоминания в мессенджере уже сейчас', capability_ids=['cap_messenger'], status='blocked', rationale='Функция только запланирована')
blocked['data']['issues'] = [e('issue_promise', code='UNAVAILABLE_CAPABILITY', severity='blocking', target={'kind': 'message', 'id': 'message_email', 'version': 1}, explanation='Обещана недоступная функция', evidence=[], suggested_fix='Использовать сообщение о доступной email-функции', return_to='communications', status='open')]
fixtures['unsupported_promise'] = blocked
failure = copy.deepcopy(missing)
failure['run'].update(status='failed', stage='research', error={'error': dict(code='UPSTREAM_UNAVAILABLE', message='Поисковый сервис недоступен', retryable=True, request_id='request_demo', run_id='run_demo', field_errors=[])})
failure['questions'] = []
fixtures['search_error'] = failure
stale = copy.deepcopy(base)
stale['project']['current_brief_version'] = 2
stale['brief']['version'] = 2
stale['brief']['input']['budget']['amount'] = '50000'
stale['brief']['input']['constraints'].append('Платная реклама запрещена')
stale['run'].update(status='paused', quality='draft', brief_version=2, revision=2)
stale['artifacts'][0]['freshness'] = 'stale'
fixtures['stale_result'] = stale
for name, value in fixtures.items():
    write(OUT / 'examples' / (name + '.json'), value)
stage_request = dict(schema_version='1.0.0', run_id='run_demo', project_id='project_demo', stage='brief', brief=missing['brief'], input_data=missing['data'], input_artifacts=[], limits=dict(max_model_calls=10, max_search_calls=8, max_repair_cycles=2, timeout_seconds=120, budget=None), prompt_version='brief-v1', model_id='fixture-model')
write(OUT / 'examples' / 'stage_request.json', stage_request)
write(OUT / 'examples' / 'stage_result.json', dict(schema_version='1.0.0', run_id='run_demo', project_id='project_demo', brief_version=1, stage='brief', outcome='needs_input', data=missing['data'], artifacts=[], questions=missing['questions'], suggested_next_stage=None, usage_ids=[], error=None))
write(OUT / 'examples' / 'api_error.json', failure['run']['error'])
print('Generated schema, OpenAPI and 9 synthetic examples in', OUT)
