"""Human-facing exports from saved structured results; raw records remain in storage."""
from html import escape
import re
from urllib.parse import urlsplit

LABELS={'ready':'Отчёт сформирован; гипотезы требуют проверки','draft':'Черновик','blocked':'Есть блокирующие замечания'}
WORDS={'RUB':'₽','available':'Доступно','planned':'Запланировано','unavailable':'Недоступно','allowed':'Допустимое обещание','blocked':'Заблокировано','hypothesis_only':'Гипотеза для проверки','user_provided':'Со слов пользователя','external_fact':'Внешнее утверждение','hypothesis':'Гипотеза','forecast':'Прогноз','supported':'Поддержано источником','unverified':'Не проверено','contradicted':'Есть противоречия','read':'Прочитан','resolved':'Исправлено','open':'Открыто','accepted_limitation':'Принятое ограничение'}


def safe_url(value):
    try:
        parsed=urlsplit(value or '')
        return value if parsed.scheme in ('http','https') and parsed.hostname and not parsed.username and not any(ord(c)<32 for c in value) else None
    except ValueError:
        return None


def readable(value, data):
    """Translate model references only in presentation text, preserving stored evidence."""
    names={item['id']:'«'+item['name']+'»' for item in data['options']}
    for collection,label in [('capabilities','Возможность'),('claims','Утверждение'),('sources','Источник'),('messages','Сообщение'),('experiments','Эксперимент'),('issues','Замечание')]:
        for number,item in enumerate(data[collection],1):
            names[item['id']]=item.get('name') or item.get('title') or f'{label} {number}'
    text=str(value or '')
    text=text.replace('brief.input.product','описании продукта').replace('brief.input.','брифе: ')
    text=text.replace('синтетический возврат S04 показывает возможный механизм отказа, а не его рыночную частоту','в учебном примере одна мастерская получила возврат, потому что ожидала автоматические SMS. Частота таких отказов у реальных клиентов неизвестна')
    text=re.sub(r'Тексты экспериментов здесь не предоставлены:[^\n]*?статусы чужих issues не изменяются\.', '', text)
    text=text.replace(' с сохранением ID','').replace('в issues','в замечаниях').replace('из issues','из замечаний')
    return re.sub(r'(?<![\w-])[A-Za-z][A-Za-z0-9_]*(?![\w-])',lambda m:names.get(m[0],WORDS.get(m[0], 'связанный материал' if m[0].startswith(('agent_','source_')) else m[0])),text)


