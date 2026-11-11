"""Read original strings and cover research-query groups within a fixed URL budget."""
import re
from app.tools.research import canonical_url, ResearchError


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in value.values():
            yield from strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from strings(item)


def extract_user_urls(value):
    urls, errors = [], []
    for text in strings(value):
        for raw in re.findall(r'https?://[^\s"<>]+', text):
            raw = raw.rstrip('.,;:!?»”')
            # Strip closing prose punctuation only when not balanced inside a URL path/query.
            for opening, closing in (('(', ')'), ('[', ']'), ('{', '}')):
                while raw.endswith(closing) and raw.count(closing) > raw.count(opening):
                    raw = raw[:-1]
            try:
                url = canonical_url(raw)
                if url not in urls:
                    urls.append(url)
            except ResearchError:
                errors.append('Отклонён недопустимый пользовательский URL')
    return urls, errors


def select_urls(groups, limit):
    """Round-robin coverage, preserving search rank within each query and explicit links."""
    selected = []
    queues = [list(dict.fromkeys(group['urls'])) for group in groups]
    while len(selected) < limit and any(queues):
        for queue in queues:
            while queue and queue[0] in selected:
                queue.pop(0)
            if queue and len(selected) < limit:
                selected.append(queue.pop(0))
    return selected


def coverage(groups, selected, sources):
    # Mirror notes retain successfully read URLs after content deduplication.
    read = {s['url'] for s in sources if s['verification'] == 'read' and s['url']}
    for url in selected:
        if any(s['verification'] == 'read' and url in re.findall(r'https?://[^\s]+', s['origin_note']) for s in sources):
            read.add(url)
    results = []
    for group in groups:
        picked = [url for url in selected if url in group['urls']]
        successful = [url for url in picked if url in read]
        reason = None
        if not successful:
            reason = ('Нет доступных кандидатов (ошибка поиска, исключение или пустая выдача).' if not group['urls']
                      else 'Общий лимит источников не позволил прочитать вопрос.' if not picked
                      else 'Выбранные страницы не удалось прочитать (частичный сбой Extract).')
        results.append(dict(label=group['label'], query=group.get('query'), candidates=group['urls'],
                            selected=picked, read=successful, gap=reason))
    return results
