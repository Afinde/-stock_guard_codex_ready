from __future__ import annotations

from sqlalchemy import select

from .config import get_settings
from .data_provider import MarketDataError, fetch_daily_history
from .db import SessionLocal, SignalRecord
from .strategy import Signal, generate_signal


def scan_watchlist() -> list[dict]:
    settings = get_settings()
    results: list[dict] = []

    for symbol in settings.watchlist:
        try:
            history = fetch_daily_history(symbol)
            signal = generate_signal(
                symbol=symbol,
                history=history,
                account_equity=settings.account_equity,
                risk_per_trade=settings.risk_per_trade,
                stop_loss_pct=settings.stop_loss_pct,
                max_single_position_pct=settings.max_single_position_pct,
            )
            save_signal(signal)
            results.append(signal.to_dict())
        except (MarketDataError, ValueError) as exc:
            results.append({"symbol": symbol, "action": "DATA_ERROR", "reason": str(exc)})

    return sorted(results, key=lambda x: x.get("score", -1), reverse=True)


def save_signal(signal: Signal) -> None:
    with SessionLocal() as session:
        record = SignalRecord(**signal.to_dict())
        session.add(record)
        session.commit()


def latest_signals(limit: int = 50) -> list[dict]:
    with SessionLocal() as session:
        rows = session.scalars(
            select(SignalRecord).order_by(SignalRecord.generated_at.desc()).limit(limit)
        ).all()
        return [
            {
                "id": row.id,
                "symbol": row.symbol,
                "generated_at": row.generated_at.isoformat(),
                "action": row.action,
                "score": row.score,
                "price": row.price,
                "stop_price": row.stop_price,
                "take_profit_1": row.take_profit_1,
                "take_profit_2": row.take_profit_2,
                "suggested_shares": row.suggested_shares,
                "reason": row.reason,
            }
            for row in rows
        ]
