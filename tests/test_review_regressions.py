"""Reproductions R1–R6 from review; semantic quality remains a separate live acceptance."""
from copy import deepcopy
from dataclasses import replace
import json

import httpx
import pytest

from app.agents.context import effective_context
from app.agents.runtime import AgentRuntime
from app.agents import report
from app.storage import Storage
from app.storage.validation import validate_snapshot
from app.tools.research import ResearchTools, source, canonical_url, ResearchError
from app.tools.selection import extract_user_urls, select_urls, coverage
from app.workflow.results import artifact_for
from app.worker import WorkerService
from scripts.try_workflow import LIMITS
from test_agents import (ROOT, Sink, CONTEXT, request, config, bundle, FixtureModel, real_handlers_with_fixture)


@pytest.mark.parametrize('changed', [False, True])
def test_research_repair_keeps_issue_evidence_valid_and_preserves_original_history(tmp_path, changed):
    store = Storage(tmp_path / 'state')
    project = store.create_project('Research repair', json.loads((ROOT / 'examples/agents/tutor.json').read_text()))
    run = store.create_run(project['id'], dict(LIMITS, timeout_seconds=600))
    registry, model = real_handlers_with_fixture(tmp_path, store)
    path = tmp_path / 'materials/provided.md'
    path.write_text('Исходная цитата: факт со слов пользователя.')
    generate = model.generate
    review_count = []
    def revised(**kwargs):
        result = generate(**kwargs)
        incoming = json.loads(kwargs['messages'][-1]['content'])['data']
        if kwargs['stage'] == 'research':
            read = next(s for s in incoming['sources'] if s['kind'] == 'user_file')
            claim = result.data['claims'][0]
            claim.update(kind='user_provided', verification='supported', evidence=[dict(source_id=read['id'],
                source_version=read['version'], quote=read['content_excerpt'], locator=read['locator'], relation='supports')])
            if 'Исходная цитата' not in read['content_excerpt']:
                claim.update(kind='hypothesis', verification='unverified', evidence=[],
                             limitations=['Исходное свидетельство исчезло; нового подтверждения нет.'])
        if kwargs['stage'] == 'review':
            review_count.append(1)
            if len(review_count) == 1:
                claim = incoming['claims'][0]
                result.data['issues'] = [dict(id='issue_research', code='RECHECK_SOURCE', severity='blocking',
                    target=dict(kind='claim', id=claim['id'], version=claim['version']), explanation='Перепроверить основание.',
                    evidence=deepcopy(claim['evidence']), suggested_fix='Проверить повторным чтением, иначе отказаться от факта.',
                    return_to='research', status='open')]
                if changed:
                    path.write_text('Новый фрагмент: прежняя цитата удалена.')
        return result
    model.generate = revised
    WorkerService(store, registry).run(once=True)
    final = store.get_snapshot(run['id'])
    assert final['run']['status'] == 'waiting_for_input', final['run']
    assert final['run']['stage'] == 'selection' and len(review_count) == 2
    assert not any('diagnostic' in a['prompt_version'] for a in final['artifacts'])
    validate_snapshot(final)
    read = next(s for s in final['data']['sources'] if s['kind'] == 'user_file')
    assert read['version'] == (2 if changed else 1)
    issue = final['data']['issues'][0]
    original = store.get_entity(project['id'], 'issue', issue['id'], 1)
    assert original['evidence'][0]['source_version'] == 1
    assert 'Исходная цитата' in original['evidence'][0]['quote']
    if changed:
        assert issue['evidence'] == [] and 'история сохранена' in issue['explanation']
        assert final['data']['claims'][0]['verification'] == 'unverified'
        assert 'нового подтверждения нет' in final['data']['claims'][0]['limitations'][0]
    else:
        assert issue['evidence'][0]['source_version'] == 1
    assert store.get_checkpoint(final['run']['checkpoint_id'])['graph_state']['repairs_used'] == 1


