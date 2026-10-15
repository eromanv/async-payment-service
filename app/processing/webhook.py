import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)


async def deliver_webhook(url: str, payload: dict[str, Any]) -> None:
    timeout = httpx.Timeout(10.0, connect=5.0)
    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, json=payload)
        response.raise_for_status()
    logger.info("webhook delivered url=%s status_code=%s", url, response.status_code)
