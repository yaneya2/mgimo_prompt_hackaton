> Исторический документ: отдельные утверждения о состоянии интеграции и версиях могли устареть. Актуальные инструкции — в [корневом README](../../README.md).

Адаптер OpenRouter — A3

Используется Chat Completions без streaming и вызовов инструментов. Провайдер выбирается через APP_MODEL_PROVIDER=openrouter, модель — через точный APP_MODEL_NAME. API-ключ задаётся в локальном `.env` как APP_MODEL_API_KEY. Существующий `.env` автоматически не изменяется. Для /health ключ не требуется.

Дополнительные настройки: APP_MODEL_TIMEOUT_SECONDS=60 (тайм-аут сетевой операции), APP_MODEL_MAX_TOKENS=2048 (лимит ответа), APP_MODEL_MAX_RETRIES=1 (добавочные попытки, максимум 3). Общий бюджет времени и денег процесса относится к A8; тайм-аут сетевой операции не является точным общим дедлайном.

Пример для участника 2, запускаемый из корня проекта:

```python
from app.config import Settings
from app.llm import JsonlUsageSink, OpenRouterAdapter, ModelError

settings = Settings()
sink = JsonlUsageSink(settings.data_dir / "usage.jsonl")
schema = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}
with OpenRouterAdapter(settings, sink) as model:
    try:
        result = model.generate(
            messages=[{"role": "user", "content": "Предложи один вопрос для уточнения аудитории."}],
            schema=schema, run_id="run_example", stage="brief",
        )
        # result.data — проверенный JSON-объект
        # result.usage_ids — уже записанные события, не начислять их повторно
    except ModelError as error:
        # error.code, error.retryable, error.usage_ids
        # Ошибка не содержит ответ провайдера, сообщения пользователя или ключ.
        raise
```

Адаптер синхронный: вызывать из worker или threadpool, не блокировать асинхронный маршрут FastAPI. Созданный адаптером HTTP-клиент закрывается контекстным менеджером; переданный клиент принадлежит вызывающей стороне. Для тестирования используется httpx.MockTransport.

Схема должна описывать объект. Поддерживаются локальные JSON Pointer ссылки `#/$defs/...`, внешние ссылки, `$id` и `$dynamicRef` запрещены. Для определения из A1 передать его поля вместе с `$defs` из `contracts/schema.json`. Набор JSON Schema, поддерживаемый конкретной моделью, может быть уже локального валидатора: такой запрос завершится INVALID_REQUEST, без молчаливого отказа от схемы. Поддержку structured outputs нужно проверить у выбранной модели. Параметры маршрутизации require_parameters=true и allow_fallbacks=false исключают молчаливый переход на маршрут без нужных параметров.

Успех требует finish_reason=stop, корректного JSON без повторяющихся ключей/NaN и соответствия схеме. Невалидный ответ не исправляется новым платным вызовом автоматически. Семантическую истинность данных адаптер не проверяет.

Ошибки: CONFIGURATION_ERROR, INVALID_REQUEST, INVALID_SCHEMA, AUTHENTICATION_ERROR, INSUFFICIENT_CREDITS, ACCESS_DENIED, MODEL_NOT_FOUND, RATE_LIMITED, PROVIDER_ERROR, TIMEOUT, NETWORK_ERROR, MODEL_REFUSAL, INCOMPLETE_RESPONSE, INVALID_RESPONSE, USAGE_RECORDING_ERROR. Это внутренние ошибки адаптера; преобразование в публичный ErrorResponse выполняется будущим API/оркестратором.

Автоматические повторы разрешены только для 429/502/503/504, ограничены APP_MODEL_MAX_RETRIES. Retry-After соблюдается, но ожидание более 10 секунд возвращает управление вызывающей стороне без раннего повтора. Тайм-аут и сетевые ошибки автоматически не повторяются: исходный запрос мог уже выполниться. Повтор после серверной ошибки также не гарантирует отсутствия повторной оплаты.

Каждая попытка создаёт событие Usage по контракту A1 в `data/usage.jsonl` (в Docker — `/data/usage.jsonl`). Сохраняются входные/выходные токены, длительность, модель и стоимость, включая стоимость ответа, который не прошёл валидацию. Отсутствие данных обозначается null/unknown. Отчёт OpenRouter о стоимости используется в USD; для совместимости с Money A1 сумма округляется до 6 знаков, и при потере точности помечается estimated. Это ещё не точный бухгалтерский журнал; точность Money можно расширить согласованным изменением A1. usage.cached=false означает отсутствие локального кэша вызова, а не отсутствие кэшированных токенов у провайдера.

JsonlUsageSink — временное append-only хранилище для одного worker. При агрегировании считать уникальные event.id. В A4 его можно заменить реализацией UsageSink с базой данных. JSONL не даёт транзакционной гарантии при аварийном выключении; обрыв после ответа провайдера может оставить использование неизвестным. Ошибка записи прерывает выполнение и не запускает новый вызов. Историю предыдущих попыток можно найти по run_id; ключи и промпты в журнал не записываются. Сам факт предоставления события sink не делает произвольные сторонние события достоверными.

Явная проверка с настоящим ключом и выбранной моделью (расходует баланс):

```bash
.venv/bin/python -E -m scripts.try_openrouter
```

По умолчанию запуск backend и тесты не обращаются к OpenRouter. Для локальной установки после обновления выполнить `uv sync --locked` (либо `.bootstrap/bin/uv sync --locked`); для Docker — `docker compose up --build --wait`. `.env.example` содержит шаблон, реальные ключи в него не добавлять.

Протокол проверок и ограничение живой проверки: [A3_CHECKS](../../project-notes/reports/A3_CHECKS.md).

Основание реализации: [структурированные ответы](https://openrouter.ai/docs/guides/features/structured-outputs), [ошибки](https://openrouter.ai/docs/api_reference/errors-and-debugging), [учёт использования](https://openrouter.ai/docs/cookbook/administration/usage-accounting).
