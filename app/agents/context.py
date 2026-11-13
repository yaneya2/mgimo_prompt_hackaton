"""Versioned, read-only projections of human answers and relevant stage summaries."""
from copy import deepcopy

from app.storage.validation import COLLECTIONS

# Only the latest answer to the same question in the same BriefVersion is effective.
# Free-form answers are never guessed into budget/date/channel fields by the renderer.
def clarifications(brief_version, inputs):
    latest = {}
    for record in inputs:
        if record.get('kind') != 'answers' or record.get('brief_version') != brief_version:
            continue
        questions = {q['id']: q for q in record.get('questions', [])}
        for answer in record.get('payload', {}).get('answers', []):
            qid = answer['question_id']
            previous = latest.get(qid)
            history = (previous['superseded'] + [dict(question=previous['question'], answer=previous['answer'],
                                                      input_id=previous['input_id'])]) if previous else []
            latest[qid] = dict(question_id=qid, question=questions.get(qid, {}).get('text', qid),
                               answer=answer['value'], input_id=record.get('id'), stage=record.get('stage'),
                               brief_version=brief_version, superseded=history)
    return list(latest.values())


def effective_context(brief, inputs):
    return dict(brief=deepcopy(brief), clarifications=clarifications(brief['version'], inputs),
                precedence='Актуальные ответы уточняют исходный бриф. Последний ответ на тот же вопрос заменяет предыдущий. '
                           'Исходная BriefVersion неизменна; не угадывать структуру свободного ответа.')


SUMMARY_STAGES = {'brief': (), 'research': ('brief',), 'options': ('brief', 'research'),
                  'communications': ('brief', 'research', 'options'),
                  'review': ('brief', 'research', 'options', 'communications'), 'report': ('options',)}


def current_summaries(request, stage):
    """Exclude diagnostic, stale, foreign and old-version artifacts; validate current entity refs."""
    latest = {}
    indexes = {kind: {item['id']: item['version'] for item in request['input_data'][collection]}
               for kind, collection in COLLECTIONS.items()}
    for artifact in request['input_artifacts']:
        if (artifact['stage'] not in SUMMARY_STAGES[stage] or artifact['freshness'] != 'current'
                or artifact['project_id'] != request['project_id'] or artifact['run_id'] != request['run_id']
                or artifact['brief_version'] != request['brief']['version']
                or artifact['prompt_version'].startswith('platform-diagnostic')
                or any(indexes[ref['kind']].get(ref['id']) != ref['version'] for ref in artifact['entities'])):
            continue
        previous = latest.get(artifact['stage'])
        if previous is None or (artifact['created_at'], artifact['version']) > (previous['created_at'], previous['version']):
            latest[artifact['stage']] = artifact
    return [dict(stage=item['stage'], artifact_id=item['id'], version=item['version'],
                 prompt_version=item['prompt_version'], summary=item['summary'])
            for name in SUMMARY_STAGES[stage] if (item := latest.get(name))]
