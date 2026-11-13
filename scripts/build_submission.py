"""Build the submission PDF; optional authoring dependency: reportlab."""
from pathlib import Path
import json
from html import escape
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.lib import colors
from reportlab.lib.styles import ParagraphStyle
from reportlab.lib.enums import TA_LEFT
from reportlab.platypus import SimpleDocTemplate,Paragraph,Spacer,Image,PageBreak,Table,TableStyle

ROOT=Path(__file__).resolve().parents[1]
def main():
    fonts=[Path('/System/Library/Fonts/Supplemental/Arial.ttf'),Path('/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf')]
    font=next((p for p in fonts if p.exists()),None)
    if not font:raise SystemExit('Install Arial or DejaVu Sans for Cyrillic rendering')
    pdfmetrics.registerFont(TTFont('Body',str(font)))
    out=ROOT/'output/pdf/submission.pdf';out.parent.mkdir(parents=True,exist_ok=True)
    doc=SimpleDocTemplate(str(out),pagesize=(595,842),rightMargin=42,leftMargin=42,topMargin=48,bottomMargin=44)
    body=ParagraphStyle('body',fontName='Body',fontSize=10,leading=15,textColor=colors.HexColor('#233d37'),spaceAfter=10)
    title=ParagraphStyle('title',parent=body,fontSize=26,leading=32,spaceAfter=20)
    heading=ParagraphStyle('heading',parent=body,fontSize=16,leading=21,spaceAfter=14)
    small=ParagraphStyle('small',parent=body,fontSize=8,leading=11)
    story=[]
    def p(text,style=body):story.append(Paragraph(escape(text),style))
    def h(text):p(text,heading)
    def image(name):
        f=ROOT/'docs/screenshots'/name
        from reportlab.lib.utils import ImageReader
        w,ht=ImageReader(str(f)).getSize();story.append(Image(str(f),width=511,height=511*ht/w));story.append(Spacer(1,12))
    p('Продуктовая стратегия с проверяемыми основаниями',title)
    p('Рабочий интерфейс, управление процессом и результаты проверки. Состояние пакета: 13 ноября 2026 года.')
    image('landing.jpg')
    h('От брифа до решения')
    p('Система уточняет продукт, сохраняет источники и утверждения, предлагает три направления, разрабатывает коммуникации и эксперименты, запускает критика. Направление явно выбирает человек. Итог сохраняет версии, основания, открытые вопросы и ограничения.')
    p('Кадры интерфейса в документе получены на явно синтетическом учебном сценарии. Они показывают работу интерфейса, а не живое веб-исследование.',small)
    story.append(PageBreak())
    h('Архитектура и реальные инструменты')
    p('HTML/CSS/JavaScript → FastAPI /api/v1 → SQLite → отдельный worker → этапы brief, research, options, communications, review, selection, report. Оркестрация, сохранение, контроль версий и сборка отчёта выполняются кодом.')
    p('Python 3.12, FastAPI, Pydantic/JSON Schema, SQLite; версии Python-пакетов закреплены в uv.lock. Модель измерений: gpt-6-luna через локально авторизованный Codex CLI. Доступен адаптер OpenRouter. Поиск: Tavily Search и Extract, локальные TXT/Markdown. В измерениях ниже использованы только замороженные пользовательские карточки; Tavily-ключ не предоставлен.')
    p('Продуктовые инструкции: prompts/*-v2.md; общий запрет выполнять инструкции из источников, разделение фактов/гипотез, связь обещаний с доступными возможностями и основание замечаний критика. Manifest strategy-agents-v3 отражает контекст исключённых источников; тексты промптов не подгонялись по holdout.')
    image('selection.jpg')
    p('Клиент обращается только к API. Команды сохраняют Idempotency-Key, сервер проверяет версии. Модель не вызывается из HTTP-запроса. HTML экранирует внешнее содержимое; выгрузки привязаны к неизменяемому снимку.',small)
    story.append(PageBreak())
    h('Вмешательство и сохранение истории')
    image('revision.jpg')
    p('Проверено в браузере: проект сохранился после перезапуска приложения; бюджет изменён со 100 000 до 50 000 RUB, добавлен запрет платной рекламы. Созданы версия брифа 2 и новый запуск; версия 1 доступна в истории. Синтетический worker дошёл до нового выбора человека.')
    p('Пересчёт полный: прежние зависимые рекомендации не используются в новом запуске. Исторические checkpoint сохраняются, старые проекции получают признак stale. Исключение источника тоже создаёт новую версию и запуск.')
    p('Запрос паузы во время вызова переводит процесс в pause_requested; текущий этап подтверждается, затем процесс останавливается. Лимиты вызовов, времени и доработок контролируются кодом. При неизвестной цене денежный предел нельзя гарантировать, внешние вызовы с таким пределом останавливаются.')
    story.append(PageBreak())
    h('Техническая приёмка')
    p('230 тестов пройдено, 2 живых теста пропущены. Проверка контрактов: 35 схем, 9 примеров, 17 операций API, 8 негативных случаев. В новом чистом окружении uv sync --locked --offline установил 40 пакетов из локального кэша, после чего выполнен полный pytest.')
    p('Покрыто: HTTP-сценарий до выгрузки; явный выбор; повтор команд; конфликт версий; пауза во время этапа; изменение бюджета; сохранение истории; исключение источника; экранирование HTML. Существующие тесты проверяют SIGKILL и восстановление в новом процессе, противоречия, недоступный поиск и инструкции во внешнем документе.')
    image('report.jpg')
    p('Браузером проверены создание проекта, выбор с обоснованием, обновление страницы, скачивание HTML и новая версия условий. Черновик требует явного разрешения на выгрузку. Итог отображает качество и открытые вопросы.',small)
    story.append(PageBreak())
    h('Сравнение режимов на новых задачах')
    p('Одинаковые замороженные входы: цифровые возвраты, мобильная заточка, аренда. SHA-256 входов зафиксированы до вызовов. По одному прогону каждого режима; 9 запусков и 17 вызовов модели. Выбор человека не имитировался.')
    rows=json.loads((ROOT/'evals/results/pilot/results.json').read_text())['rows']
    data=[['Кейс / режим','Вызовы','Секунды','Полнота','Критик']]
    cases={'returns':'Возвраты','sharpening':'Заточка','rental':'Аренда'}
    for r in rows:data.append([cases[r['case']]+' / '+r['mode'],str(r['calls']),str(r['seconds']),'до выбора' if r['comparison_complete'] else 'уточнение',str(r['residual'])])
    table=Table(data,colWidths=[205,55,65,100,66]);table.setStyle(TableStyle([('FONTNAME',(0,0),(-1,-1),'Body'),('FONTSIZE',(0,0),(-1,-1),8),('BACKGROUND',(0,0),(-1,0),colors.HexColor('#dae8df')),('TEXTCOLOR',(0,0),(-1,-1),colors.HexColor('#233d37')),('BOTTOMPADDING',(0,0),(-1,-1),10),('TOPPADDING',(0,0),(-1,-1),10),('LINEBELOW',(0,0),(-1,-1),.3,colors.HexColor('#d2ddd6'))]));story.append(table);story.append(Spacer(1,18))
    p('Returns/no-critic остановился на уточнении и не выдал стратегию: этот запуск не считается бездефектным. Полные режимы оставили 3, 3 и 4 предупреждения, исправленных 0. Наблюдались незаданные владельцы экспериментов и локальные идентификаторы в свободном тексте. Нулевой программный счётчик дефектов не подтверждает смысловую достоверность.')
    p('Стоимость всех вызовов неизвестна, а не равна нулю. Дополнительные роли увеличили время и число вызовов; универсальное улучшение качества этими данными не доказано. Независимая ручная рубрика подготовлена, но оценки не заполнены. Снимки и usage доступны в evals/results/pilot.',small)
    story.append(PageBreak())
    h('Разработка, ограничения и комплект')
    p('Интеграцию и исправления выполнял Codex по запросу пользователя: API-клиент и экраны, безопасные выгрузки, новая версия при изменении условий, фиксация паузы, защита от повторов и устаревших фоновых ответов. Независимая приёмка другим человеком не заявляется. Журнал разработки: docs/build-log.md; runtime-промпты отдельно в prompts/.')
    p('Ограничения: локальный однопользовательский запуск без сетевой авторизации; полный пересчёт вместо инкрементального; неизвестная цена провайдера; небольшая выборка измерений; веб-прогон с ключом Tavily и независимая смысловая оценка ещё не выполнены. Дополнительные негативные входы подготовлены, отдельные живые прогоны на них не проводились.')
    h('Как воспроизвести')
    p('Установить Python 3.12 и uv, выполнить uv sync --locked, настроить собственный .env по .env.example. Команда: python -E -m scripts.run_local. Учебный запуск: python -E -m scripts.run_local --demo --data-dir data/demo. Открыть http://127.0.0.1:8000. Проверка: python -E -m pytest -q и python -E scripts/check_contracts.py.')
    h('Материалы для дальнейшей сдачи')
    p('README, docs/interface.md, сценарий docs/demo-script.md, кадры docs/screenshots, эта PDF-версия, новые кейсы и измерения evals/. Требуется записать и разместить видео, сверить полные правила и доступ к материалам, выполнить внешнюю отправку. Личные конкурсные документы не включаются в репозиторий.')
    p('Этот документ описывает фактически реализованное и проверенное поведение. Сохранённые кадры и ответы не выдаются за текущий живой поиск или опубликованную демонстрацию.',small)
    def footer(canvas,doc):
        canvas.setStrokeColor(colors.HexColor('#b9cec1'));canvas.line(42,34,553,34);canvas.setFont('Body',8);canvas.setFillColor(colors.HexColor('#527066'));canvas.drawString(42,22,'ПРОДУКТОВАЯ СТРАТЕГИЯ • МАТЕРИАЛЫ ПРОЕКТА');canvas.drawRightString(553,22,str(doc.page))
    doc.build(story,onFirstPage=footer,onLaterPages=footer);print(out)
if __name__=='__main__':main()
