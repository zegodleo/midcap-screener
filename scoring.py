"""
Phase 2 — Scoring engine
========================

Takes the per-company factor dicts (from factors.py) plus optional price-based
factors, normalizes each factor to 0-100 across the universe (percentile rank),
applies bucket weights, and produces a final score + a COVERAGE figure.

Why percentile-rank normalization:
  - Robust to outliers and to factors on wildly different scales (a margin is
    0-1, market cap is billions). Each company is scored by where it ranks vs
    peers, 0 (worst) to 100 (best).
  - For factors where "lower is better" (debt, dilution, valuation multiple),
    we invert so high score always = more attractive.

Coverage:
  - Each factor a company actually has contributes to its coverage count.
  - The final score is the weighted average over the buckets it HAS data for,
    and we report coverage so thin-data companies can be flagged/excluded.
"""

from __future__ import annotations
import pandas as pd

# Factor -> (bucket, higher_is_better)
# Buckets and weights:
BUCKET_WEIGHTS = {
    "growth": 0.35,
    "quality": 0.25,
    "balance": 0.15,
    "valuation": 0.10,
    "size": 0.10,
    "liquidity": 0.05,
}

# Each factor maps to a bucket and a direction.
# higher_is_better=False means we invert the percentile (low raw = high score).
FACTOR_SPEC = {
    # growth
    "revenue_cagr":           ("growth", True),
    "revenue_yoy":            ("growth", True),
    # quality
    "gross_margin":           ("quality", True),
    "gross_margin_trend":     ("quality", True),
    "operating_margin":       ("quality", True),
    "operating_margin_trend": ("quality", True),
    "roe":                    ("quality", True),
    # balance sheet
    "debt_to_equity":         ("balance", False),   # lower is better
    "share_change":           ("balance", False),   # lower (less dilution) better
    # valuation (price-dependent; added by enrichment)
    "price_to_sales":         ("valuation", False), # lower is better
    # size (price-dependent)
    "market_cap":             ("size", True),       # bigger within the band
    # liquidity (price-dependent)
    "dollar_volume":          ("liquidity", True),
}


def _percentile_scores(s: pd.Series, higher_is_better: bool) -> pd.Series:
    """Rank a factor column to 0-100 percentile. NaNs stay NaN (no data)."""
    pct = s.rank(pct=True) * 100.0
    if not higher_is_better:
        pct = 100.0 - pct
    return pct


def score_universe(rows: list[dict]) -> pd.DataFrame:
    """
    rows: list of dicts, each = {ticker, company, sector, ...all factors...}.
    Returns a DataFrame with per-factor scores, per-bucket scores, a final
    'score' (0-100), and 'coverage' (# of factors with data, out of total).
    """
    df = pd.DataFrame(rows)

    # 1) Normalize each available factor to a 0-100 score column.
    score_cols_by_bucket: dict[str, list[str]] = {b: [] for b in BUCKET_WEIGHTS}
    for factor, (bucket, hib) in FACTOR_SPEC.items():
        if factor not in df.columns:
            continue
        col = f"score__{factor}"
        df[col] = _percentile_scores(pd.to_numeric(df[factor], errors="coerce"), hib)
        score_cols_by_bucket[bucket].append(col)

    # 2) Bucket score = mean of available factor-scores in that bucket.
    bucket_score_cols = []
    for bucket, cols in score_cols_by_bucket.items():
        bcol = f"bucket__{bucket}"
        if cols:
            df[bcol] = df[cols].mean(axis=1, skipna=True)
        else:
            df[bcol] = pd.NA
        bucket_score_cols.append((bucket, bcol))

    # 3) Final score = weighted avg over buckets that HAVE a score, with the
    #    weights renormalized to the available buckets. BUT a company must meet
    #    a minimum data-coverage bar to receive a ranked score — otherwise a
    #    name with only one bucket could top the table on thin data (exactly
    #    the failure mode coverage exists to prevent). Below the bar, the score
    #    is still computed but the company is marked low-confidence and sorted
    #    below all sufficiently-covered names.
    MIN_WEIGHT_COVERAGE = 0.60  # must have buckets totaling >=60% of weight

    def final_row(r):
        num, wsum = 0.0, 0.0
        for bucket, bcol in bucket_score_cols:
            v = r[bcol]
            if pd.notna(v):
                w = BUCKET_WEIGHTS[bucket]
                num += v * w
                wsum += w
        return num / wsum if wsum > 0 else pd.NA
    df["score"] = df.apply(final_row, axis=1)

    # 4) Coverage: how many of the defined factors this company actually had.
    factor_cols = [f for f in FACTOR_SPEC if f in df.columns]
    df["coverage"] = df[factor_cols].notna().sum(axis=1)
    df["coverage_max"] = len(factor_cols)
    # weight-coverage: fraction of total bucket weight that was available
    def weight_cov(r):
        return sum(BUCKET_WEIGHTS[b] for b, bcol in bucket_score_cols
                   if pd.notna(r[bcol]))
    df["weight_coverage"] = df.apply(weight_cov, axis=1)

    # Confidence flag: did the company clear the coverage bar?
    df["sufficient_data"] = df["weight_coverage"] >= MIN_WEIGHT_COVERAGE

    # Sort: well-covered names first (by score), then low-confidence names
    # (by score) below them — so thin data can never top the ranking.
    df = df.sort_values(
        ["sufficient_data", "score"],
        ascending=[False, False],
        na_position="last",
    )
    df = df.reset_index(drop=True)
    df.insert(0, "rank", df.index + 1)
    return df
