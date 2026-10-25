"""Compact market-data tool for the Quant agent (AutoHedge ships none attached)."""
import json
import math


def market_snapshot(ticker: str) -> str:
    """
    Get a compact quantitative snapshot for a Yahoo Finance ticker.

    Args:
        ticker (str): Yahoo Finance symbol, e.g. 'CL=F' (WTI crude futures), 'BZ=F' (Brent),
            'AAPL', 'SPY', 'BTC-USD'.

    Returns:
        str: JSON with last price, 1M/3M/6M/12M returns, 50/200-day moving averages,
        20/60-day annualized realized volatility, 52-week high/low, average volume and last 5 closes.
    """
    try:
        import yfinance as yf

        hist = yf.Ticker(ticker.strip().upper()).history(period="1y", interval="1d")
        close = hist["Close"].dropna()
        if len(close) < 30:
            return json.dumps({"error": f"not enough data for {ticker}"})

        def ret(days):
            return round(float(close.iloc[-1] / close.iloc[-1 - days] - 1) * 100, 2) if len(close) > days else None

        def vol(days):
            r = close.pct_change().dropna().tail(days)
            return round(float(r.std()) * math.sqrt(252) * 100, 2) if len(r) > 5 else None

        def ma(days):
            return round(float(close.tail(days).mean()), 2) if len(close) >= days else None

        return json.dumps({
            "symbol": ticker.upper(),
            "as_of": str(close.index[-1].date()),
            "last": round(float(close.iloc[-1]), 2),
            "return_pct": {"1M": ret(21), "3M": ret(63), "6M": ret(126), "12M": ret(len(close) - 1)},
            "ma50": ma(50), "ma200": ma(200),
            "realized_vol_pct": {"20d": vol(20), "60d": vol(60)},
            "high_52w": round(float(close.max()), 2), "low_52w": round(float(close.min()), 2),
            "avg_volume_20d": int(hist["Volume"].tail(20).mean()),
            "last_5_closes": [round(float(x), 2) for x in close.tail(5)],
        })
    except Exception as e:
        return json.dumps({"error": str(e)[:300]})
