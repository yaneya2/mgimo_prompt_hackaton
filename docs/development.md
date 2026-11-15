# Разработка и настройка

Установка зависимостей: `uv sync --locked` (Python 3.12). Локальный `.env` создаётся из `.env.example` и не добавляется в Git. Одна команда `.venv/bin/python -E -m scripts.run_local` запускает HTTP-сервер и отдельный worker. Для повторного открытия того же проекта используйте прежний каталог `--data-dir`. Для проверки без внешних вызовов добавьте `--demo`; этот режим явно синтетический.

Настройки `APP_MODEL_PROVIDER`, `APP_MODEL_NAME`, `APP_MODEL_API_KEY`, `APP_CODEX_COMMAND` и тайм-ауты описаны в [инструкции моделей](../app/llm/README.md). Поиск использует `APP_TAVILY_API_KEY`; локальные материалы задаются через `APP_RESEARCH_MATERIALS_DIR` и `APP_RESEARCH_FILES`. Ключи не пишутся в кейсы, отчёты и Git. Среда контейнера не наследует авторизацию Codex на хосте; для реального контейнерного запуска настраивается доступный провайдер модели.

`docker compose up --build --wait` поднимает сервер. Для worker используйте `docker compose --profile worker up --build -d` с единым томом данных и настроенным `APP_STAGE_FACTORY`. Контейнер предназначен для Linux Docker; подробности переменных есть в `.env.example` и `app/config.py`. `docker compose down` сохраняет том, `down --volumes` удаляет его намеренно.

Проверки: `pytest -q`, Node-тесты из [README](../README.md), `scripts/check_contracts.py` и `scripts.check_secrets`. После изменения контрактов обновляйте схемы и примеры согласованно. После изменения промптов запускайте регрессии и отдельную оценку на новых кейсах. Живые проверки модели и Tavily выполнять только явно, учитывая возможные расходы.