def test_source_reuses_content_version_but_changed_citation_is_not_blindly_remapped():
    req = request()
    first = source(req, key='doc', title='doc', text='Прежняя цитата', kind='user_file')
    req['input_data']['sources'] = [first]
    assert source(req, key='doc', title='doc', text='Прежняя цитата', kind='user_file') == first
    newer = source(req, key='doc', title='doc', text='Изменённый текст', kind='user_file')
    assert newer['version'] == 2
    data = bundle(); data['sources'] = [newer]
    issue = dict(target=dict(kind='claim', id=data['claims'][0]['id'], version=1), evidence=[dict(source_id=first['id'],
        source_version=1, quote='Прежняя цитата', locator=first['locator'], relation='supports')], status='open', explanation='Проверить')
    updated = AgentRuntime.retarget(issue, data)
    assert updated['evidence'] == [] and updated['status'] == 'open'
    assert issue['evidence'][0]['source_version'] == 1  # Input and archived history immutable.


def test_report_and_roles_use_latest_answers_without_mutating_brief(tmp_path):
    store = Storage(tmp_path / 'state')
    brief = json.loads((ROOT / 'examples/agents/tutor.json').read_text())
    brief.update(budget=None, horizon='Срок неизвестен', unknowns=['Бюджет и срок требуют уточнения'])
    project = store.create_project('Clarifications', brief)
    run = store.create_run(project['id'], dict(LIMITS, timeout_seconds=600))
    registry, model = real_handlers_with_fixture(tmp_path, store)
    original = model.generate
    asked = []
    def generate(**kwargs):
        result = original(**kwargs)
        incoming = json.loads(kwargs['messages'][-1]['content'])
        active = incoming['effective_context']['clarifications']
        if kwargs['stage'] == 'brief' and len(incoming['answers']) < 2:
            qids = ['budget', 'horizon', 'channel'] if not incoming['answers'] else ['budget']
            result.data['questions'] = [dict(id='question_' + key, stage='brief', text={'budget':'Какой бюджет?',
                'horizon':'Какой срок?', 'channel':'Какой канал запрещён?'}[key], required=True, options=[]) for key in qids]
            asked.append(qids)
        if kwargs['stage'] == 'options':
            assert any(c['answer'] == '45678 RUB' for c in active)
            assert any(c['answer'] == 'Три недели' for c in active)
            for option in result.data['options']:
                option.update(channels=['Органические сообщества'], resource_assumptions=['Предел 45678 RUB, без платной рекламы'])
        if kwargs['stage'] == 'communications':
            assert any(c['answer'] == 'Платная реклама запрещена' for c in active)
            for message in result.data['messages']:
                message['channel'] = 'Органические сообщества'
            for experiment in result.data['experiments']:
                experiment['duration'] = 'Три недели'
        return result
    model.generate = generate
    WorkerService(store, registry).run(once=True)
    snap = store.get_snapshot(run['id'])
    store.submit_answers(run['id'], [dict(question_id='question_budget', value='99999 RUB'),
        dict(question_id='question_horizon', value='Три недели'), dict(question_id='question_channel', value='Платная реклама запрещена')],
        expected_revision=snap['run']['revision'])
    WorkerService(store, registry).run(once=True)
    snap = store.get_snapshot(run['id'])
    store.submit_answers(run['id'], [dict(question_id='question_budget', value='45678 RUB')], expected_revision=snap['run']['revision'])
    WorkerService(store, registry).run(once=True)
    snap = store.get_snapshot(run['id']); option = snap['data']['options'][1]
    store.submit_selection(run['id'], option['id'], option['version'], 'Предпочитаю другой сегмент', expected_revision=snap['run']['revision'])
    WorkerService(store, registry).run(once=True)
    final = store.get_snapshot(run['id'])
    assert final['run']['status'] == 'completed', final['run']
    text = next(a['summary'] for a in final['artifacts'] if a['stage'] == 'report')
    assert all(value in text for value in ['45678 RUB', 'Три недели', 'Платная реклама запрещена', 'Какой бюджет?'])
    assert '99999 RUB' not in text and 'Бюджет: null' not in text
    assert final['brief']['input']['budget'] is None and final['brief']['version'] == 1
    assert store.get_brief(project['id'])['input'] == brief
    assert all(m['channel'] == 'Органические сообщества' for m in final['data']['messages'])


