"""Simple policy engine for penalty evaluation."""
from __future__ import annotations
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from sqlalchemy import func
from sqlalchemy.orm import Session

from .models import PenaltyPolicy, PenaltyEvent, PenaltyLedger

class PolicyEngine:
    """Evaluates penalty events against active policies."""

    def __init__(self, session: Session):
        self.session = session
        # cooldown tracker: {(user_id, source): datetime}
        self._cooldowns: Dict[tuple, datetime] = {}

    # --- policy loading ---
    def policies_for(self, event: PenaltyEvent) -> List[PenaltyPolicy]:
        """Return active policies matching event scope. Simplified implementation."""
        policies = (
            self.session.query(PenaltyPolicy)
            .filter(PenaltyPolicy.is_active == True)  # noqa: E712
            .all()
        )
        # Scope matching simplified: check direction/point if provided
        matched = []
        for p in policies:
            scope = p.scope or {}
            dirs = scope.get("direction_ids")
            pts = scope.get("point_ids")
            if dirs and event.direction_id not in dirs:
                continue
            if pts and event.point_id not in pts:
                continue
            matched.append(p)
        return matched

    # --- evaluation ---
    def apply_event(self, event: PenaltyEvent) -> List[PenaltyLedger]:
        """Evaluate policies for event and write ledger entries."""
        ledgers: List[PenaltyLedger] = []
        policies = self.policies_for(event)
        for policy in policies:
            for rule in policy.rules or []:
                if rule.get("when") != event.source:
                    continue
                if not self._threshold_pass(rule.get("thresholds", {}), event.payload or {}):
                    continue
                if self._grace_applies(event, rule):
                    continue
                if self._cooldown_applies(event, rule):
                    continue
                if self._dedupe_applies(event):
                    continue
                points = rule.get("points", 0)
                amount = rule.get("amount")
                per_cap = rule.get("per_occurrence_cap", {})
                if per_cap.get("points") is not None:
                    points = min(points, per_cap["points"])
                if per_cap.get("amount") is not None and amount is not None:
                    amount = min(amount, per_cap["amount"])
                # caps
                points, amount = self._apply_caps(event.user_id, policy, points, amount)
                if points == 0 and not amount:
                    continue
                ledger = PenaltyLedger(
                    event=event,
                    user_id=event.user_id,
                    policy=policy,
                    points=points,
                    amount=amount,
                    reasons=[event.source],
                )
                self.session.add(ledger)
                ledgers.append(ledger)
                self._cooldowns[(event.user_id, event.source)] = datetime.utcnow()
        if ledgers:
            self.session.commit()
        return ledgers

    # --- helpers ---
    def _threshold_pass(self, thresholds: Dict, payload: Dict) -> bool:
        for key, val in thresholds.items():
            metric = payload.get(key.split("_", 1)[1]) if "_" in key else payload.get(key)
            if metric is None:
                return False
            if key.startswith("gt_") and not (metric > val):
                return False
            if key.startswith("lt_") and not (metric < val):
                return False
        return True

    def _grace_applies(self, event: PenaltyEvent, rule: Dict) -> bool:
        grace = rule.get("grace") or {}
        count_per_day = grace.get("count_per_day")
        if not count_per_day:
            return False
        start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        count = (
            self.session.query(PenaltyEvent)
            .filter(
                PenaltyEvent.user_id == event.user_id,
                PenaltyEvent.source == event.source,
                PenaltyEvent.occurred_at >= start,
            )
            .count()
        )
        return count <= count_per_day

    def _cooldown_applies(self, event: PenaltyEvent, rule: Dict) -> bool:
        cd = rule.get("cooldown_min")
        if not cd:
            return False
        key = (event.user_id, event.source)
        last = self._cooldowns.get(key)
        if last and datetime.utcnow() - last < timedelta(minutes=cd):
            return True
        return False

    def _dedupe_applies(self, event: PenaltyEvent) -> bool:
        if not event.dedupe_key:
            return False
        exists = (
            self.session.query(PenaltyEvent)
            .filter(PenaltyEvent.dedupe_key == event.dedupe_key, PenaltyEvent.id != event.id)
            .first()
        )
        return bool(exists)

    def _apply_caps(self, user_id: int, policy: PenaltyPolicy, points: int, amount: Optional[float]):
        caps = policy.caps or {}
        today = datetime.utcnow().date()
        start = datetime(today.year, today.month, today.day)
        total_points = (
            self.session.query(PenaltyLedger)
            .filter(
                PenaltyLedger.user_id == user_id,
                PenaltyLedger.policy_id == policy.id,
                PenaltyLedger.applied_at >= start,
            )
            .with_entities(func.coalesce(func.sum(PenaltyLedger.points), 0))
            .scalar()
        )
        max_daily = caps.get("daily", {}).get("points")
        if max_daily is not None and total_points >= max_daily:
            return 0, None
        if max_daily is not None:
            points = min(points, max_daily - total_points)
        if amount is not None:
            max_amt = caps.get("month", {}).get("amount")
            if max_amt is not None:
                # naive monthly calculation
                mstart = start.replace(day=1)
                month_amt = (
                    self.session.query(PenaltyLedger)
                    .filter(
                        PenaltyLedger.user_id == user_id,
                        PenaltyLedger.policy_id == policy.id,
                        PenaltyLedger.applied_at >= mstart,
                    )
                    .with_entities(func.coalesce(func.sum(PenaltyLedger.amount), 0))
                    .scalar()
                )
                if month_amt >= max_amt:
                    amount = None
                else:
                    amount = min(amount, max_amt - float(month_amt))
        return points, amount