"""Render saved data only. Raw source HTML and script-capable URLs never become markup."""
from html import escape
import json
from urllib.parse import urlsplit
from app.agents import report
from app.workflow.stages import StageContext

LABELS={'ready':'Готово','draft':'Черновик','blocked':'Есть блокирующие замечания'}


def safe_url(value):
    try:
        parsed=urlsplit(value or '')
        return value if parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not any(ord(c)<32 for c in value) else None
    except ValueError: return None


def markdown(snapshot, inputs=()):
    run,brief=snapshot['run'],snapshot['brief']
    historical=brief['version']!=snapshot['project']['current_brief_version']
    synthetic=any(a['prompt_version'].startswith('synthetic-') for a in snapshot['artifacts'])
    header=['# '+snapshot['project']['name'], '', 'Версия брифа: '+str(brief['version']),
            'Актуальность: '+('ИСТОРИЧЕСКАЯ ВЕРСИЯ' if historical else 'Текущая версия'),
            'Качество: '+LABELS[run['quality']], 'Состояние: '+run['status'], 'Запуск: '+run['id'], '']
    if synthetic: header+=['СИНТЕТИЧЕСКИЙ ПРИМЕР. Внешнее исследование не выполнялось.','']
    if run['quality']!='ready' or historical: header+=['ЧЕРНОВАЯ ВЫГРУЗКА: ограничения и замечания требуют проверки.','']
    if any(d['author']=='user' and d['action']=='select' for d in snapshot['data']['decisions']):
        req=dict(project_id=run['project_id'],run_id=run['id'],brief=brief,input_data=snapshot['data'],input_artifacts=snapshot['artifacts'],
                 stage='report',prompt_version='renderer-v1',model_id='python-renderer')
        text=report.run(req,StageContext(list(inputs),[],0,synthetic))['artifacts'][0]['summary']
    else:
        text='## Бриф\n'+brief['input']['product']+'\n\nЦель: '+brief['input']['goal']+'\n\nОкончательное направление ещё не выбрано.\n'
        for title,key in [('Альтернативы','options'),('Утверждения','claims'),('Источники','sources'),('Замечания','issues')]:
            text+='\n## '+title+'\n\n'+ '\n'.join('- '+json.dumps(v,ensure_ascii=False) for v in snapshot['data'][key])+'\n'
    if snapshot['questions']:
        text+='\n## Открытые вопросы\n'+'\n'.join('- '+q['text'] for q in snapshot['questions'])
    text+='\n\n## Прочитанные фрагменты\n'
    for src in snapshot['data']['sources']:
        text+='\n### '+src['title']+'\n'+(src['url'] or 'Пользовательский материал')+'\n\n'+(src['content_excerpt'] or 'Источник недоступен.')+'\n'
    return '\n'.join(header)+text+'\n'


def html(snapshot,inputs=()):
    """Small Markdown subset, escaping first; no third-party HTML parser or raw HTML execution."""
    blocks=[];paragraph=[]
    def flush():
        if paragraph:blocks.append('<p>'+'<br>'.join(paragraph)+'</p>');paragraph.clear()
    for line in markdown(snapshot,inputs).splitlines():
        if not line.strip():flush();continue
        if line.startswith('#') and ' ' in line:
            flush();prefix,title=line.split(' ',1);level=min(len(prefix),4)
            blocks.append(f'<h{level}>'+escape(title)+f'</h{level}>')
        elif line.startswith('- '):flush();blocks.append('<p class="item">• '+escape(line[2:])+'</p>')
        elif safe_url(line):flush();blocks.append('<p><a rel="noreferrer noopener" href="'+escape(line,quote=True)+'">'+escape(line)+'</a></p>')
        else:paragraph.append(escape(line))
    flush()
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+escape(snapshot['project']['name'])+'</title><style>body{font:16px/1.65 system-ui,sans-serif;max-width:940px;margin:50px auto;padding:0 24px;color:#192c2b}h1,h2{line-height:1.2;margin-top:38px}p{overflow-wrap:anywhere}h2{border-top:1px solid #d8dfdc;padding-top:24px}.item{padding-left:18px}a{color:#16635d}@media print{body{margin:0;font-size:11pt}h2,h3{break-after:avoid}}</style><body>'+''.join(blocks)+'</body></html>'