def test_urls_are_read_from_original_strings_and_active_clarifications():
    text = {'product':'https://example.org/product\nПродолжение', 'constraints':['https://example.org/a?x=1&y=2\tТекст',
            '(https://example.org/docs).', 'https://example.org/wiki/Test_(topic)'], 'audience':['https://example.org/product']}
    urls, errors = extract_user_urls(text)
    assert urls == ['https://example.org/product', 'https://example.org/a?x=1&y=2',
                    'https://example.org/docs', 'https://example.org/wiki/Test_(topic)']
    assert not errors
    for url in ['https://example.org/a\nwrong', 'https://example.org/a\\nwrong', 'https://example.org/\tbad']:
        with pytest.raises(ResearchError): canonical_url(url)


def test_research_http_sees_urls_from_answers_and_covers_both_queries(tmp_path):
    req = request()
    req['brief']['input']['product'] = 'https://example.org/product\nПродолжение'
    inputs = [dict(kind='answers', brief_version=req['brief']['version'], questions=[dict(id='question_link', text='Ссылка?')],
        payload=dict(answers=[dict(question_id='question_link', value='https://example.org/answer?x=1&y=2\tОписание')]))]
    sent = []; seen = []
    def transport(http_req):
        body = json.loads(http_req.content)
        if http_req.url.path == '/search':
            number = 1 if body['query'] == 'first' else 2
            return httpx.Response(200, json=dict(results=[dict(url=f'https://example.org/q{number}/{i}') for i in range(5)]))
        sent.extend(body['urls'])
        return httpx.Response(200, json=dict(results=[dict(url=url, raw_content='Прочитано ' + url)
            for url in body['urls'] if '/q2/' not in url], failed_results=[]))
    class Model(FixtureModel):
        def generate(self, **kwargs):
            incoming=json.loads(kwargs['messages'][-1]['content']); seen.append(incoming)
            from app.llm.base import ModelResult
            if 'queries' in kwargs['schema']['properties']:
                return ModelResult(dict(questions=['Вопрос 1','Вопрос 2'], queries=['first','second']), 'fixture', None, [])
            return ModelResult(dict(summary='Исследование', questions=[], claims=[]), 'fixture', None, [])
    settings = config(tmp_path, tavily_api_key='fixture', research_max_sources=5)
    factory = lambda settings,sink,gate,request: ResearchTools(settings,sink,gate,request,
        client=httpx.Client(transport=httpx.MockTransport(transport)))
    runtime=AgentRuntime(settings,Sink(),model_factory=Model,tools_factory=factory)
    result=runtime(req,replace(CONTEXT,inputs=inputs))
    assert result['outcome']=='success'
    assert 'https://example.org/product' in sent and 'https://example.org/answer?x=1&y=2' in sent
    assert any('/q1/' in u for u in sent) and any('/q2/' in u for u in sent)
    assert len(sent)==5 and len(set(sent))==5
    covered=seen[-1]['extra']['research_coverage']
    assert covered[-1]['selected'] and not covered[-1]['read'] and 'Extract' in covered[-1]['gap']
    assert 'Вопрос 2' in result['artifacts'][0]['summary']


def test_source_selection_reports_uncovered_questions_and_shared_candidates():
    groups=[dict(label='user',urls=['https://example.org/shared']),dict(label='q1',urls=['https://example.org/shared','https://example.org/a']),
            dict(label='q2',urls=['https://example.org/b'])]
    selected=select_urls(groups,2)
    assert selected==['https://example.org/shared','https://example.org/a']
    read=[dict(url=u,verification='read',origin_note='') for u in selected]
    assert 'лимит' in coverage(groups,selected,read)[-1]['gap']
    assert coverage([dict(label='failed',urls=['https://example.org/b'])], ['https://example.org/b'],
                    [dict(url='https://example.org/a',verification='read',origin_note='Зеркало: https://example.org/bb')])[0]['gap']


