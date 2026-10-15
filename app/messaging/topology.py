from faststream.rabbit import Channel, ExchangeType, RabbitBroker, RabbitExchange, RabbitQueue

from app.core.config import get_settings

ATTEMPT_HEADER = "x-attempt"
ROUTING_KEY_NEW = "payments.new"
ROUTING_KEY_DLQ = "payments.dlq"

payments_exchange = RabbitExchange(
    "payments",
    type=ExchangeType.DIRECT,
    durable=True,
)
dlx_exchange = RabbitExchange(
    "payments.dlx",
    type=ExchangeType.DIRECT,
    durable=True,
)

payments_new_queue = RabbitQueue(
    "payments.new",
    durable=True,
    routing_key=ROUTING_KEY_NEW,
    arguments={
        "x-dead-letter-exchange": "payments.dlx",
        "x-dead-letter-routing-key": ROUTING_KEY_DLQ,
    },
)
payments_dlq = RabbitQueue(
    "payments.dlq",
    durable=True,
    routing_key=ROUTING_KEY_DLQ,
)


def _retry_queue(name: str, ttl_ms: int) -> RabbitQueue:
    return RabbitQueue(
        name,
        durable=True,
        arguments={
            "x-message-ttl": ttl_ms,
            "x-dead-letter-exchange": "payments",
            "x-dead-letter-routing-key": ROUTING_KEY_NEW,
        },
    )


retry_queues: tuple[RabbitQueue, ...] = (
    _retry_queue("payments.retry.1s", 1_000),
    _retry_queue("payments.retry.2s", 2_000),
    _retry_queue("payments.retry.4s", 4_000),
)
RETRY_QUEUE_NAMES: tuple[str, ...] = tuple(queue.name for queue in retry_queues)

broker = RabbitBroker(
    get_settings().rabbitmq_url,
    default_channel=Channel(prefetch_count=10, on_return_raises=True),
)


async def declare_topology(rabbit: RabbitBroker) -> None:
    payments = await rabbit.declare_exchange(payments_exchange)
    dlx = await rabbit.declare_exchange(dlx_exchange)

    dlq = await rabbit.declare_queue(payments_dlq)
    await dlq.bind(dlx, routing_key=ROUTING_KEY_DLQ)

    new_queue = await rabbit.declare_queue(payments_new_queue)
    await new_queue.bind(payments, routing_key=ROUTING_KEY_NEW)

    for queue in retry_queues:
        await rabbit.declare_queue(queue)
