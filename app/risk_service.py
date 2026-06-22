from __future__ import annotations

import json
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from .db import ProposedOrderRecord, RiskDecisionRecord, SessionLocal
from .risk import (
    AccountSnapshot,
    ProposedOrder,
    RiskDecision,
    RiskPolicy,
    decimal_to_str,
)


def save_risk_decision(
    decision: RiskDecision,
    account: AccountSnapshot,
    policy: RiskPolicy,
) -> None:
    with SessionLocal() as session:
        existing = session.scalars(
            select(RiskDecisionRecord)
            .where(
                RiskDecisionRecord.signal_identity == decision.signal_identity,
                RiskDecisionRecord.account_snapshot_hash == decision.account_snapshot_hash,
                RiskDecisionRecord.risk_policy_version == decision.risk_policy_version,
            )
            .limit(1)
        ).first()
        if existing is not None:
            return
        record = RiskDecisionRecord(
            decision_id=decision.decision_id,
            signal_identity=decision.signal_identity,
            account_snapshot_hash=decision.account_snapshot_hash,
            account_snapshot_json=json.dumps(account.to_dict(), ensure_ascii=False, sort_keys=True),
            risk_policy_version=decision.risk_policy_version,
            risk_policy_snapshot=policy.snapshot,
            status=decision.status,
            requested_quantity=decision.requested_quantity,
            approved_quantity=decision.approved_quantity,
            approved_notional=decimal_to_str(decision.approved_notional),
            risk_amount=decimal_to_str(decision.risk_amount),
            rules_json=json.dumps([rule.to_dict() for rule in decision.rules], ensure_ascii=False),
            rejection_reasons_json=json.dumps(list(decision.rejection_reasons), ensure_ascii=False),
            created_at=decision.created_at,
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def save_order_proposal(order: ProposedOrder) -> None:
    with SessionLocal() as session:
        existing = session.scalars(
            select(ProposedOrderRecord)
            .where(
                ProposedOrderRecord.signal_identity == order.signal_identity,
                ProposedOrderRecord.risk_decision_id == order.risk_decision_id,
            )
            .limit(1)
        ).first()
        if existing is not None:
            return
        record = ProposedOrderRecord(
            proposal_id=order.proposal_id,
            signal_identity=order.signal_identity,
            risk_decision_id=order.risk_decision_id,
            symbol=order.symbol,
            side=order.side,
            quantity=order.quantity,
            reference_price=decimal_to_str(order.reference_price),
            stop_price=decimal_to_str(order.stop_price),
            take_profit_1=decimal_to_str(order.take_profit_1),
            take_profit_2=decimal_to_str(order.take_profit_2),
            status=order.status,
            created_at=order.created_at,
            expires_at=order.expires_at,
        )
        session.add(record)
        try:
            session.commit()
        except IntegrityError:
            session.rollback()


def latest_risk_decisions(limit: int = 50) -> list[dict]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(RiskDecisionRecord).order_by(RiskDecisionRecord.created_at.desc()).limit(limit)
        ).all()
        return [
            {
                "decision_id": row.decision_id,
                "signal_identity": row.signal_identity,
                "risk_policy_version": row.risk_policy_version,
                "status": row.status,
                "requested_quantity": row.requested_quantity,
                "approved_quantity": row.approved_quantity,
                "approved_notional": row.approved_notional,
                "risk_amount": row.risk_amount,
                "rules": json.loads(row.rules_json),
                "rejection_reasons": json.loads(row.rejection_reasons_json),
                "created_at": row.created_at.isoformat(),
                "notice": "仅为研究和风险控制建议，不构成收益保证或实际委托",
            }
            for row in rows
        ]


def latest_order_proposals(limit: int = 50) -> list[dict]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(ProposedOrderRecord).order_by(ProposedOrderRecord.created_at.desc()).limit(limit)
        ).all()
        return [
            {
                "proposal_id": row.proposal_id,
                "signal_identity": row.signal_identity,
                "risk_decision_id": row.risk_decision_id,
                "symbol": row.symbol,
                "side": row.side,
                "quantity": row.quantity,
                "reference_price": row.reference_price,
                "stop_price": row.stop_price,
                "take_profit_1": row.take_profit_1,
                "take_profit_2": row.take_profit_2,
                "status": row.status,
                "created_at": row.created_at.isoformat(),
                "expires_at": row.expires_at.isoformat(),
                "notice": "仅为研究和风险控制建议，不构成收益保证或实际委托",
            }
            for row in rows
        ]


def decimal_from_api(value) -> Decimal:
    return Decimal(str(value))
