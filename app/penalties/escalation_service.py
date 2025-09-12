"""Escalation helpers.

These functions are thin wrappers that will later integrate with
Telegram notifications or internal messaging. For now they simply log
calls; this keeps backward compatibility while allowing unit tests to
hook into them."""

import logging
from datetime import datetime, timedelta

log = logging.getLogger(__name__)


def warn(user_id: int, reason: str) -> None:
    log.info("warn user=%s: %s", user_id, reason)


def notify_admins(message: str) -> None:
    log.info("notify_admins: %s", message)


def start_probation(user_id: int, days: int, reason: str) -> None:
    ends = datetime.utcnow() + timedelta(days=days)
    log.info("start_probation user=%s until=%s reason=%s", user_id, ends, reason)


def suggest_suspension(user_id: int, summary: str) -> None:
    log.info("suggest_suspension user=%s: %s", user_id, summary)
