# Сервис асинхронной обработки платежей

HTTP API принимает платёж, сохраняет его вместе с событием outbox в одной транзакции и сразу отвечает `202`. Отдельный процесс публикует события в RabbitMQ и обрабатывает их: эмуляция платёжного шлюза, webhook и повторы при технических сбоях.

## Запуск

Нужны Docker и Docker Compose.

```bash
docker compose up --build -d
```

Миграции Alembic выполняет только API, до старта uvicorn. Consumer стартует после того, как API становится healthy.

Остановка:

```bash
docker compose down
```

Данные Postgres хранятся в volume `pgdata`. Чтобы начать с пустой базы: `docker compose down -v`.

## Адреса

| Сервис | URL |
| --- | --- |
| API | http://localhost:18080 |
| Health | http://localhost:18080/health |
| Postgres | `localhost:5432`, база `payments`, пользователь и пароль `payments` |
| RabbitMQ AMQP | `amqp://payments:payments@localhost:5672/` |
| RabbitMQ Management | http://localhost:15672 (`payments` / `payments`) |

Внутри контейнера API слушает порт `8000`. На хост он проброшен как `18080`.

Пользователь RabbitMQ не `guest`: в Docker пользователь `guest` принимает соединения только с localhost, и контейнеры API и consumer до брокера не достучались бы.

## Переменные

Значения для Compose заданы в `docker-compose.yml`. Пример для запуска вне Docker — в `.env.example`.

| Переменная | Назначение |
| --- | --- |
| `API_KEY` | Секрет заголовка `X-API-Key`. По умолчанию `dev-api-key` |
| `DATABASE_URL` | Async SQLAlchemy URL (`postgresql+asyncpg://...`) |
| `RABBITMQ_URL` | AMQP URL |
| `PAYMENT_SUCCESS_RATE` | Доля успешных ответов шлюза, от 0 до 1. По умолчанию `0.9` |
| `LOG_LEVEL` | Уровень логов |

## API

Бизнес-методы требуют заголовок `X-API-Key`. Сравнение через `secrets.compare_digest`. Нет ключа или ключ неверный — `401`.

`GET /health` ключ не требует: его вызывает healthcheck контейнера.

`POST /api/v1/payments` требует заголовок `Idempotency-Key`.

```bash
curl -sS -D - -X POST http://localhost:18080/api/v1/payments \
  -H 'Content-Type: application/json' \
  -H 'X-API-Key: dev-api-key' \
  -H 'Idempotency-Key: demo-1' \
  -d '{
    "amount": "10.50",
    "currency": "RUB",
    "description": "Оплата заказа",
    "metadata": {"order_id": "1001"},
    "webhook_url": "http://host.docker.internal:18081/"
  }'
```

Ответ `202`:

```json
{
  "payment_id": "uuid",
  "status": "pending",
  "created_at": "2026-10-15T18:00:00+00:00"
}
```

`amount` — число больше нуля, до 18 цифр и 2 знаков после запятой. `currency`: `RUB`, `USD` или `EUR`. В JSON сумма уходит строкой, чтобы не терять точность `Decimal`.

Получение платежа:

```bash
curl -sS http://localhost:18080/api/v1/payments/<payment_id> \
  -H 'X-API-Key: dev-api-key'
```

Неизвестный `payment_id` — `404`. В ответе есть `processed_at`, `webhook_delivered_at` и `metadata`.

Коллекция Bruno лежит в `bruno/`. Окружение `local`: `baseUrl` = `http://localhost:18080`, ключ `dev-api-key`. Запросы: создание и получение платежа.

## Идемпотентность

`Idempotency-Key` уникален. От тела запроса считается SHA-256 канонического JSON: ключи отсортированы, сумма нормализована до двух знаков.

- Новый ключ — создаётся платёж и одна строка outbox, ответ `202`.
- Тот же ключ и то же тело — снова `202` с исходным платежом. Вторая строка outbox не пишется.
- Тот же ключ и другое тело — `409`.
- Если два запроса с одним ключом приходят одновременно, уникальный индекс ловит гонку. Проигравший запрос перечитывает строку и применяет те же правила.

## Outbox

В одной транзакции: `INSERT` в `payments` и `INSERT` в `outbox` (`event_type = payment.created`).

Relay в lifespan API примерно раз в секунду забирает пачку `SELECT … FOR UPDATE SKIP LOCKED` со статусом `pending`, публикует persistent-сообщение в exchange `payments` с routing key `payments.new` и только после успешной публикации ставит `published`. Если процесс упадёт между publish и commit, сообщение уйдёт повторно. Consumer это переносит.

Сообщение: `event_id`, `event_type`, `payment_id`, `occurred_at`.

Частичный индекс `ix_outbox_pending_created_at` по `created_at` где `status = 'pending'`.

Колонка `payments.metadata` в модели называется `payment_metadata`: имя `metadata` занято у SQLAlchemy `DeclarativeBase`.

## Очереди

- direct exchange `payments`, durable
- очередь `payments.new` (routing key `payments.new`), DLX `payments.dlx` → `payments.dlq`
- direct exchange `payments.dlx` и очередь `payments.dlq`
- retry-очереди с TTL и DLX обратно в `payments` / `payments.new`:
  - `payments.retry.1s` — 1 с
  - `payments.retry.2s` — 2 с
  - `payments.retry.4s` — 4 с

Заголовок `x-attempt` — сколько повторов уже было. Первая доставка без заголовка считается нулём.

Техническая ошибка (база, сеть, HTTP-ошибка или таймаут webhook):

1. `x-attempt = 0` — ack и публикация в `payments.retry.1s` с `x-attempt = 1`
2. `x-attempt = 1` — ack и публикация в `payments.retry.2s` с `x-attempt = 2`
3. `x-attempt = 2` — ack и публикация в `payments.retry.4s` с `x-attempt = 3`
4. `x-attempt >= 3` — `reject(requeue=False)`, сообщение уходит в `payments.dlq`

Заголовки при dead-letter сохраняются.

Бизнес-отказ шлюза (около 10% при `PAYMENT_SUCCESS_RATE=0.9`) — это `status=failed`, webhook и ack. В DLQ такой платёж не попадает. В DLQ только технические сбои после трёх повторов.

## Consumer

Один подписчик на `payments.new`. Строка платежа не блокируется на время сна шлюза (2–5 с).

1. Платежа нет — ack.
2. `webhook_delivered_at` уже заполнен — ack, это дубль outbox.
3. `status = pending` — сон 2–5 с и успех с вероятностью `PAYMENT_SUCCESS_RATE`. Затем короткая транзакция `SELECT FOR UPDATE`: если статус всё ещё `pending`, пишутся `succeeded` или `failed` и `processed_at`.
4. POST webhook. Успех — `webhook_delivered_at` и ack. Ошибка — исключение и retry/DLQ. Повторная доставка шлюз заново не вызывает.

Тело webhook: `payment_id`, `status`, `amount`, `currency`, `processed_at`. Доставка at-least-once.

У consumer в Compose есть `extra_hosts: host.docker.internal:host-gateway`, поэтому `webhook_url` может указывать на процесс на хосте, например `http://host.docker.internal:18081/`.
