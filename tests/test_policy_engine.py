import os
import sys, pathlib
sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))
os.environ.setdefault("TELEGRAM_TOKEN","test")
os.environ.setdefault("DATABASE_URL","sqlite:///:memory:")
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.penalties.models import Base, PenaltyPolicy, PenaltyEvent, PenaltyLedger
from app.penalties.policy_engine import PolicyEngine


def setup_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def seed_policy(session, with_grace=True):
    policy = PenaltyPolicy(
        name='test',
        rules=[{
            'when': 'late',
            'thresholds': {'gt_minutes': 5},
            'points': 5,
            'grace': {'count_per_day': 1} if with_grace else {},
            'per_occurrence_cap': {'points': 10},
        }],
        caps={'daily': {'points': 10}}
    )
    session.add(policy)
    session.commit()
    return policy


def test_grace_and_caps():
    sess = setup_session()
    seed_policy(sess)
    engine = PolicyEngine(sess)
    for i in range(4):
        e = PenaltyEvent(user_id=1, source='late', payload={'minutes': 10})
        sess.add(e); sess.commit()
        engine.apply_event(e)
    rows = sess.query(PenaltyLedger).all()
    assert len(rows) == 2
    assert sum(r.points for r in rows) == 10


def test_dedupe():
    sess = setup_session()
    seed_policy(sess, with_grace=False)
    engine = PolicyEngine(sess)
    e1 = PenaltyEvent(user_id=1, source='late', payload={'minutes': 10}, dedupe_key='a')
    sess.add(e1); sess.commit(); engine.apply_event(e1)
    e2 = PenaltyEvent(user_id=1, source='late', payload={'minutes': 10}, dedupe_key='a')
    sess.add(e2); sess.commit(); engine.apply_event(e2)
    rows = sess.query(PenaltyLedger).all()
    assert len(rows) == 1
