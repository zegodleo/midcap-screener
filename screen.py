"""
Phase 2 — Main orchestration (run this)
=======================================

Pipeline:
  1. Build the universe (Phase 1): S&P 400 + CIKs.
  2. For each company, fetch EDGAR CompanyFacts and extract fundamental factors.
  3. (Optional) enrich with price via yfinance for valuation/size/liquidity.
  4. Score & rank, tracking coverage.
  5. Write outputs: ranked CSV (all) + a top-N CSV.

Runs unattended in GitHub Actions. EDGAR is the backbone (no limits); price is
best-effort. Designed to finish a 400-name universe in a few minutes.
"""

from __future__ import annotations
import os
import logging

import pandas as pd

from universe import SecClient, build_universe, fetch_company_facts
from factors import extract_factors
from price import enrich_with_price
from scoring import score_universe

log = logging.getLogger("screen")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)



def run() -> pd.DataFrame:
    top_n = int(os.environ.get("TOP_N", "50"))
    skip_price = os.environ.get("SKIP_PRICE", "").lower() in ("1", "true", "yes")
    client = SecClient()

    # --- Phase 1: universe ---
    uni = build_universe(client)
    universe = uni.df[uni.df["cik"].notna()].copy()
    log.info("Scoring %d companies with CIKs.", len(universe))

    # --- Phase 2a: per-company EDGAR factors ---
    rows: list[dict] = []
    total = len(universe)
    for i, rec in enumerate(universe.itertuples(index=False), start=1):
        if i % 50 == 0 or i == 1:
            log.info("Fetching EDGAR facts %d/%d ...", i, total)
        facts = fetch_company_facts(client, rec.cik)
        f = extract_factors(facts)
        f.update({
            "ticker": rec.ticker,
            "company": rec.company,
            "sector": rec.sector,
            "cik": rec.cik,
        })
        rows.append(f)

    # --- Phase 2b: optional price enrichment ---
    if skip_price:
        log.info("SKIP_PRICE set; running on fundamentals only.")
        for r in rows:
            r.setdefault("market_cap", None)
            r.setdefault("price_to_sales", None)
            r.setdefault("dollar_volume", None)
    else:
        log.info("Enriching with price (yfinance, best-effort)...")
        rows = enrich_with_price(rows)

    # --- Phase 2c: score & rank ---
    df = score_universe(rows)

    # Tidy column order for the human-readable output
    front = ["rank", "ticker", "company", "sector", "score",
             "sufficient_data", "coverage", "coverage_max", "weight_coverage"]
    factor_cols = ["revenue_cagr", "revenue_yoy", "revenue_years",
                   "gross_margin", "gross_margin_trend",
                   "operating_margin", "operating_margin_trend", "roe",
                   "debt_to_equity", "share_change",
                   "price_to_sales", "market_cap", "dollar_volume"]
    front += [c for c in factor_cols if c in df.columns]
    ordered = front + [c for c in df.columns if c not in front]
    df = df[ordered]

    # --- Outputs ---
    out_dir = os.environ.get("OUTPUT_DIR", "output")
    os.makedirs(out_dir, exist_ok=True)
    all_path = os.path.join(out_dir, "scored_all.csv")
    top_path = os.path.join(out_dir, f"top{top_n}.csv")
    df.to_csv(all_path, index=False)
    # Top-N drawn only from sufficiently-covered names
    top = df[df["sufficient_data"]].head(top_n)
    top.to_csv(top_path, index=False)

    log.info("=" * 60)
    log.info("Phase 2 complete.")
    log.info("  Companies scored        : %d", len(df))
    log.info("  Sufficient data (ranked): %d", int(df["sufficient_data"].sum()))
    log.info("  All results  -> %s", all_path)
    log.info("  Top %-2d       -> %s", top_n, top_path)
    log.info("=" * 60)
    if len(top):
        log.info("Top 10 preview:")
        for rec in top.head(10).itertuples(index=False):
            cagr = "" if pd.isna(rec.revenue_cagr) else f"{rec.revenue_cagr*100:5.1f}%"
            log.info("  #%-2d %-6s score=%5.1f  rev_cagr=%6s  cov=%d/%d",
                     rec.rank, rec.ticker, rec.score, cagr,
                     rec.coverage, rec.coverage_max)
    return df


if __name__ == "__main__":
    run()
