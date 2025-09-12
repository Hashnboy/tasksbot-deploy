"""SQLAlchemy models for penalty system."""
from datetime import datetime
from sqlalchemy import (
    Column, Integer, String, Boolean, DateTime, Numeric, Text, ForeignKey,
    Enum, JSON, Index
)
from sqlalchemy.orm import relationship

from app.tasks_bot import Base

StrictnessEnum = Enum('lenient', 'standard', 'strict', 'custom', name='policy_strictness')
SourceEnum = Enum(
    'late','missed_checkin','geofence_fail','media_blur','media_duplicate',
    'face_mismatch','task_reject','task_overdue','verify_sla_breach',
    'receiving_delay','receiving_mismatch','procurement_dup','anomaly_sales_stock',
    name='penalty_source'
)
SeverityEnum = Enum('low','medium','high','critical', name='penalty_severity')
LedgerStatusEnum = Enum('applied','waived','reversed', name='ledger_status')
AppealStatusEnum = Enum('open','approved','rejected', name='appeal_status')

class PenaltyPolicy(Base):
    __tablename__ = 'penalty_policies'
    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    is_active = Column(Boolean, default=True)
    scope = Column(JSON, default={})
    strictness = Column(StrictnessEnum, default='standard')
    rules = Column(JSON, default=list)
    caps = Column(JSON, default={})
    grace = Column(JSON, default={})
    forgiveness = Column(JSON, default={})
    escalation = Column(JSON, default={})
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PenaltyEvent(Base):
    __tablename__ = 'penalty_events'
    id = Column(Integer, primary_key=True)
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)
    user_id = Column(Integer, index=True, nullable=False)
    direction_id = Column(Integer, index=True, nullable=True)
    point_id = Column(Integer, index=True, nullable=True)
    source = Column(SourceEnum, nullable=False)
    payload = Column(JSON, default={})
    dedupe_key = Column(String(255), nullable=True, index=True)
    severity = Column(SeverityEnum, default='low')
    __table_args__ = (
        Index('idx_penalty_events_user_time', 'user_id', 'occurred_at'),
    )

class PenaltyLedger(Base):
    __tablename__ = 'penalty_ledger'
    id = Column(Integer, primary_key=True)
    event_id = Column(Integer, ForeignKey('penalty_events.id'), nullable=False)
    user_id = Column(Integer, index=True, nullable=False)
    policy_id = Column(Integer, ForeignKey('penalty_policies.id'), nullable=False)
    applied_at = Column(DateTime, default=datetime.utcnow, index=True)
    points = Column(Integer, default=0)
    amount = Column(Numeric(10,2), nullable=True)
    reasons = Column(JSON, default=list)
    status = Column(LedgerStatusEnum, default='applied')
    waiver_reason = Column(Text, nullable=True)
    reversed_by_user_id = Column(Integer, nullable=True)
    policy = relationship('PenaltyPolicy')
    event = relationship('PenaltyEvent')
    __table_args__ = (
        Index('idx_penalty_ledger_user_applied', 'user_id', 'applied_at'),
    )

class Appeal(Base):
    __tablename__ = 'appeals'
    id = Column(Integer, primary_key=True)
    ledger_id = Column(Integer, ForeignKey('penalty_ledger.id'), nullable=False)
    user_id = Column(Integer, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)
    status = Column(AppealStatusEnum, default='open')
    moderator_user_id = Column(Integer, nullable=True)
    decision_comment = Column(Text, nullable=True)
    decided_at = Column(DateTime, nullable=True)
    ledger = relationship('PenaltyLedger')

class Probation(Base):
    __tablename__ = 'probations'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    started_at = Column(DateTime, default=datetime.utcnow)
    ends_at = Column(DateTime, nullable=False)
    reason = Column(Text, nullable=True)
    policy_snapshot = Column(JSON, default={})
    is_active = Column(Boolean, default=True)

class Reward(Base):
    __tablename__ = 'rewards'
    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, nullable=False)
    points = Column(Integer, default=0)
    badge = Column(String(50), nullable=False)
    granted_at = Column(DateTime, default=datetime.utcnow)
    comment = Column(Text, nullable=True)

class KPISnapshot(Base):
    __tablename__ = 'kpi_snapshots'
    id = Column(Integer, primary_key=True)
    period_start = Column(DateTime, nullable=False)
    period_end = Column(DateTime, nullable=False)
    direction_id = Column(Integer, nullable=True)
    point_id = Column(Integer, nullable=True)
    user_id = Column(Integer, nullable=True)
    data = Column(JSON, default={})
    __table_args__ = (
        Index('uq_kpi_period', 'period_start', 'period_end', 'direction_id', 'point_id', 'user_id', unique=True),
    )
