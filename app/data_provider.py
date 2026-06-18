from __future__ import annotations

from datetime import date, timedelta

import pandas as pd


class MarketDataError(RuntimeError):
    pass


def normalize_symbol(symbol: str) -> str:
    value = symbol.strip().lower().replace("sh", "").replace("sz", "")
    if not (value.isdigit() and len(value) == 6):
        raise ValueError(f"Invalid A-share symbol: {symbol}")
    return value


def fetch_daily_history(symbol: str, lookback_days: int = 240) -> pd.DataFrame:
    """Fetch A-share daily bars through AKShare.

    Uses unadjusted prices by default. For research, explicitly choose a consistent
    adjustment convention and avoid mixing adjusted and unadjusted series.
    """
    try:
        import akshare as ak

        end = date.today()
        start = end - timedelta(days=lookback_days * 2)
        df = ak.stock_zh_a_hist(
            symbol=normalize_symbol(symbol),
            period="daily",
            start_date=start.strftime("%Y%m%d"),
            end_date=end.strftime("%Y%m%d"),
            adjust="qfq",
        )
    except Exception as exc:  # network/provider failures must not become trade signals
        raise MarketDataError(f"Failed to fetch {symbol}: {exc}") from exc

    if df is None or df.empty:
        raise MarketDataError(f"No market data for {symbol}")

    rename_map = {
        "日期": "date",
        "开盘": "open",
        "收盘": "close",
        "最高": "high",
        "最低": "low",
        "成交量": "volume",
        "成交额": "amount",
        "换手率": "turnover",
    }
    df = df.rename(columns=rename_map)
    required = ["date", "open", "high", "low", "close", "volume"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        raise MarketDataError(f"Missing columns for {symbol}: {missing}")

    df = df[required].copy()
    df["date"] = pd.to_datetime(df["date"])
    for col in ["open", "high", "low", "close", "volume"]:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    return df.dropna().sort_values("date").tail(lookback_days).reset_index(drop=True)
