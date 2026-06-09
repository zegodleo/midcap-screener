"""
Phase 2 — Price enrichment (optional, defensive)
=================================================

Adds price-dependent factors: market_cap, price_to_sales, dollar_volume.

CRITICAL DESIGN RULE: this is the ONE flaky data source in the stack.
yfinance is an unofficial Yahoo scraper that can break or get rate-limited
(especially from cloud IPs like GitHub Actions). So:
  - It NEVER raises into the main pipeline. Any failure -> that company's
    price factors are None, which the coverage system handles honestly.
  - The EDGAR fundamental score (the other 90%) is unaffected by price gaps.

If yfinance is unavailable entirely, the whole enrichment is skipped and the
screen runs on fundamentals alone.
"""

from __future__ import annotations
import logging
import time

log = logging.getLogger("price")

try:
    import yfinance as yf
    _HAS_YF = True
except ImportError:
    _HAS_YF = False
    log.warning("yfinance not installed; price factors will be skipped.")


def enrich_with_price(rows: list[dict], pause: float = 0.3) -> list[dict]:
    """
    For each row (must have 'ticker' and ideally 'revenue_latest'), attach:
      market_cap, price_to_sales, dollar_volume.
    Missing/failed lookups leave these as None. Mutates and returns rows.
    """
    for r in rows:
        r.setdefault("market_cap", None)
        r.setdefault("price_to_sales", None)
        r.setdefault("dollar_volume", None)

    if not _HAS_YF:
        return rows

    for r in rows:
        ticker = r.get("ticker")
        if not ticker:
            continue
        try:
            # EDGAR uses MOG-A; yfinance wants MOG-A too (it accepts hyphens),
            # but some feeds prefer dots. Try as-is first.
            t = yf.Ticker(ticker)
            fi = getattr(t, "fast_info", None) or {}

            price = fi.get("last_price") if hasattr(fi, "get") else None
            mcap = fi.get("market_cap") if hasattr(fi, "get") else None
            vol = fi.get("last_volume") if hasattr(fi, "get") else None

            if mcap:
                r["market_cap"] = float(mcap)
            if price and vol:
                r["dollar_volume"] = float(price) * float(vol)

            # price/sales = market cap / latest annual revenue (from EDGAR)
            rev = r.get("revenue_latest")
            if mcap and rev and rev > 0:
                r["price_to_sales"] = float(mcap) / float(rev)

        except Exception as e:  # noqa: BLE001 - never let price kill the run
            log.debug("price lookup failed for %s: %s", ticker, e)
        finally:
            time.sleep(pause)  # be gentle; reduces Yahoo throttling

    got = sum(1 for r in rows if r.get("market_cap") is not None)
    log.info("Price enrichment: %d/%d tickers got market cap.", got, len(rows))
    return rows
