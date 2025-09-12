"""Forgiveness helpers and point decay."""
from datetime import datetime, timedelta
from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import PenaltyLedger


def apply_forgiveness(session: Session, user_id: int, ledger: PenaltyLedger, rule: dict) -> None:
    """Apply streak-based forgiveness to ledger in-place."""
    fg = rule.get("forgiveness") or {}
    streak_days = _current_streak(session, user_id)
    if streak_days >= fg.get("streak_days_to_waive", 0):
        pct = fg.get("waive_percent", 0)
        ledger.points = int(ledger.points * (100 - pct) / 100)
        if ledger.amount:
            ledger.amount = ledger.amount * (100 - pct) / 100


def _current_streak(session: Session, user_id: int) -> int:
    last_penalty = (
        session.query(PenaltyLedger)
        .filter(PenaltyLedger.user_id == user_id)
        .order_by(PenaltyLedger.applied_at.desc())
        .first()
    )
    if not last_penalty:
        return 0
    days = (datetime.utcnow() - last_penalty.applied_at).days
    return days


def decay_weekly(session: Session, user_id: int, points: int) -> None:
    """Subtract points weekly. Simple implementation."""
    if points <= 0:
        return
    session.query(PenaltyLedger).filter(
        PenaltyLedger.user_id == user_id,
        PenaltyLedger.status == 'applied',
    ).update({PenaltyLedger.points: PenaltyLedger.points - points})
    session.commit()
