"""Deterministic checks supplement semantic criticism, never claim truth."""
from app.agents.contracts import identity, ref


def check_bundle(data, excluded=()):
    defects = []
    sources = {s['id']: s for s in data['sources'] if s['id'] not in excluded}
    caps = {c['id']: c for c in data['capabilities']}
    options = {o['id']: o for o in data['options']}
    claims = {c['id']: c for c in data['claims']}
    messages = {m['id']: m for m in data['messages']}

    def defect(collection, item, code, text, target_stage):
        defects.append(dict(id=identity(code + ':' + item['id']), code=code, severity='blocking',
                            target=ref(collection, item), explanation=text, evidence=[],
                            suggested_fix=text, return_to=target_stage, status='open'))

    for claim in data['claims']:
        valid_support = False
        contradiction = False
        for e in claim['evidence']:
            s = sources.get(e['source_id'])
            if not s or s['version'] != e['source_version'] or s['verification'] != 'read' or e['quote'] not in (s['content_excerpt'] or ''):
                defect('claims', claim, 'INVALID_EVIDENCE', 'Нужна дословная цитата из прочитанного и не исключённого источника актуальной версии.', 'research')
            else:
                valid_support |= e['relation'] == 'supports' and s['kind'] == 'web'
                contradiction |= e['relation'] == 'contradicts'
        if claim['kind'] == 'external_fact' and claim['verification'] == 'supported' and not valid_support:
            defect('claims', claim, 'UNSUPPORTED_FACT', 'Внешний факт требует поддержки прочитанным веб-источником; иначе понизить до гипотезы.', 'research')
        if contradiction and claim['verification'] != 'disputed':
            defect('claims', claim, 'CONFLICT_HIDDEN', 'Сохранить конфликт свидетельств со статусом disputed.', 'research')
    signatures = set()
    for option in data['options']:
        signature = tuple(str(option[k]).strip().casefold() for k in ('segment', 'value', 'channels', 'priorities'))
        if signature in signatures:
            defect('options', option, 'DUPLICATE_OPTION', 'Изменить сегмент, ценность, приоритеты или способ распространения, а не название.', 'options')
        signatures.add(signature)
        if any(cid not in caps for cid in option['capability_ids']) or any(cid not in claims for cid in option['claim_ids']):
            defect('options', option, 'INVALID_OPTION_REF', 'Использовать существующие возможности и утверждения.', 'options')
    for message in data['messages']:
        if message['status'] == 'blocked' or any(cid not in caps or caps[cid]['status'] != 'available' for cid in message['capability_ids']):
            defect('messages', message, 'UNSUPPORTED_PROMISE', 'Удалить обещание отсутствующей функции; оставить приглашение проверить гипотезу.', 'communications')
        if message['status'] == 'allowed' and not message['capability_ids']:
            defect('messages', message, 'UNLINKED_PROMISE', 'Связать обещание с доступной возможностью или поставить hypothesis_only.', 'communications')
        if message['option_id'] not in options or any(cid not in claims for cid in message['claim_ids']):
            defect('messages', message, 'INVALID_MESSAGE_REF', 'Исправить ссылки сообщения на вариант и основания.', 'communications')
    for message in data['messages']:
        if not any(message['id'] in exp['message_ids'] for exp in data['experiments']):
            defect('messages', message, 'UNTESTED_MESSAGE', 'Связать сообщение с измеримым экспериментом.', 'communications')
    for exp in data['experiments']:
        if exp['option_id'] not in options or any(mid not in messages or messages[mid]['option_id'] != exp['option_id'] for mid in exp['message_ids']):
            defect('experiments', exp, 'INVALID_EXPERIMENT_REF', 'Эксперимент должен ссылаться на сообщения собственного направления.', 'communications')
    for option in data['options']:
        if not any(e['option_id'] == option['id'] for e in data['experiments']):
            defect('options', option, 'MISSING_EXPERIMENT', 'Добавить измеримый эксперимент для направления.', 'communications')
        if not any(m['option_id'] == option['id'] for m in data['messages']):
            defect('options', option, 'MISSING_MESSAGE', 'Добавить сообщение и канал для направления.', 'communications')
    return defects
