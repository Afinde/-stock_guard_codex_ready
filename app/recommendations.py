from __future__ import annotations

import json
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from .db import IndustrySnapshotRecord, InstrumentRecord, MarketQuoteSnapshotRecord, SignalRecord

TZ = ZoneInfo("Asia/Shanghai")
BUY_SIGNALS = {"BUY_WATCH", "BUY_CONFIRM"}


def stock_recommendations(session: Session, *, phase: str = "manual", limit: int = 20) -> dict[str, Any]:
    rows = session.scalars(
        select(SignalRecord)
        .where(SignalRecord.signal_type.in_(BUY_SIGNALS))
        .order_by(SignalRecord.score.desc(), SignalRecord.generated_at.desc(), SignalRecord.id.desc())
        .limit(limit)
    ).all()
    instruments = _instruments(session, [row.symbol for row in rows])
    quotes = _latest_quotes(session, [row.symbol for row in rows])
    sectors = _leading_stock_sectors(session)
    items = []
    for row in rows:
        instrument = instruments.get(row.symbol)
        quote = quotes.get(row.symbol)
        sector = (instrument.industry if instrument and instrument.industry else "") or sectors.get(row.symbol, "")
        name = (instrument.name if instrument and instrument.name else "") or _quote_name(quote) or row.symbol
        items.append(
            {
                "symbol": row.symbol,
                "name": name,
                "sector": sector,
                "signal_type": row.signal_type or row.action,
                "score": row.score,
                "rank_score": row.score,
                "reference_price": row.reference_price if row.reference_price is not None else row.price,
                "latest_price": None if quote is None else quote.last_price,
                "market_time": "" if quote is None else _iso(quote.market_time),
                "stop_loss_price": row.stop_loss_price if row.stop_loss_price is not None else row.stop_price,
                "take_profit_1_price": row.take_profit_1_price if row.take_profit_1_price is not None else row.take_profit_1,
                "take_profit_2_price": row.take_profit_2_price if row.take_profit_2_price is not None else row.take_profit_2,
                "suggested_shares": row.suggested_shares,
                "reasons": _json(row.reasons, []),
                "invalidation_conditions": _json(row.invalidation_conditions, []),
                "generated_at": _iso(row.generated_at),
                "market_trade_date": row.market_trade_date or row.market_as_of_date or "",
                "research_only": True,
            }
        )
    return {"phase": phase, "generated_at": _iso(datetime.now(TZ)), "research_only": True, "items": items}


def sector_recommendations(session: Session, *, phase: str = "manual", limit: int = 20) -> dict[str, Any]:
    rows = session.scalars(
        select(IndustrySnapshotRecord)
        .where(IndustrySnapshotRecord.quality_status == "VALID")
        .order_by(IndustrySnapshotRecord.market_time.desc(), IndustrySnapshotRecord.change_pct.desc().nullslast())
        .limit(max(limit * 3, limit))
    ).all()
    seen: set[str] = set()
    items = []
    for row in rows:
        if row.industry_name in seen:
            continue
        seen.add(row.industry_name)
        items.append(
            {
                "sector": row.industry_name,
                "provider": row.provider,
                "rank_score": _rank_score(row),
                "change_pct": None if row.change_pct is None else str(row.change_pct),
                "turnover": None if row.turnover is None else str(row.turnover),
                "leading_stock": row.leading_stock or "",
                "market_time": _iso(row.market_time),
                "reason": _sector_reason(row),
                "research_only": True,
            }
        )
        if len(items) >= limit:
            break
    items.sort(key=lambda item: item["rank_score"], reverse=True)
    return {"phase": phase, "generated_at": _iso(datetime.now(TZ)), "research_only": True, "items": items}


def _instruments(session: Session, symbols: list[str]) -> dict[str, InstrumentRecord]:
    if not symbols:
        return {}
    rows = session.scalars(select(InstrumentRecord).where(InstrumentRecord.symbol.in_(set(symbols)))).all()
    return {row.symbol: row for row in rows}


def _latest_quotes(session: Session, symbols: list[str]) -> dict[str, MarketQuoteSnapshotRecord]:
    if not symbols:
        return {}
    rows = session.scalars(
        select(MarketQuoteSnapshotRecord)
        .where(MarketQuoteSnapshotRecord.symbol.in_(set(symbols)), MarketQuoteSnapshotRecord.quality_status == "VALID")
        .order_by(MarketQuoteSnapshotRecord.symbol.asc(), MarketQuoteSnapshotRecord.market_time.desc(), MarketQuoteSnapshotRecord.id.desc())
    ).all()
    latest: dict[str, MarketQuoteSnapshotRecord] = {}
    for row in rows:
        latest.setdefault(row.symbol, row)
    return latest


def _leading_stock_sectors(session: Session) -> dict[str, str]:
    rows = session.scalars(
        select(IndustrySnapshotRecord)
        .where(IndustrySnapshotRecord.leading_stock.is_not(None), IndustrySnapshotRecord.quality_status == "VALID")
        .order_by(IndustrySnapshotRecord.market_time.desc())
    ).all()
    result: dict[str, str] = {}
    for row in rows:
        if row.leading_stock:
            result.setdefault(row.leading_stock, row.industry_name)
    return result


def _rank_score(row: IndustrySnapshotRecord) -> float:
    change = float(row.change_pct or 0)
    turnover_boost = min(float(row.turnover or 0) / 1_000_000_000, 10.0)
    return round(change * 100 + turnover_boost, 6)


def _quote_name(row: MarketQuoteSnapshotRecord | None) -> str:
    if row is None:
        return ""
    payload = _json(row.payload_json, {})
    return str(payload.get("name", "")).strip()


def _sector_reason(row: IndustrySnapshotRecord) -> str:
    parts = []
    if row.change_pct is not None:
        parts.append(f"板块涨跌幅 {row.change_pct}")
    if row.turnover is not None:
        parts.append(f"成交额 {row.turnover}")
    if row.leading_stock:
        parts.append(f"领涨股 {row.leading_stock}")
    return "；".join(parts) if parts else "板块数据有效"


def _json(value: str | None, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def _iso(value: datetime | None) -> str:
    if value is None:
        return ""
    if value.tzinfo is None:
        value = value.replace(tzinfo=TZ)
    return value.isoformat()
