"""Appeals workflow."""
from datetime import datetime
from sqlalchemy.orm import Session

from .models import Appeal, PenaltyLedger


def create_appeal(session: Session, ledger_id: int, user_id: int) -> Appeal:
    appeal = Appeal(ledger_id=ledger_id, user_id=user_id, created_at=datetime.utcnow())
    session.add(appeal)
    session.commit()
    return appeal


def resolve_appeal(session: Session, appeal_id: int, moderator_id: int, approve: bool, comment: str) -> Appeal:
    appeal = session.get(Appeal, appeal_id)
    if not appeal:
        raise ValueError("appeal not found")
    appeal.status = 'approved' if approve else 'rejected'
    appeal.moderator_user_id = moderator_id
    appeal.decision_comment = comment
    appeal.decided_at = datetime.utcnow()
    if approve:
        ledger = session.get(PenaltyLedger, appeal.ledger_id)
        if ledger:
            ledger.status = 'waived'
    session.commit()
    return appeal
