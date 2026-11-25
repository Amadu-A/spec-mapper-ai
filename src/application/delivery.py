# src/application/delivery.py
"""Общий bounded dispatcher всех пяти видов заданий; at-least-once, conditional confirm."""

import asyncio
import logging


async def dispatch_once(jobs, broker, limit: int = 4) -> int:
    """Сетевой publish отделён от reservation/confirm; ошибка не блокирует следующие jobs."""
    logger = logging.getLogger(__name__)
    count = 0
    for _ in range(limit):
        attempt = await jobs.reserve_delivery()
        if attempt is None:
            break
        context = {
            "job_id": attempt["job_id"],
            "generation": attempt["generation"],
            "attempt": attempt["attempt"],
        }
        logger.info("delivery_attempt", extra=context)
        try:
            async with asyncio.timeout(jobs.settings.delivery_publish_timeout):
                await broker.publish(attempt["job_id"], generation=attempt["generation"])
        except Exception as error:
            code = (
                "broker_timeout"
                if isinstance(error, TimeoutError)
                else "broker_" + type(error).__name__
            )
            await jobs.delivery_failure(attempt, code)
            logger.warning("delivery_retry", extra={**context, "error_code": code})
        else:
            await jobs.confirm_delivery(attempt)
            logger.info("delivery_confirm", extra=context)
            count += 1
    return count
