"""Freeze new acceptance inputs before any model comparison; these are fictional conditions."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
from app.storage.database import now
from app.storage.validation import empty_data,validate_snapshot

CASES=[
('returns','Учёт возвратов в небольших магазинах','Сервис ручного учёта возвратов: доступны карточка обращения, статус и выгрузка CSV. Автоматического возврата денег и интеграции с кассой нет.',
 ['Небольшие интернет-магазины','Локальные магазины одежды'],['Карточка обращения','Статус обращения','Выгрузка CSV'],['Автоматический возврат денег','Интеграция с кассой'],'Шесть недель','60000'),
('sharpening','Выездная заточка садового инструмента','Услуга выездной заточки инструмента: доступны ручная запись на согласованный день, предварительная оценка по фото и выдача заказа на месте. Круглосуточного выезда и гарантии восстановления любого инструмента нет.',
 ['Садовые товарищества','Небольшие ландшафтные бригады'],['Запись на согласованный день','Предварительная оценка по фото','Выдача заказа на месте'],['Круглосуточный выезд','Гарантия восстановления любого инструмента'],'Четыре недели','35000'),
('rental','Прокат оборудования для небольших событий','Услуга проката: доступны каталог трёх комплектов света, ручное согласование даты и выдача со склада. Доставки, автоматической проверки доступности и монтажа нет.',
 ['Небольшие организаторы событий','Фотографы'],['Каталог комплектов света','Ручное согласование даты','Выдача со склада'],['Доставка','Автоматическая проверка доступности','Монтаж'],'Пять недель','45000')]


def snapshot(case):
    key,title,product,audiences,available,unavailable,horizon,budget=case
    source_id='source_'+key;project_id='project_'+key;timestamp=now()
    brief=dict(product=product,goal='Выбрать первый сегмент и проверяемый способ привлечения',audience=audiences,horizon=horizon,
               budget=dict(amount=budget,currency='RUB'),constraints=['Платная реклама запрещена','Один ответственный за пилот',
                'Не обещать отсутствующие функции: '+', '.join(unavailable)],assumptions=['Условия вымышлены и заданы пользователем',
                'Данных о спросе нет; разрешены явно обозначенные осторожные допущения для экспериментов'],unknowns=['Спрос','Готовность платить','Базовые метрики'],excluded_source_ids=[])
    data=empty_data()
    def entity(identity,**fields):return dict(id=identity,version=1,project_id=project_id,brief_version=1,**fields)
    data['sources']=[entity(source_id,kind='user_file',title=title+' — карточка условий',url=None,publisher=None,published_at=None,
                      retrieved_at=timestamp,content_excerpt=product,locator='Условие приёмочного кейса',verification='read',
                      origin_note='Вымышленная карточка пользователя; не внешнее исследование рынка.')]
    data['capabilities']=[entity('cap_'+key+'_'+str(i),name=name,status='available',basis='Явно задано в условии кейса',source_ids=[source_id]) for i,name in enumerate(available)]
    data['capabilities'] += [entity('cap_'+key+'_absent_'+str(i),name=name,status='hypothesis',basis='В условии прямо указано отсутствие; не обещать в коммуникации.',source_ids=[source_id]) for i,name in enumerate(unavailable)]
    data['claims']=[entity('claim_'+key,text='Возможности продукта заданы в карточке пользователя; независимая проверка не проводилась.',
        kind='user_provided',verification='supported',evidence=[dict(source_id=source_id,source_version=1,quote=product,locator='Условие кейса',relation='supports')],
        limitations=['Нет внешних свидетельств спроса и эффекта'])]
    result=dict(schema_version='1.0.0',project=dict(id=project_id,name=title,created_at=timestamp,current_brief_version=1),
                brief=dict(project_id=project_id,version=1,created_at=timestamp,author='user',input=brief),
                run=dict(id='run_seed_'+key,project_id=project_id,brief_version=1,revision=1,status='paused',stage='options',quality='draft',
                         created_at=timestamp,updated_at=timestamp,checkpoint_id=None,attempt=0,error=None),data=data,artifacts=[],questions=[],usage=[])
    validate_snapshot(result)
    return result


def main():
    root=Path('evals/cases');root.mkdir(parents=True,exist_ok=True)
    manifest={}
    for case in CASES:
        path=root/(case[0]+'.json')
        if not path.exists():path.write_text(json.dumps(snapshot(case),ensure_ascii=False,indent=2)+'\n')
        manifest[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    # Negative cases are acceptance inputs, not additional paid comparisons.
    base=snapshot(CASES[0]);base['brief']['input'].update(budget=None,horizon='Не определён',audience=[],unknowns=['Бюджет','Горизонт','Аудитория'])
    missing=root/'incomplete.json'
    if not missing.exists():missing.write_text(json.dumps(base,ensure_ascii=False,indent=2)+'\n')
    base=snapshot(CASES[1]);base['brief']['input']['budget']['amount']='0';base['brief']['input']['constraints']+=['Платная реклама обязательна','Расходы сверх нуля запрещены']
    conflict=root/'conflict.json'
    if not conflict.exists():conflict.write_text(json.dumps(base,ensure_ascii=False,indent=2)+'\n')
    for path in [missing,conflict]:manifest[path.name]=hashlib.sha256(path.read_bytes()).hexdigest()
    path=root/'manifest.json'
    if path.exists() and json.loads(path.read_text())['sha256']!=manifest:raise RuntimeError('Frozen acceptance inputs changed')
    path.write_text(json.dumps(dict(frozen_at=now(),sha256=manifest,used_for_prompt_tuning=False,
         independence='Separate held-out inputs from development examples; no independent human scoring claimed.'),ensure_ascii=False,indent=2)+'\n')
    print(path)


if __name__=='__main__':main()
