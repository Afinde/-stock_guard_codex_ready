from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

import pandas as pd

from .data_provider import RecoverableProviderError


class PublicMarketProvider(Protocol):
    name: str

    def fetch_spot_quotes(self) -> list["SpotQuote"]:
        ...

    def fetch_industries(self) -> list["IndustrySnapshot"]:
        ...

    def fetch_stock_news(self, symbols: list[str]) -> list["StockNews"]:
        ...


@dataclass(frozen=True)
class SpotQuote:
    provider: str
    symbol: str
    name: str
    exchange: str
    market_time: datetime
    open_price: Decimal
    high_price: Decimal
    low_price: Decimal
    last_price: Decimal
    previous_close: Decimal | None
    volume: int
    amount: Decimal | None
    checksum: str


@dataclass(frozen=True)
class IndustrySnapshot:
    provider: str
    industry_name: str
    market_time: datetime
    change_pct: Decimal | None
    turnover: Decimal | None
    leading_stock: str | None
    checksum: str


@dataclass(frozen=True)
class StockNews:
    provider: str
    symbol: str
    title: str
    summary: str
    source_url: str
    source_url_hash: str
    published_at: datetime | None
    checksum: str


class FixtureProvider:
    name = "fixture"

    def fetch_spot_quotes(self) -> list[SpotQuote]:
        now = datetime(2026, 1, 5, 15, 1, tzinfo=timezone.utc)
        return [
            make_spot_quote(self.name, "600519.SH", "贵州茅台", now, "1700.00", "1710.00", "1688.00", "1705.00", "1690.00", 100000, "170500000.00"),
            make_spot_quote(self.name, "000858.SZ", "五粮液", now, "130.00", "132.00", "129.00", "131.00", "130.50", 500000, "65500000.00"),
        ]

    def fetch_industries(self) -> list[IndustrySnapshot]:
        now = datetime(2026, 1, 5, 15, 1, tzinfo=timezone.utc)
        return [make_industry(self.name, "白酒", now, "0.0123", "800000000.00", "600519.SH")]

    def fetch_stock_news(self, symbols: list[str]) -> list[StockNews]:
        now = datetime(2026, 1, 5, 10, 0, tzinfo=timezone.utc)
        return [make_news(self.name, symbol, f"{symbol} 公告摘要", "fixture news", f"https://example.invalid/{symbol}", now) for symbol in symbols[:3]]


class AkShareEastMoneyProvider:
    name = "eastmoney"

    def fetch_spot_quotes(self) -> list[SpotQuote]:
        try:
            import akshare as ak

            frame = ak.stock_zh_a_spot_em()
        except Exception as exc:
            raise RecoverableProviderError(f"eastmoney spot fetch failed: {type(exc).__name__}") from exc
        return normalize_eastmoney_spot(frame, self.name)

    def fetch_stock_news(self, symbols: list[str]) -> list[StockNews]:
        try:
            import akshare as ak
        except Exception as exc:
            raise RecoverableProviderError(f"akshare import failed: {type(exc).__name__}") from exc
        rows: list[StockNews] = []
        for symbol in symbols:
            try:
                frame = ak.stock_news_em(symbol=symbol.split(".", 1)[0])
                rows.extend(normalize_eastmoney_news(frame, self.name, symbol))
            except Exception:
                continue
        return rows

    def fetch_industries(self) -> list[IndustrySnapshot]:
        return []


class AkShareTongHuaShunProvider:
    name = "tonghuashun"

    def fetch_spot_quotes(self) -> list[SpotQuote]:
        return []

    def fetch_stock_news(self, symbols: list[str]) -> list[StockNews]:
        return []

    def fetch_industries(self) -> list[IndustrySnapshot]:
        try:
            import akshare as ak

            frame = ak.stock_board_industry_name_ths()
        except Exception as exc:
            raise RecoverableProviderError(f"tonghuashun industry fetch failed: {type(exc).__name__}") from exc
        return normalize_ths_industries(frame, self.name)


def provider_by_name(name: str) -> PublicMarketProvider:
    normalized = name.strip().lower()
    if normalized == "fixture":
        return FixtureProvider()
    if normalized in {"eastmoney", "em"}:
        return AkShareEastMoneyProvider()
    if normalized in {"tonghuashun", "ths"}:
        return AkShareTongHuaShunProvider()
    raise ValueError("unsupported public market provider")


def normalize_eastmoney_spot(frame: pd.DataFrame, provider: str) -> list[SpotQuote]:
    required = ["代码", "名称", "最新价", "今开", "最高", "最低", "昨收", "成交量", "成交额"]
    _require_columns(frame, required)
    now = datetime.now(timezone.utc)
    rows = []
    for item in frame.to_dict("records"):
        symbol = normalize_symbol(str(item["代码"]))
        rows.append(
            make_spot_quote(
                provider,
                symbol,
                str(item["名称"]),
                now,
                item["今开"],
                item["最高"],
                item["最低"],
                item["最新价"],
                item.get("昨收"),
                int(_decimal(item["成交量"])),
                item.get("成交额"),
            )
        )
    return rows