def test_current_recommendation_reaches_critic_and_report_without_stale_alternative(tmp_path):
    req=request('review'); req['input_data']=bundle()
    option_request=dict(req,stage='options')
    current=artifact_for(option_request,'RECOMMEND_CURRENT: вариант 1; отказ от 2/3; SENSITIVITY: при смене бюджета выбрать 2.')
    stale=deepcopy(current);stale.update(version=2,freshness='stale',summary='STALE_COMPARISON')
    foreign=deepcopy(current);foreign.update(brief_version=99,summary='WRONG_BRIEF')
    req['input_artifacts']=[current,stale,foreign]
    captured=[]
    class Capture(FixtureModel):
        def generate(self,**kwargs):
            captured.append(kwargs['messages'][-1]['content'])
            return super().generate(**kwargs)
    AgentRuntime(config(tmp_path),Sink(),model_factory=Capture)(req,CONTEXT)
    assert 'RECOMMEND_CURRENT' in captured[0] and 'SENSITIVITY' in captured[0]
    assert 'STALE_COMPARISON' not in captured[0] and 'WRONG_BRIEF' not in captured[0]
    req['stage']='report'
    req['input_data']['decisions'][0].update(option_id='option_2',rationale='USER_CHOICE_DIFFERENT')
    text=report.run(req,CONTEXT)['artifacts'][0]['summary']
    assert all(marker in text for marker in ['RECOMMEND_CURRENT','SENSITIVITY','USER_CHOICE_DIFFERENT','Направление 2'])
    assert 'STALE_COMPARISON' not in text and 'WRONG_BRIEF' not in text


@pytest.mark.parametrize('persistent', [False, True])
def test_full_evaluation_repairs_or_preserves_blocking_defect_using_production_router(tmp_path, persistent):
    from scripts.compare_agents import compare
    counts = {}
    class CorrectableModel(FixtureModel):
        def generate(self, **kwargs):
            result = super().generate(**kwargs)
            if kwargs['stage'] == 'communications':
                run_id = kwargs['run_id']; counts[run_id] = counts.get(run_id, 0) + 1
                if persistent or counts[run_id] == 1:
                    result.data['messages'][0].update(status='blocked', text='FIXME: обещание недоступной функции')
                else:
                    result.data['messages'][0].update(status='hypothesis_only', text='Исправленное приглашение проверить потребность')
            return result
    snapshot = json.loads((ROOT / 'contracts/examples/success.json').read_text())
    results = compare(snapshot, config(tmp_path, model_name='fixture-model'), tmp_path / 'compare',
                      modes=('no-critic','critic-once','full'), model_factory=CorrectableModel)
    by_mode={r['mode']:json.loads(__import__('pathlib').Path(r['file']).read_text()) for r in results}
    assert by_mode['no-critic']['calls']==2 and not by_mode['no-critic']['defects_fixed']
    assert by_mode['critic-once']['calls']==3 and by_mode['critic-once']['status']=='incomplete'
    full=by_mode['full']
    assert full['defects_found']
    assert not full['human_selection_submitted'] and not full['data']['decisions']
    if persistent:
        assert full['status']=='incomplete' and full['residual_issues'] and not full['defects_fixed']
        assert full['calls']==7
    else:
        assert full['status']=='waiting_for_input' and full['defects_fixed'] and not full['residual_issues']
        assert full['calls']==5 and not full['deterministic_defects']
        assert any(m['version']==2 and 'Исправленное' in m['text'] for m in full['data']['messages'])


def test_live_preflight_requires_pinned_model_and_web_key_without_calling_services(tmp_path):
    from scripts.try_agents_live import preflight
    missing=preflight(config(tmp_path),'web')
    assert missing['status']=='blocked' and len(missing['missing'])==2 and not missing['services_called']
    configured=preflight(config(tmp_path,model_name='fixture-model',tavily_api_key='do-not-leak'),'web')
    assert configured['status']=='configured' and 'do-not-leak' not in json.dumps(configured)


