"""
Phase 2 — Price enrichment via Stooq (free, no blocking, no limits)
===================================================================

Replaces yfinance, which Yahoo blocks from cloud IPs (GitHub Actions). Stooq
serves a simple CSV quote endpoint that does not block datacenter traffic and
has no rate caps.

Stooq gives price + volume (not market cap directly), so we COMPUTE:
  market_cap   = close_price * shares_outstanding   (shares from EDGAR)
  price_to_sales = market_cap / latest_annual_revenue (revenue from EDGAR)
  dollar_volume  = close_price * daily_volume

This is more robust than yfinance: two solid sources (Stooq price + EDGAR
fundamentals) instead of one flaky scraper. Still fully defensive — any failure
leaves that company's price factors as None, handled by the coverage system.
"""

from __future__ import annotations
import csv
import io
import logging
import time

import requests

log = logging.getLogger("price")

STOOQ_URL = "https://stooq.com/q/l/?s={sym}&f=sd2t2ohlcv&h&e=csv"


def _to_stooq_symbol(ticker: str) -> str:
    """AAPL -> AAPL.US ; MOG.A -> MOG-A.US (Stooq uses dashes + .US suffix)."""
    return ticker.replace(".", "-").upper() + ".US"


def _fetch_quote(session: requests.Session, ticker: str) -> tuple | None:
    """Return (close, volume) for a ticker, or None if unavailable."""
    url = STOOQ_URL.format(sym=_to_stooq_symbol(ticker))
    try:
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        if not rows:
            return None
        row = rows[0]
        close_raw = row.get("Close", "")
        vol_raw = row.get("Volume", "")
        if close_raw in ("", "N/D", None):
            return None
        close = float(close_raw)
        vol = float(vol_raw) if vol_raw not in ("", "N/D", None) else None
        return close, vol
    except Exception as e:  # noqa: BLE001 - never let price kill the run
        log.debug("Stooq lookup failed for %s: %s", ticker, e)
        return None


def enrich_with_price(rows: list[dict], pause: float = 0.05) -> list[dict]:
    """
    Attach market_cap, price_to_sales, dollar_volume to each row.
    Needs 'ticker', and from EDGAR: 'shares_latest' and 'revenue_latest'.
    Missing/failed lookups leave price factors as None.
    """
    for r in rows:
        r.setdefault("market_cap", None)
        r.setdefault("price_to_sales", None)
        r.setdefault("dollar_volume", None)

    session = requests.Session()
    session.headers.update({"User-Agent": "MidCapScreener research"})

    got = 0
    for r in rows:
        ticker = r.get("ticker")
        if not ticker:
            continue
        quote = _fetch_quote(session, ticker)
        time.sleep(pause)
        if quote is None:
            continue
        close, vol = quote
        shares = r.get("shares_latest")
        rev = r.get("revenue_latest")

        if shares and shares > 0:
            mcap = close * shares
            r["market_cap"] = mcap
            got += 1
            if rev and rev > 0:
                r["price_to_sales"] = mcap / rev
        if vol:
            r["dollar_volume"] = close * vol

    log.info("Price enrichment (Stooq): %d/%d tickers got market cap.",
             got, len(rows))
    return rows