def normalize_eastmoney_news(frame: pd.DataFrame, provider: str, symbol: str) -> list[StockNews]:
    if frame.empty:
        return []
    title_col = _first_existing(frame, ["新闻标题", "标题", "title"])
    url_col = _first_existing(frame, ["新闻链接", "链接", "url"])
    time_col = _first_existing(frame, ["发布时间", "时间", "datetime"])
    rows = []
    for item in frame.to_dict("records"):
        published = _parse_datetime(item.get(time_col)) if time_col else None
        rows.append(make_news(provider, symbol, str(item.get(title_col, "")), "", str(item.get(url_col, "")), published))
    return rows


def normalize_ths_industries(frame: pd.DataFrame, provider: str) -> list[IndustrySnapshot]:
    name_col = _first_existing(frame, ["板块", "板块名称", "name", "行业"])
    change_col = _first_existing(frame, ["涨跌幅", "涨跌幅/%", "change_pct"])
    leader_col = _first_existing(frame, ["领涨股", "leading_stock"])
    now = datetime.now(timezone.utc)
    return [make_industry(provider, str(row.get(name_col, "")), now, row.get(change_col), None, str(row.get(leader_col, "")) if leader_col else None) for row in frame.to_dict("records")]


def make_spot_quote(provider: str, symbol: str, name: str, market_time: datetime, open_price: Any, high_price: Any, low_price: Any, last_price: Any, previous_close: Any, volume: int, amount: Any) -> SpotQuote:
    values = {
        "provider": provider,
        "symbol": symbol,
        "market_time": market_time.isoformat(),
        "open": str(_decimal(open_price)),
        "high": str(_decimal(high_price)),
        "low": str(_decimal(low_price)),
        "last": str(_decimal(last_price)),
        "previous_close": None if previous_close in {None, ""} else str(_decimal(previous_close)),
        "volume": volume,
        "amount": None if amount in {None, ""} else str(_decimal(amount)),
    }
    if min(_decimal(open_price), _decimal(high_price), _decimal(low_price), _decimal(last_price)) <= 0:
        raise ValueError("price must be positive")
    if _decimal(high_price) < max(_decimal(open_price), _decimal(low_price), _decimal(last_price)):
        raise ValueError("invalid high price")
    if _decimal(low_price) > min(_decimal(open_price), _decimal(high_price), _decimal(last_price)):
        raise ValueError("invalid low price")
    return SpotQuote(provider, symbol, name, symbol.split(".", 1)[1] if "." in symbol else "", market_time, _decimal(open_price), _decimal(high_price), _decimal(low_price), _decimal(last_price), None if previous_close in {None, ""} else _decimal(previous_close), int(volume), None if amount in {None, ""} else _decimal(amount), checksum(values))


def make_industry(provider: str, name: str, market_time: datetime, change_pct: Any, turnover: Any, leading_stock: str | None) -> IndustrySnapshot:
    payload = {"provider": provider, "industry_name": name, "market_time": market_time.isoformat(), "change_pct": None if change_pct in {None, ""} else str(_decimal(change_pct)), "turnover": None if turnover in {None, ""} else str(_decimal(turnover)), "leading_stock": leading_stock or ""}
    return IndustrySnapshot(provider, name, market_time, None if change_pct in {None, ""} else _decimal(change_pct), None if turnover in {None, ""} else _decimal(turnover), leading_stock, checksum(payload))


def make_news(provider: str, symbol: str, title: str, summary: str, source_url: str, published_at: datetime | None) -> StockNews:
    url_hash = hashlib.sha256(source_url.encode("utf-8")).hexdigest()
    payload = {"provider": provider, "symbol": symbol, "title": title, "summary": summary, "source_url_hash": url_hash, "published_at": "" if published_at is None else published_at.isoformat()}
    return StockNews(provider, symbol, title, summary, source_url, url_hash, published_at, checksum(payload))


def normalize_symbol(code: str) -> str:
    raw = code.strip().upper()
    if raw.endswith((".SH", ".SZ", ".BJ")):
        return raw
    if raw.startswith("6"):
        return f"{raw}.SH"
    if raw.startswith(("0", "3")):
        return f"{raw}.SZ"
    if raw.startswith(("4", "8")):
        return f"{raw}.BJ"
    return raw


def checksum(payload: dict[str, Any]) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _decimal(value: Any) -> Decimal:
    try:
        result = Decimal(str(value).replace(",", "").strip())
    except (InvalidOperation, AttributeError) as exc:
        raise ValueError(f"invalid decimal value: {value}") from exc
    if result.is_nan():
        raise ValueError("decimal value must not be NaN")
    return result


def _require_columns(frame: pd.DataFrame, columns: list[str]) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"provider schema missing columns: {missing}")


def _first_existing(frame: pd.DataFrame, columns: list[str]) -> str | None:
    for column in columns:
        if column in frame.columns:
            return column
    return None


def _parse_datetime(value: Any) -> datetime | None:
    if value in {None, ""}:
        return None
    parsed = pd.to_datetime(value)
    if parsed.tzinfo is None:
        return parsed.to_pydatetime().replace(tzinfo=timezone.utc)
    return parsed.to_pydatetime().astimezone(timezone.utc)