def markdown(snapshot, inputs=()):
    run, brief, data=snapshot['run'],snapshot['brief']['input'],snapshot['data']
    historical=snapshot['brief']['version']!=snapshot['project']['current_brief_version']
    synthetic=any(a['prompt_version'].startswith('synthetic-') for a in snapshot['artifacts'])
    text=lambda value:readable(value,data)
    lines=['# '+snapshot['project']['name'],'', 'Версия брифа: '+str(snapshot['brief']['version']), 'Качество: '+LABELS[run['quality']]]
    if historical:lines+=['Это сохранённая историческая версия условий.']
    if synthetic:lines+=['СИНТЕТИЧЕСКИЙ ПРИМЕР. Внешнее исследование не выполнялось.']
    if run['quality']!='ready' or historical:lines+=['ЧЕРНОВИК. ЧЕРНОВАЯ ВЫГРУЗКА: ограничения и замечания требуют проверки.']
    lines+=['','Продукт: '+text(brief['product']),'Цель: '+text(brief['goal']),'Срок: '+text(brief['horizon']), 'Бюджет: '+(brief['budget']['amount']+' '+WORDS.get(brief['budget']['currency'],brief['budget']['currency']) if brief['budget'] else 'не указан')]
    lines+=['','## Действующие ограничения']+['- '+text(value) for value in brief['constraints']]
    decision=next((d for d in reversed(data['decisions']) if d['author']=='user' and d['action']=='select'),None)
    option=next((o for o in data['options'] if decision and o['id']==decision['option_id']),None)
    if option:
        lines+=['','## Решение','### '+text(option['name']),text(decision['rationale']),'', '**Аудитория:** '+text(option['segment']),'**Предлагаемая ценность:** '+text(option['value']),'**Почему выбран этот путь:** '+text(option['rationale'])]
        lines+=['','## Бюджет и ресурсы']+['- '+text(v) for v in option['resource_assumptions']]
        lines+=['','## План действий']+['- '+text(v) for v in option['priorities']]
        lines+=['','## Каналы']+['- '+text(v) for v in option['channels']]
        lines+=['','## Сообщения клиентам']
        messages=[m for m in data['messages'] if m['option_id']==option['id']]
        for m in messages:
            lines+=['### '+text(m['channel']),text(m['text'])]
        lines+=['','## Как проверить результат']
        for x in data['experiments']:
            if x['option_id']!=option['id']:continue
            lines+=['### '+text(x['hypothesis']),'**Порядок:** '+text(x['method']),'**Метрики:** '+text(x['metric']),'**Порог:** '+text(x['decision_threshold']),'**Основание порога:** '+text(x['threshold_basis']),'**Срок:** '+text(x['duration']),'**Ответственный:** '+text(x['owner'] or 'Не назначен'),'**Условие остановки:** '+text(x['stop_condition'])]
        lines+=['','## Риски и ограничения']+['- '+text(v) for v in option['objections']]
        lines+=['','**Когда пересмотреть решение:** '+text(option['failure_condition'])]
        lines+=['','## Другие направления']
        for other in data['options']:
            if other['id']!=option['id']:lines+=['### '+text(other['name']),text(other['rationale'])]
    else:
        lines+=['','## Пока нет окончательного выбора','Сравнение сохранено, направление ещё не выбрано человеком.']
        for other in data['options']:lines+=['### '+text(other['name']),text(other['rationale'])]
    lines+=['','## Возможности продукта']+['- '+text(c['name'])+' — '+WORDS.get(c['status'],c['status']) for c in data['capabilities']]
    lines+=['','## Основания и источники']
    sources={s['id']:s for s in data['sources']}
    for number,claim in enumerate(data['claims'],1):
        titles=list(dict.fromkeys(sources[e['source_id']]['title'] for e in claim['evidence'] if e['source_id'] in sources))
        lines+=['### Утверждение '+str(number),text(claim['text']),'**Основание:** '+', '.join(titles or ['Не указано']),'**Статус:** '+WORDS.get(claim['verification'],claim['verification'])]
        if claim['limitations']:lines+=['**Ограничения:** '+text('; '.join(claim['limitations']))]
    lines+=['','### Использованные материалы']
    for source in data['sources']:
        title=source['title'];url=safe_url(source.get('url'))
        lines+=['- '+(f'[{title}]({url})' if url else title)+' — '+('веб-страница' if source['kind']=='web' else 'предоставленный материал')+', '+WORDS.get(source['verification'],source['verification']).lower()]
    lines+=['','## Результат проверки']
    for issue in data['issues']:
        label='Исправленное замечание' if issue['status']=='resolved' else 'Требуется исправить' if issue['severity']=='blocking' else 'Ограничение результата'
        lines+=['### '+label,text(issue['explanation'])]
    reviews=[a for a in snapshot['artifacts'] if a['stage']=='review' and a['freshness']=='current']
    lines += [text(a['summary']) for a in reviews]
    if not reviews and not data['issues']:lines+=['Заключение проверки пока не сохранено.']
    if snapshot['questions']:lines+=['','## Открытые вопросы']+['- '+q['text'] for q in snapshot['questions']]
    lines+=['','Точные записи этапов, цитаты, идентификаторы и расход сервисов сохранены в истории запуска. Неизвестная стоимость не равна нулю.']
    return re.sub(r'\n{3,}', '\n\n', '\n'.join(lines)).strip()+'\n'


def html(snapshot,inputs=()):
    blocks=[];paragraph=[]
    def inline(value):
        escaped=escape(value)
        escaped=re.sub(r'\[([^\]]+)\]\((https?://[^\s)]+)\)',lambda m:'<a rel="noreferrer noopener" href="'+escape(m[2],quote=True)+'">'+m[1]+'</a>' if safe_url(m[2]) else m[0],escaped)
        return re.sub(r'\*\*(.+?)\*\*',r'<strong>\1</strong>',escaped)
    def flush():
        if paragraph:blocks.append('<p>'+'<br>'.join(paragraph)+'</p>');paragraph.clear()
    for line in markdown(snapshot,inputs).splitlines():
        if not line.strip():flush();continue
        if line.startswith('#') and ' ' in line:
            flush();prefix,title=line.split(' ',1);level=min(len(prefix),4)
            blocks.append(f'<h{level}>'+inline(title)+f'</h{level}>')
        elif line.startswith('- '):flush();blocks.append('<p class="item">• '+inline(line[2:])+'</p>')
        elif safe_url(line):flush();blocks.append('<p><a rel="noreferrer noopener" href="'+escape(line,quote=True)+'">'+escape(line)+'</a></p>')
        else:paragraph.append(inline(line))
    flush()
    return '<!doctype html><html lang="ru"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>'+escape(snapshot['project']['name'])+'</title><style>body{font:16px/1.65 system-ui,sans-serif;max-width:940px;margin:50px auto;padding:0 24px;color:#192c2b}h1,h2{line-height:1.2;margin-top:38px}p{overflow-wrap:anywhere}h2{border-top:1px solid #d8dfdc;padding-top:24px}.item{padding-left:18px}a{color:#16635d}@media print{body{margin:0;font-size:11pt}h2,h3{break-after:avoid}}</style><body>'+''.join(blocks)+'</body></html>'


def render_snapshot(snapshot, format):
    return html(snapshot) if format == 'html' else markdown(snapshot)