def test_existing_quote_can_move_to_new_source_version_but_original_issue_stays_immutable():
    data=bundle(); old=data['sources'][0]
    old['content_excerpt']='Цитата для проверки'
    old['version']=2
    evidence=dict(source_id=old['id'],source_version=1,quote='Цитата',locator='фрагмент',relation='supports')
    issue=dict(target=dict(kind='claim',id=data['claims'][0]['id'],version=1),evidence=[evidence],status='open',explanation='Проверка')
    revised=AgentRuntime.retarget(issue,data)
    assert revised['evidence'][0]['source_version']==2
    assert issue['evidence'][0]['source_version']==1


def test_unavailable_source_on_repair_stops_with_coherent_partial_evidence(tmp_path):
    from app.workflow.stages import StageRegistry, StageSpec, AGENT_STAGES
    store=Storage(tmp_path/'state')
    project=store.create_project('Unavailable',json.loads((ROOT/'examples/agents/tutor.json').read_text()))
    run=store.create_run(project['id'],dict(LIMITS,timeout_seconds=600))
    reads=[]; reviews=[]
    class Tools:
        def __init__(self,settings,sink,gate,req): self.req=req
        def read_file(self,path):
            reads.append(1)
            return source(self.req,key='https://example.org/fact',title='Источник',url='https://example.org/fact',
                          text='Исходная цитата' if len(reads)==1 else '', note='Тест чтения страницы')
        def close(self): pass
    class Model(FixtureModel):
        def generate(self,**kwargs):
            result=super().generate(**kwargs)
            data=json.loads(kwargs['messages'][-1]['content'])['data']
            if kwargs['stage']=='research':
                src=next(s for s in data['sources'] if s['kind']=='web')
                result.data['claims'][0].update(kind='external_fact',verification='supported',evidence=[dict(source_id=src['id'],
                    source_version=src['version'],quote='Исходная цитата',locator=src['locator'],relation='supports')])
            if kwargs['stage']=='review' and not reviews:
                reviews.append(1);claim=data['claims'][0]
                result.data['issues']=[dict(id='issue_retry',code='RECHECK',severity='blocking',
                    target=dict(kind='claim',id=claim['id'],version=claim['version']),explanation='Перепроверить',
                    evidence=deepcopy(claim['evidence']),suggested_fix='Повторить чтение',return_to='research',status='open')]
            return result
    settings=config(tmp_path,research_files=['provided.md'])
    runtime=AgentRuntime(settings,store,model_factory=Model,tools_factory=Tools)
    registry=StageRegistry('unavailable-v2',{s:StageSpec(runtime,s+'-v2','fixture-model') for s in AGENT_STAGES},synthetic=True)
    WorkerService(store,registry).run(once=True)
    final=store.get_snapshot(run['id'])
    assert final['run']['status']=='incomplete' and final['run']['stage']=='research'
    assert final['data']['claims'][0]['verification']=='unverified'
    assert final['data']['issues'][0]['evidence']==[]
    assert not any(a['prompt_version'].startswith('platform-diagnostic') for a in final['artifacts'])
    assert len(reads)==2
    validate_snapshot(final)


def test_recommendation_aliases_follow_namespaced_ids_without_changing_literal_evidence():
    from app.agents.contracts import namespace_payload, META
    req=request('options')
    payload=dict(summary='Рекомендую option_1; option_10 и not_part_option_1 не менять.',questions=[],
                 options=[{k:v for k,v in o.items() if k not in META} for o in bundle()['options']])
    actual=namespace_payload(req,payload)
    assert actual['options'][0]['id'] in actual['summary']
    assert 'option_10' in actual['summary'] and 'not_part_option_1' in actual['summary']
    req=request('research');claim={k:v for k,v in bundle()['claims'][0].items() if k not in META}
    claim['evidence'][0]['quote']='claim_email'
    normalized=namespace_payload(req,dict(summary='claim_email',questions=[],claims=[claim]))
    assert normalized['summary']==normalized['claims'][0]['id']
    assert normalized['claims'][0]['evidence'][0]['quote']=='claim_email'
