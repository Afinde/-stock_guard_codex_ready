from __future__ import annotations

from dataclasses import asdict, dataclass

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class Signal:
    symbol: str
    action: str
    score: float
    price: float
    stop_price: float
    take_profit_1: float
    take_profit_2: float
    suggested_shares: int
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


def rsi(close: pd.Series, period: int = 14) -> pd.Series:
    delta = close.diff()
    gain = delta.clip(lower=0).rolling(period).mean()
    loss = -delta.clip(upper=0).rolling(period).mean()
    rs = gain / loss.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def enrich(df: pd.DataFrame) -> pd.DataFrame:
    out = df.copy()
    out["ma20"] = out["close"].rolling(20).mean()
    out["ma60"] = out["close"].rolling(60).mean()
    out["mom20"] = out["close"].pct_change(20)
    out["vol20"] = out["close"].pct_change().rolling(20).std()
    out["volume_ma20"] = out["volume"].rolling(20).mean()
    out["volume_ratio"] = out["volume"] / out["volume_ma20"]
    out["rsi14"] = rsi(out["close"], 14)
    out["high20"] = out["high"].rolling(20).max().shift(1)
    return out


def calculate_position_shares(
    account_equity: float,
    entry_price: float,
    stop_loss_pct: float,
    risk_per_trade: float,
    max_single_position_pct: float,
) -> int:
    if entry_price <= 0 or stop_loss_pct <= 0:
        return 0
    risk_budget = account_equity * risk_per_trade
    risk_per_share = entry_price * stop_loss_pct
    shares_by_risk = int(risk_budget / risk_per_share)
    shares_by_cap = int((account_equity * max_single_position_pct) / entry_price)
    raw = max(0, min(shares_by_risk, shares_by_cap))
    return (raw // 100) * 100  # A-share board lot


def generate_signal(
    symbol: str,
    history: pd.DataFrame,
    account_equity: float,
    risk_per_trade: float = 0.005,
    stop_loss_pct: float = 0.05,
    max_single_position_pct: float = 0.15,
) -> Signal:
    df = enrich(history)
    if len(df) < 80:
        raise ValueError(f"Insufficient history for {symbol}; need at least 80 bars")

    row = df.iloc[-1]
    score = 0.0
    reasons: list[str] = []

    if row.close > row.ma20 > row.ma60:
        score += 30
        reasons.append("趋势向上：收盘价>MA20>MA60")
    if 0.03 <= row.mom20 <= 0.18:
        score += 20
        reasons.append("20日动量处于温和区间")
    if row.vol20 <= 0.035:
        score += 15
        reasons.append("20日波动率受控")
    if 1.1 <= row.volume_ratio <= 2.5:
        score += 15
        reasons.append("量能温和放大")
    if 45 <= row.rsi14 <= 68:
        score += 10
        reasons.append("RSI未明显过热")
    if row.close > row.high20:
        score += 10
        reasons.append("突破前20日高点")

    price = round(float(row.close), 2)
    action = "BUY_WATCH" if score >= 70 else "HOLD"
    shares = calculate_position_shares(
        account_equity=account_equity,
        entry_price=price,
        stop_loss_pct=stop_loss_pct,
        risk_per_trade=risk_per_trade,
        max_single_position_pct=max_single_position_pct,
    ) if action == "BUY_WATCH" else 0

    return Signal(
        symbol=symbol,
        action=action,
        score=round(score, 2),
        price=price,
        stop_price=round(price * (1 - stop_loss_pct), 2),
        take_profit_1=round(price * 1.05, 2),
        take_profit_2=round(price * 1.08, 2),
        suggested_shares=shares,
        reason="；".join(reasons) or "未满足入选条件",
    )


def evaluate_exit(entry_price: float, current_price: float, highest_price: float) -> tuple[str, str]:
    if current_price <= entry_price * 0.95:
        return "SELL", "触发固定5%止损"
    if highest_price >= entry_price * 1.08 and current_price <= highest_price * 0.97:
        return "SELL", "达到第二目标后回撤3%，触发移动止盈"
    if current_price >= entry_price * 1.05:
        return "REDUCE", "达到第一目标，建议分批减仓而非一次清仓"
    return "HOLD", "未触发退出条件"
