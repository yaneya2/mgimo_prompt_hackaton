"""Six A5-compatible handlers. Models never write platform state or select for a human."""
from copy import deepcopy
import json
from pathlib import Path

from app.agents.context import effective_context, current_summaries
from app.agents.checks import check_bundle
from app.agents.contracts import ROOT, OWNED, identity, ref, response_schema, stamp, namespace_payload
from app.agents.limits import CallGate, LimitExceeded
from app.agents import report
from app.llm import create_model
from app.llm.base import ModelError
from app.storage.validation import COLLECTIONS, validate
from app.tools.research import ResearchTools, ResearchError, source
from app.tools.selection import extract_user_urls, select_urls, coverage
from app.workflow.results import artifact_for, result_for
from app.workflow.stages import AGENT_STAGES, StageRegistry, StageSpec

RELEVANT = {
    'brief': ['capabilities', 'sources'],
    'research': ['capabilities', 'sources', 'claims', 'issues'],
    'options': ['capabilities', 'claims', 'options', 'issues'],
    'communications': ['capabilities', 'claims', 'options', 'messages', 'experiments', 'issues'],
    'review': list(COLLECTIONS.values()),
}


class AgentRuntime:
    def __init__(self, settings, sink, *, model_factory=create_model, tools_factory=ResearchTools):
        self.settings, self.sink = settings, sink
        self.model_factory, self.tools_factory = model_factory, tools_factory
        self.gate = CallGate(settings.data_dir)

    def generate(self, request, context, stage, *, extra=None, schema=None, instruction=None):
        relevant = {key: request['input_data'][key] for key in RELEVANT[stage]}
        if 'issues' in relevant:
            relevant['issues'] = [self.retarget(i, request['input_data']) for i in relevant['issues']]
        payload = dict(brief=request['brief'],
                       effective_context=effective_context(request['brief'], context.inputs),
                       stage_summaries=current_summaries(request, stage),
                       data=relevant,
                       answers=[item for item in context.inputs if item.get('kind') == 'answers'
                                and item.get('brief_version') == request['brief']['version']],
                       feedback=context.feedback, repairs_used=context.repairs_used, extra=extra)
        content = json.dumps(payload, ensure_ascii=False)
        if len(content) > self.settings.agent_context_chars:
            raise LimitExceeded('Контекст превышает лимит; сократите материалы. Ограничения и доказательства не обрезаны.')
        self.gate.reserve(request, 'model', 1 + (self.settings.model_max_retries if self.settings.model_provider == 'openrouter' else 0))
        system = (ROOT / 'prompts/common-v2.md').read_text() + '\n' + (ROOT / ('prompts/' + stage + '-v2.md')).read_text()
        if instruction:
            system += '\n' + instruction
        with self.model_factory(self.settings, self.sink) as model:
            return model.generate(messages=[dict(role='system', content=system), dict(role='user', content=content)],
                                  schema=schema or response_schema(stage), run_id=request['run_id'], stage=request['stage'],
                                  prompt_version=request['prompt_version'], schema_version='agents-payload-v1')

    def __call__(self, request, context):
        self.gate.begin(request)
        try:
            if request['stage'] == 'report':
                return report.run(request, context)
            return self.execute(request, context)
        except LimitExceeded as exc:
            output = result_for(request, outcome='incomplete')
            output['artifacts'] = [artifact_for(request, str(exc))]
            return output
        finally:
            self.gate.finish(request)

    def execute(self, request, context, *, generated=None, frozen_research=False):
        stage = request['stage']
        request = deepcopy(request)
        extra = {}
        collected = []
        usage_ids = []
        if stage == 'brief':
            text = json.dumps(dict(brief=request['brief']['input'], answers=context.inputs), ensure_ascii=False)
            if len(text) > self.settings.agent_context_chars:
                raise LimitExceeded('Бриф превышает лимит контекста.')
            collected = [source(request, key=request['run_id'] + ':brief', title='Бриф и ответы пользователя',
                                text=text, kind='user_text', note='Данные пользователя; независимая проверка не проводилась.')]
            extra['brief_source_id'] = collected[0]['id']
        if stage == 'research' and not frozen_research:
            tools = self.tools_factory(self.settings, self.sink, self.gate, request)
            errors = []
            try:
                excluded_urls = {s['url'] for s in context.excluded_sources if s['url']}
                excluded_files = {s.get('origin_note', '').split('\n')[0].removeprefix('material-key:')
                                  for s in context.excluded_sources if s['kind'] == 'user_file'}
                for path in self.settings.research_files:
                    try:
                        if Path(path).as_posix() not in excluded_files:
                            collected.append(tools.read_file(path))
                    except ResearchError as exc:
                        errors.append(str(exc))
                urls, url_errors = extract_user_urls(dict(brief=request['brief']['input'],
                    clarifications=[dict(question=c['question'], answer=c['answer'])
                                    for c in effective_context(request['brief'], context.inputs)['clarifications']]))
                errors.extend(url_errors)
                groups = [dict(label='Ссылки пользователя', urls=urls)] if urls else []
                if self.settings.tavily_api_key:
                    plan_schema = dict(type='object', additionalProperties=False, required=['questions', 'queries'],
                                       properties={'questions': {'type': 'array', 'items': {'type': 'string'}, 'maxItems': 4},
                                                   'queries': {'type': 'array', 'items': {'type': 'string', 'maxLength': 500},
                                                               'minItems': 1, 'maxItems': self.settings.research_max_queries}})
                    plan = self.generate(request, context, stage, schema=plan_schema,
                                         instruction='Сейчас верни только план исследования: questions — исследовательские вопросы, queries — запросы к первичным источникам. Не создавай утверждения.')
                    usage_ids += plan.usage_ids
                    extra['research_plan'] = plan.data
                    for number, query in enumerate(plan.data['queries']):
                        group = dict(label=plan.data['questions'][number] if number < len(plan.data['questions']) else query,
                                     query=query, urls=[])
                        try:
                            group['urls'] = [item['url'] for item in tools.search(query)]
                        except ResearchError as exc:
                            errors.append(str(exc))
                        groups.append(group)
                    groups.extend(dict(label=question, urls=[]) for question in plan.data['questions'][len(plan.data['queries']):])
                    excluded = set(request['brief']['input']['excluded_source_ids'])
                    for group in groups:
                        group['urls'] = [url for url in group['urls']
                            if url not in excluded_urls and source(request, key=url, title=url, text='', url=url)['id'] not in excluded]
                    urls = select_urls(groups, self.settings.research_max_sources)
                    try:
                        collected.extend(tools.extract(urls))
                    except ResearchError as exc:
                        errors.append(str(exc))
                    extra['research_coverage'] = coverage(groups, urls, collected)
                    for item in extra['research_coverage']:
                        if item['gap']:
                            errors.append(item['label'] + ': ' + item['gap'])

                else:
                    errors.append('APP_TAVILY_API_KEY не задан: веб-исследование не выполнено.')
            finally:
                tools.close()
            collected = [s for s in collected if s['id'] not in request['brief']['input']['excluded_source_ids']]
            extra['tool_errors'] = errors
            # Merge only actual tool reads into the model's evidence registry.
            all_sources = {s['id']: s for s in request['input_data']['sources']}
            all_sources.update({s['id']: s for s in collected})
            excluded = set(request['brief']['input']['excluded_source_ids'])
            request['input_data']['sources'] = [s for s in all_sources.values() if s['id'] not in excluded]
            if not any(s['verification'] == 'read' and s['kind'] != 'user_text' for s in request['input_data']['sources']):
                output = result_for(request, outcome='incomplete')
                output['data']['sources'] = collected
                coherent = deepcopy(request['input_data'])
                output['data']['claims'] = [stamp(request, 'claims', {k:v for k,v in self.refresh_claim(c, coherent).items() if k != 'version'})
                                            for c in coherent['claims']]
                coherent['claims'] = output['data']['claims']
                output['data']['issues'] = [stamp(request, 'issues', {k:v for k,v in self.retarget(i, coherent).items() if k != 'version'})
                                            for i in coherent['issues']]
                output['artifacts'] = [artifact_for(request, 'Исследование не завершено: ' + '; '.join(errors or ['Нет прочитанных материалов.']))]
                return output
        # The brief source is also visible to the analyst, without altering original BriefVersion.
        if stage == 'brief':
            request['input_data']['sources'] = collected
        generated = generated or self.generate(request, context, stage, extra=extra)
        usage_ids += generated.usage_ids
        payload = namespace_payload(request, generated.data)
        output = result_for(request)
        output['usage_ids'] = usage_ids
        if payload['questions']:
            output['outcome'] = 'needs_input'
            output['questions'] = [dict(question, stage=stage) for question in payload['questions']]
            output['artifacts'] = [artifact_for(request, payload['summary'])]
            return output
        for collection in OWNED[stage]:
            output['data'][collection] = [stamp(request, collection, item) for item in payload[collection]]
            if collection != 'issues':
                old_ids = {item['id'] for item in request['input_data'][collection]}
                if not old_ids <= {item['id'] for item in output['data'][collection]}:
                    raise ModelError('INVALID_RESPONSE', 'Existing objects must be revised rather than silently dropped')
        output['data']['sources'] = collected
        if stage == 'research':
            output['data']['claims'] = [self.refresh_claim(c, request['input_data']) for c in output['data']['claims']]
        if stage == 'brief':
            for cap in output['data']['capabilities']:
                # A model cannot invent independent provenance for a user statement.
                cap['source_ids'] = [collected[0]['id']]
                cap['basis'] = 'Со слов пользователя: ' + cap['basis']
        if stage == 'communications':
            caps = {c['id']: c for c in request['input_data']['capabilities']}
            for message in output['data']['messages']:
                if message['status'] == 'allowed' and any(cid in caps and caps[cid]['status'] != 'available'
                                                         for cid in message['capability_ids']):
                    message['status'] = 'blocked'
        merged = deepcopy(request['input_data'])
        for collection in COLLECTIONS.values():
            items = {item['id']: item for item in merged[collection]}
            items.update({item['id']: item for item in output['data'][collection]})
            merged[collection] = list(items.values())
        if stage == 'review':
            semantic = output['data']['issues']
            # Review outputs list currently detected defects; deterministic defects cannot be dismissed by a model.
            raw_issues = {i['id']: i for i in semantic}
            for issue in check_bundle(merged, request['brief']['input']['excluded_source_ids']):
                raw_issues[issue['id']] = issue
            for old in request['input_data']['issues']:
                if old['id'] not in raw_issues:
                    resolved = deepcopy(old)
                    resolved.pop('version', None)
                    resolved['status'] = 'resolved'
                    resolved['explanation'] += ' Повторная проверка: дефект не воспроизведён.'
                    raw_issues[old['id']] = resolved
            output['data']['issues'] = [stamp(request, 'issues', self.retarget(i, merged)) for i in raw_issues.values()]
            if any(i['severity'] == 'blocking' and i['status'] != 'resolved' for i in output['data']['issues']):
                output['outcome'] = 'needs_revision'
        else:
            # Keep issue references valid after their target receives a new immutable version.
            for old in request['input_data']['issues']:
                updated = self.retarget(old, merged)
                updated.pop('version', None)
                output['data']['issues'].append(stamp(request, 'issues', updated))
        for collection in COLLECTIONS.values():
            if collection != 'sources':
                output['data'][collection] = [stamp(request, collection, {k: v for k, v in item.items() if k != 'version'})
                                               for item in output['data'][collection]]
        entities = [ref(collection, item) for collection in COLLECTIONS.values()
                    if collection != 'issues' or stage == 'review' for item in output['data'][collection]]
        summary = payload['summary']
        if stage == 'research' and extra.get('research_coverage'):
            summary += '\nПокрытие исследовательских вопросов: ' + json.dumps(extra['research_coverage'], ensure_ascii=False)
        if stage == 'research' and extra.get('tool_errors'):
            summary += '\nПробелы исследования: ' + '; '.join(extra['tool_errors'])
        artifact = artifact_for(request, summary, entities=entities)
        artifact['model_id'] = generated.model
        output['artifacts'] = [artifact]
        validate('StageResult', output)
        return output

    @staticmethod
    def reconcile_evidence(value, data):
        value = deepcopy(value)
        sources = {s['id']: s for s in data['sources']}
        kept, lost = [], []
        for evidence in value.get('evidence', []):
            current = sources.get(evidence['source_id'])
            if current and current['verification'] == 'read' and evidence['quote'] in current['content_excerpt']:
                evidence['source_version'] = current['version']
                kept.append(evidence)
            else:
                lost.append(evidence)
        value['evidence'] = kept
        return value, lost

    @staticmethod
    def refresh_claim(claim, data):
        claim, lost = AgentRuntime.reconcile_evidence(claim, data)
        if lost:
            claim['verification'] = 'unverified'
            claim['limitations'] += ['Прежнее доказательство больше не входит в актуальный прочитанный фрагмент: '
                                    + json.dumps(e, ensure_ascii=False) for e in lost]
        return claim

    @staticmethod
    def retarget(issue, data):
        issue, lost = AgentRuntime.reconcile_evidence(issue, data)
        if lost:
            issue['status'] = 'open'
            issue['explanation'] += (' Исходное свидетельство утратило актуальную поддержку; '
                                    'история сохранена в архивной версии замечания: ' + json.dumps(lost, ensure_ascii=False))
        target = issue['target']
        collection = COLLECTIONS[target['kind']]
        current = next((i for i in data[collection] if i['id'] == target['id']), None)
        if current:
            target['version'] = current['version']
        return issue


def build_stages(settings, usage_sink):
    runtime = AgentRuntime(settings, usage_sink)
    model_id = settings.model_name or settings.model_provider + ':default'
    return StageRegistry('strategy-agents-v3', {stage: StageSpec(runtime, stage + '-v3',
                         'python-assembler-v2' if stage == 'report' else model_id) for stage in AGENT_STAGES})
