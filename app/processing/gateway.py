import asyncio
import logging
import random
from typing import Literal

logger = logging.getLogger(__name__)

GatewayOutcome = Literal["succeeded", "failed"]


async def emulate_gateway(success_rate: float) -> GatewayOutcome:
    delay = random.uniform(2.0, 5.0)
    logger.info("gateway emulation sleeping %.2fs", delay)
    await asyncio.sleep(delay)
    if random.random() < success_rate:
        return "succeeded"
    return "failed"
