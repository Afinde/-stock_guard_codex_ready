from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, ROUND_DOWN
from enum import StrEnum
from typing import Any
from zoneinfo import ZoneInfo

from .data_provider import business_now
from .strategy import Signal, SignalType


MONEY_QUANT = Decimal("0.01")


class RiskStatus(StrEnum):
    APPROVED = "APPROVED"
    REDUCED = "REDUCED"
    REJECTED = "REJECTED"
    RISK_OFF = "RISK_OFF"
    INVALID_INPUT = "INVALID_INPUT"


class ProposedOrderStatus(StrEnum):
    PROPOSED = "PROPOSED"
    REVIEWED = "REVIEWED"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"


@dataclass(frozen=True)
class RiskPolicy:
    risk_per_trade: Decimal = Decimal("0.005")
    stop_loss_pct: Decimal = Decimal("0.05")
    max_symbol_weight: Decimal = Decimal("0.15")
    max_portfolio_weight: Decimal = Decimal("0.60")
    max_industry_weight: Decimal = Decimal("0.25")
    max_daily_loss_pct: Decimal = Decimal("0.02")
    max_consecutive_losses: int = 3
    reduce_risk_drawdown: Decimal = Decimal("0.08")
    risk_off_drawdown: Decimal = Decimal("0.12")
    reduced_max_portfolio_weight: Decimal = Decimal("0.30")
    lot_size: int = 100
    policy_version: str = "risk_policy_v1"

    def __post_init__(self) -> None:
        ratios = [
            self.risk_per_trade,
            self.stop_loss_pct,
            self.max_symbol_weight,
            self.max_portfolio_weight,
            self.max_industry_weight,
            self.max_daily_loss_pct,
            self.reduce_risk_drawdown,
            self.risk_off_drawdown,
            self.reduced_max_portfolio_weight,
        ]
        if any(ratio < 0 or ratio > 1 for ratio in ratios):
            raise ValueError("risk policy ratios must be between 0 and 1")
        if self.risk_per_trade >= self.max_symbol_weight:
            raise ValueError("risk_per_trade must be less than max_symbol_weight")
        if self.max_symbol_weight > self.max_portfolio_weight:
            raise ValueError("max_symbol_weight must not exceed max_portfolio_weight")
        if self.reduced_max_portfolio_weight > self.max_portfolio_weight:
            raise ValueError("reduced_max_portfolio_weight must not exceed max_portfolio_weight")
        if self.risk_off_drawdown <= self.reduce_risk_drawdown:
            raise ValueError("risk_off_drawdown must be greater than reduce_risk_drawdown")
        if self.max_consecutive_losses <= 0:
            raise ValueError("max_consecutive_losses must be a positive integer")
        if self.lot_size <= 0:
            raise ValueError("lot_size must be a positive integer")

    @property
    def snapshot(self) -> str:
        return stable_json(self.to_dict())

    @property
    def version(self) -> str:
        return hashlib.sha256(self.snapshot.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        return {key: decimal_to_str(value) for key, value in payload.items()}


@dataclass(frozen=True)
class PositionSnapshot:
    symbol: str
    quantity: int
    available_quantity: int
    average_cost: Decimal
    current_price: Decimal
    market_value: Decimal
    industry: str | None = None

    def __post_init__(self) -> None:
        if self.quantity < 0 or self.available_quantity < 0:
            raise ValueError("position quantity must not be negative")
        if self.available_quantity > self.quantity:
            raise ValueError("available_quantity must not exceed quantity")
        if self.average_cost < 0 or self.current_price < 0 or self.market_value < 0:
            raise ValueError("position money fields must not be negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "quantity": self.quantity,
            "available_quantity": self.available_quantity,
            "average_cost": decimal_to_str(self.average_cost),
            "current_price": decimal_to_str(self.current_price),
            "market_value": decimal_to_str(self.market_value),
            "industry": self.industry,
        }


@dataclass(frozen=True)
class AccountSnapshot:
    account_id: str
    as_of: datetime
    total_equity: Decimal
    available_cash: Decimal
    market_value: Decimal
    frozen_cash: Decimal
    daily_realized_pnl: Decimal
    daily_unrealized_pnl: Decimal
    peak_equity: Decimal
    consecutive_losses: int
    positions: tuple[PositionSnapshot, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if self.as_of.tzinfo is None:
            raise ValueError("account snapshot time must include timezone")
        if self.total_equity <= 0:
            raise ValueError("total_equity must be greater than 0")
        if self.available_cash < 0 or self.market_value < 0 or self.frozen_cash < 0:
            raise ValueError("cash and market values must not be negative")
        if self.peak_equity < 0:
            raise ValueError("peak_equity must not be negative")
        if self.consecutive_losses < 0:
            raise ValueError("consecutive_losses must not be negative")

    def symbol_market_value(self, symbol: str) -> Decimal:
        return sum((p.market_value for p in self.positions if p.symbol == symbol), Decimal("0"))

    def industry_market_value(self, industry: str | None) -> Decimal:
        if industry is None:
            return Decimal("0")
        return sum((p.market_value for p in self.positions if p.industry == industry), Decimal("0"))

    def to_dict(self) -> dict[str, Any]:
        return {
            "account_id": self.account_id,
            "as_of": self.as_of.isoformat(),
            "total_equity": decimal_to_str(self.total_equity),
            "available_cash": decimal_to_str(self.available_cash),
            "market_value": decimal_to_str(self.market_value),
            "frozen_cash": decimal_to_str(self.frozen_cash),
            "daily_realized_pnl": decimal_to_str(self.daily_realized_pnl),
            "daily_unrealized_pnl": decimal_to_str(self.daily_unrealized_pnl),
            "peak_equity": decimal_to_str(self.peak_equity),
            "consecutive_losses": self.consecutive_losses,
            "positions": [position.to_dict() for position in self.positions],
        }


@dataclass(frozen=True)
class RiskRuleResult:
    rule_name: str
    passed: bool
    actual_value: str
    limit_value: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class RiskDecision:
    decision_id: str
    status: str
    requested_quantity: int
    approved_quantity: int
    approved_notional: Decimal
    risk_amount: Decimal
    symbol_weight_after: Decimal
    portfolio_weight_after: Decimal
    industry_weight_after: Decimal | None
    rules: tuple[RiskRuleResult, ...]
    rejection_reasons: tuple[str, ...]
    signal_identity: str
    account_snapshot_hash: str
    risk_policy_version: str
    created_at: datetime

    def to_dict(self) -> dict[str, Any]:
        return {
            "decision_id": self.decision_id,
            "status": self.status,
            "requested_quantity": self.requested_quantity,
            "approved_quantity": self.approved_quantity,
            "approved_notional": decimal_to_str(self.approved_notional),
            "risk_amount": decimal_to_str(self.risk_amount),
            "symbol_weight_after": decimal_to_str(self.symbol_weight_after),
            "portfolio_weight_after": decimal_to_str(self.portfolio_weight_after),
            "industry_weight_after": (
                None if self.industry_weight_after is None else decimal_to_str(self.industry_weight_after)
            ),
            "rules": [rule.to_dict() for rule in self.rules],
            "rejection_reasons": list(self.rejection_reasons),
            "signal_identity": self.signal_identity,
            "account_snapshot_hash": self.account_snapshot_hash,
            "risk_policy_version": self.risk_policy_version,
            "created_at": self.created_at.isoformat(),
        }


@dataclass(frozen=True)
class ProposedOrder:
    proposal_id: str
    created_at: datetime
    expires_at: datetime
    symbol: str
    side: str
    quantity: int
    reference_price: Decimal
    stop_price: Decimal
    take_profit_1: Decimal
    take_profit_2: Decimal
    signal_identity: str
    risk_decision_id: str
    status: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "proposal_id": self.proposal_id,
            "created_at": self.created_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "symbol": self.symbol,
            "side": self.side,
            "quantity": self.quantity,
            "reference_price": decimal_to_str(self.reference_price),
            "stop_price": decimal_to_str(self.stop_price),
            "take_profit_1": decimal_to_str(self.take_profit_1),
            "take_profit_2": decimal_to_str(self.take_profit_2),
            "signal_identity": self.signal_identity,
            "risk_decision_id": self.risk_decision_id,
            "status": self.status,
        }


class RiskEngine:
    def evaluate(
        self,
        *,
        signal: Signal,
        account: AccountSnapshot,
        policy: RiskPolicy,
        reference_price: Decimal,
        stop_price: Decimal,
        industry: str | None = None,
    ) -> RiskDecision:
        rules: list[RiskRuleResult] = []
        rejection_reasons: list[str] = []
        signal_identity_value = signal_identity(signal)
        account_hash = account_snapshot_hash(account)
        created_at = business_now("Asia/Shanghai")

        def finish(status: RiskStatus, requested: int = 0, approved: int = 0) -> RiskDecision:
            approved_notional = money(reference_price * Decimal(approved))
            risk_amount = money((reference_price - stop_price) * Decimal(approved)) if approved else Decimal("0.00")
            symbol_weight = _weight(account.symbol_market_value(signal.symbol) + approved_notional, account.total_equity)
            portfolio_weight = _weight(account.market_value + approved_notional, account.total_equity)
            industry_weight = (
                _weight(account.industry_market_value(industry) + approved_notional, account.total_equity)
                if industry
                else None
            )
            return RiskDecision(
                decision_id=stable_id("risk", signal_identity_value, account_hash, policy.version, str(created_at)),
                status=status.value,
                requested_quantity=requested,
                approved_quantity=approved,
                approved_notional=approved_notional,
                risk_amount=risk_amount,
                symbol_weight_after=symbol_weight,
                portfolio_weight_after=portfolio_weight,
                industry_weight_after=industry_weight,
                rules=tuple(rules),
                rejection_reasons=tuple(rejection_reasons),
                signal_identity=signal_identity_value,
                account_snapshot_hash=account_hash,
                risk_policy_version=policy.version,
                created_at=created_at,
            )

        if signal.signal_type == SignalType.DATA_ERROR:
            rejection_reasons.append("DATA_ERROR信号不得进入仓位计算")
            return finish(RiskStatus.INVALID_INPUT)
        if signal.signal_type not in {SignalType.BUY_WATCH, SignalType.BUY_CONFIRM}:
            rejection_reasons.append("非买入类信号不得生成新的买入建议")
            return finish(RiskStatus.REJECTED)
        if stop_price >= reference_price:
            rejection_reasons.append("stop_price必须小于reference_price")
            return finish(RiskStatus.INVALID_INPUT)
        if reference_price <= 0:
            rejection_reasons.append("reference_price必须大于0")
            return finish(RiskStatus.INVALID_INPUT)
        if account.peak_equity <= 0:
            rejection_reasons.append("peak_equity必须大于0")
            return finish(RiskStatus.INVALID_INPUT)

        daily_loss = -(account.daily_realized_pnl + account.daily_unrealized_pnl)
        daily_loss_limit = money(account.total_equity * policy.max_daily_loss_pct)
        add_rule(
            rules,
            "daily_loss",
            daily_loss < daily_loss_limit,
            daily_loss,
            daily_loss_limit,
            "日内亏损未达到限制" if daily_loss < daily_loss_limit else "日内亏损达到限制",
        )
        if daily_loss >= daily_loss_limit:
            rejection_reasons.append("日内亏损达到限制")
            return finish(RiskStatus.RISK_OFF)

        add_rule(
            rules,
            "consecutive_losses",
            account.consecutive_losses < policy.max_consecutive_losses,
            Decimal(account.consecutive_losses),
            Decimal(policy.max_consecutive_losses),
            "连续亏损次数未达到限制" if account.consecutive_losses < policy.max_consecutive_losses else "连续亏损达到限制",
        )
        if account.consecutive_losses >= policy.max_consecutive_losses:
            rejection_reasons.append("连续亏损达到限制")
            return finish(RiskStatus.RISK_OFF)

        drawdown = (account.peak_equity - account.total_equity) / account.peak_equity
        add_rule(
            rules,
            "drawdown",
            drawdown < policy.risk_off_drawdown,
            drawdown,
            policy.risk_off_drawdown,
            "组合回撤未达到停止交易阈值" if drawdown < policy.risk_off_drawdown else "组合回撤达到停止交易阈值",
        )
        if drawdown >= policy.risk_off_drawdown:
            rejection_reasons.append("组合回撤达到停止交易阈值")
            return finish(RiskStatus.RISK_OFF)

        effective_portfolio_limit = (
            policy.reduced_max_portfolio_weight
            if drawdown >= policy.reduce_risk_drawdown
            else policy.max_portfolio_weight
        )
        add_rule(
            rules,
            "effective_portfolio_limit",
            True,
            effective_portfolio_limit,
            policy.max_portfolio_weight,
            "组合回撤进入降风险状态" if drawdown >= policy.reduce_risk_drawdown else "使用正常组合仓位上限",
        )

        per_share_risk = reference_price - stop_price
        risk_budget = money(account.total_equity * policy.risk_per_trade)
        risk_allowed = floor_to_lot(int(risk_budget / per_share_risk), policy.lot_size)
        cash_allowed = floor_to_lot(int(account.available_cash / reference_price), policy.lot_size)
        symbol_capacity_value = account.total_equity * policy.max_symbol_weight - account.symbol_market_value(signal.symbol)
        symbol_allowed = floor_to_lot(max(0, int(symbol_capacity_value / reference_price)), policy.lot_size)
        portfolio_capacity_value = account.total_equity * effective_portfolio_limit - account.market_value
        portfolio_allowed = floor_to_lot(max(0, int(portfolio_capacity_value / reference_price)), policy.lot_size)
        industry_allowed: int | None = None
        if industry:
            industry_capacity_value = account.total_equity * policy.max_industry_weight - account.industry_market_value(industry)
            industry_allowed = floor_to_lot(max(0, int(industry_capacity_value / reference_price)), policy.lot_size)

        requested = min(
            quantity
            for quantity in [risk_allowed, cash_allowed, symbol_allowed, portfolio_allowed, industry_allowed]
            if quantity is not None
        )
        approved = floor_to_lot(requested, policy.lot_size)
        approved_notional = money(reference_price * Decimal(approved))
        symbol_after = account.symbol_market_value(signal.symbol) + approved_notional
        portfolio_after = account.market_value + approved_notional
        industry_after = account.industry_market_value(industry) + approved_notional if industry else Decimal("0")

        add_rule(rules, "risk_budget", risk_allowed >= policy.lot_size, Decimal(risk_allowed), Decimal(policy.lot_size), "单笔风险预算允许交易单位")
        add_rule(rules, "available_cash", approved_notional <= account.available_cash, approved_notional, account.available_cash, "未超过可用现金")
        add_rule(rules, "symbol_weight", symbol_after <= account.total_equity * policy.max_symbol_weight, _weight(symbol_after, account.total_equity), policy.max_symbol_weight, "未超过单票仓位")
        add_rule(rules, "portfolio_weight", portfolio_after <= account.total_equity * effective_portfolio_limit, _weight(portfolio_after, account.total_equity), effective_portfolio_limit, "未超过组合仓位")
        if industry:
            add_rule(rules, "industry_weight", industry_after <= account.total_equity * policy.max_industry_weight, _weight(industry_after, account.total_equity), policy.max_industry_weight, "未超过行业集中度")
        else:
            add_rule(rules, "industry_weight", True, Decimal("0"), policy.max_industry_weight, "行业为空，行业集中度规则未执行")

        if approved < policy.lot_size:
            rejection_reasons.append("批准数量不足一个交易单位")
            return finish(RiskStatus.REJECTED, requested=requested, approved=0)

        status = RiskStatus.REDUCED if drawdown >= policy.reduce_risk_drawdown else RiskStatus.APPROVED
        return finish(status, requested=requested, approved=approved)


def create_order_proposal(
    *,
    signal: Signal,
    decision: RiskDecision,
    now: datetime | None = None,
    ttl_minutes: int = 30,
) -> ProposedOrder | None:
    if signal.signal_type not in {SignalType.BUY_WATCH, SignalType.BUY_CONFIRM}:
        return None
    if decision.status not in {RiskStatus.APPROVED.value, RiskStatus.REDUCED.value}:
        return None
    if decision.approved_quantity <= 0:
        return None
    created_at = now or business_now("Asia/Shanghai")
    if created_at.tzinfo is None:
        raise ValueError("created_at must include timezone")
    proposal_id = stable_id("proposal", decision.signal_identity, decision.decision_id)
    return ProposedOrder(
        proposal_id=proposal_id,
        created_at=created_at,
        expires_at=created_at + timedelta(minutes=ttl_minutes),
        symbol=signal.symbol,
        side="BUY",
        quantity=decision.approved_quantity,
        reference_price=Decimal(str(signal.reference_price)),
        stop_price=Decimal(str(signal.stop_loss_price)),
        take_profit_1=Decimal(str(signal.take_profit_1_price)),
        take_profit_2=Decimal(str(signal.take_profit_2_price)),
        signal_identity=decision.signal_identity,
        risk_decision_id=decision.decision_id,
        status=ProposedOrderStatus.PROPOSED.value,
    )


def signal_identity(signal: Signal) -> str:
    return "|".join(
        [
            signal.symbol,
            signal.market_as_of_date.isoformat(),
            signal.strategy_name,
            signal.strategy_version,
            signal.parameter_version,
            signal.signal_type,
        ]
    )


def account_snapshot_hash(account: AccountSnapshot) -> str:
    return hashlib.sha256(stable_json(account.to_dict()).encode("utf-8")).hexdigest()[:16]


def add_rule(
    rules: list[RiskRuleResult],
    rule_name: str,
    passed: bool,
    actual_value: Decimal,
    limit_value: Decimal,
    reason: str,
) -> None:
    rules.append(
        RiskRuleResult(
            rule_name=rule_name,
            passed=passed,
            actual_value=decimal_to_str(actual_value),
            limit_value=decimal_to_str(limit_value),
            reason=reason,
        )
    )


def floor_to_lot(quantity: int, lot_size: int) -> int:
    if quantity <= 0:
        return 0
    return (quantity // lot_size) * lot_size


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANT, rounding=ROUND_DOWN)


def _weight(value: Decimal, total: Decimal) -> Decimal:
    return (value / total).quantize(Decimal("0.000001"), rounding=ROUND_DOWN)


def decimal_to_str(value: Any) -> Any:
    if isinstance(value, Decimal):
        return format(value, "f")
    return value


def stable_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def stable_id(prefix: str, *parts: str) -> str:
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"{prefix}_{digest}"


def parse_decimal(value: Any) -> Decimal:
    return Decimal(str(value))


def now_shanghai() -> datetime:
    return datetime.now(ZoneInfo("Asia/Shanghai"))
