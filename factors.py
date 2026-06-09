"""
Phase 2 — Factor extraction from EDGAR CompanyFacts
====================================================

Turns one company's raw CompanyFacts JSON into a flat dict of fundamental
factors. Every factor can be None (missing data) — that is expected and is
tracked downstream as "coverage". Nothing here raises on missing data.

Design notes:
  - EDGAR tags vary by filer (e.g. revenue may be 'Revenues' or
    'RevenueFromContractWithCustomerExcludingAssessedTax'). We try a list of
    synonyms for each concept and take the first that yields data.
  - We only use ANNUAL (10-K, fp='FY') figures for trend/CAGR work, deduping
    restatements by fiscal-period-end date (last value wins).
  - All factors are "as filed" — we do not fetch price here. Price-dependent
    factors (P/S, market-cap band) are added in a separate enrichment step.
"""

from __future__ import annotations

# Candidate XBRL tags for each concept, in priority order.
# Companies tag the same economic concept differently; we try each in turn.
TAG_SYNONYMS = {
    "revenue": [
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "RevenuesNetOfInterestExpense",
        "SalesRevenueGoodsNet",
        "SalesRevenueServicesNet",
        "RegulatedAndUnregulatedOperatingRevenue",
    ],
    "gross_profit": ["GrossProfit"],
    "operating_income": ["OperatingIncomeLoss"],
    "net_income": ["NetIncomeLoss"],
    "stockholders_equity": [
        "StockholdersEquity",
        "StockholdersEquityIncludingPortionAttributableToNoncontrollingInterest",
    ],
    "total_debt": [  # we sum long-term + current portions if present
        "LongTermDebtNoncurrent",
        "LongTermDebt",
    ],
    "current_debt": ["LongTermDebtCurrent", "DebtCurrent"],
    "shares_outstanding": [
        "CommonStockSharesOutstanding",
        "WeightedAverageNumberOfDilutedSharesOutstanding",
        "WeightedAverageNumberOfSharesOutstandingBasic",
    ],
    "cash": [
        "CashAndCashEquivalentsAtCarryingValue",
        "CashCashEquivalentsRestrictedCashAndRestrictedCashEquivalents",
    ],
}


def _annual_series(facts: dict, tag: str,
                   taxonomy: str = "us-gaap", unit: str = "USD") -> dict:
    """
    Extract clean annual values for one tag: {period_end_date: value}, sorted.
    Annual = 10-K, fp='FY'. Duplicate end dates collapse (last write wins),
    which handles restatements. Returns {} if the tag/unit is absent.
    """
    try:
        rows = facts["facts"][taxonomy][tag]["units"][unit]
    except (KeyError, TypeError):
        return {}
    out: dict[str, float] = {}
    for r in rows:
        if r.get("form") != "10-K" or r.get("fp") != "FY":
            continue
        end = r.get("end")
        val = r.get("val")
        if end is None or val is None:
            continue
        out[end] = val
    return dict(sorted(out.items()))


def _series_for_concept(facts: dict, concept: str, unit: str = "USD") -> dict:
    """Try each synonym tag for a concept; return the first non-empty series."""
    for tag in TAG_SYNONYMS.get(concept, []):
        unit_try = "shares" if concept == "shares_outstanding" else unit
        s = _annual_series(facts, tag, unit=unit_try)
        if s:
            return s
    return {}


def _latest(series: dict):
    """Most recent value in an annual series, or None."""
    if not series:
        return None
    return list(series.values())[-1]


def _cagr(series: dict):
    """
    Compound annual growth rate across the full available annual series.
    Uses the ACTUAL calendar span between the first and last period-end
    dates (robust to gaps/missing years), not the count of data points.
    Needs >=2 positive points spanning >=1 year. Returns decimal or None.
    """
    items = list(series.items())
    if len(items) < 2:
        return None
    (d_first, first), (d_last, last) = items[0], items[-1]
    if first is None or last is None or first <= 0 or last <= 0:
        return None
    # years between the two period-end dates (ISO 'YYYY-MM-DD')
    try:
        y0 = int(d_first[:4]) + (int(d_first[5:7]) - 1) / 12
        y1 = int(d_last[:4]) + (int(d_last[5:7]) - 1) / 12
    except (ValueError, IndexError):
        return None
    span = y1 - y0
    if span < 0.5:  # need a meaningful span
        return None
    return (last / first) ** (1 / span) - 1


def _yoy(series: dict):
    """Most recent year-over-year growth (decimal) or None."""
    vals = list(series.values())
    if len(vals) < 2:
        return None
    prev, curr = vals[-2], vals[-1]
    if prev is None or curr is None or prev <= 0:
        return None
    return curr / prev - 1


def _ratio(num, den):
    """Safe division: None if either missing or denominator ~0."""
    if num is None or den is None or den == 0:
        return None
    return num / den


def _margin_trend(margin_series: dict):
    """
    Direction of a margin over time: latest minus earliest (decimal points).
    Positive = expanding. Needs >=2 points. None otherwise.
    """
    vals = [v for v in margin_series.values() if v is not None]
    if len(vals) < 2:
        return None
    return vals[-1] - vals[0]


def extract_factors(facts: dict | None) -> dict:
    """
    Main entry point. Given CompanyFacts JSON (or None), return a flat dict of
    fundamental factors. Any factor may be None. Never raises on missing data.
    """
    out: dict = {
        "revenue_cagr": None, "revenue_yoy": None, "revenue_years": 0,
        "gross_margin": None, "gross_margin_trend": None,
        "operating_margin": None, "operating_margin_trend": None,
        "roe": None,
        "roic": None,
        "debt_to_equity": None,
        "share_change": None,
        "net_income_latest": None,
        "revenue_latest": None,
        "shares_latest": None,
    }
    if not facts:
        return out

    rev = _series_for_concept(facts, "revenue")
    gp = _series_for_concept(facts, "gross_profit")
    oi = _series_for_concept(facts, "operating_income")
    ni = _series_for_concept(facts, "net_income")
    eq = _series_for_concept(facts, "stockholders_equity")
    ltd = _series_for_concept(facts, "total_debt")
    cd = _series_for_concept(facts, "current_debt")
    sh = _series_for_concept(facts, "shares_outstanding")
    cash = _series_for_concept(facts, "cash")

    # --- Growth ---
    out["revenue_cagr"] = _cagr(rev)
    out["revenue_yoy"] = _yoy(rev)
    out["revenue_years"] = len(rev)
    out["revenue_latest"] = _latest(rev)

    # --- Quality: margins + trend (align by shared period-end dates) ---
    def margin_series(numer: dict, denom: dict) -> dict:
        m = {}
        for d in denom:
            if d in numer and denom[d] not in (None, 0):
                m[d] = numer[d] / denom[d]
        return dict(sorted(m.items()))

    gm = margin_series(gp, rev)
    om = margin_series(oi, rev)
    out["gross_margin"] = _latest(gm)
    out["gross_margin_trend"] = _margin_trend(gm)
    out["operating_margin"] = _latest(om)
    out["operating_margin_trend"] = _margin_trend(om)

    # --- Quality: ROE = latest net income / latest equity ---
    out["net_income_latest"] = _latest(ni)
    out["roe"] = _ratio(_latest(ni), _latest(eq))

    # --- Quality: ROIC = NOPAT / Invested Capital ---
    # NOPAT ~= operating income * (1 - statutory tax 21%); a standard
    # approximation when we don't compute an effective rate per company.
    # Invested capital ~= total debt + equity - cash (common definition).
    oi_latest = _latest(oi)
    eq_latest = _latest(eq)
    cash_latest = _latest(cash)
    debt_l = _latest(ltd)
    cdebt_l = _latest(cd)
    if oi_latest is not None and eq_latest is not None:
        nopat = oi_latest * (1 - 0.21)
        total_debt = (debt_l or 0) + (cdebt_l or 0)
        invested_capital = total_debt + eq_latest - (cash_latest or 0)
        out["roic"] = _ratio(nopat, invested_capital)

    # --- Balance sheet: debt/equity (sum LT + current debt if both present) ---
    debt_latest = debt_l
    cdebt_latest = cdebt_l
    if debt_latest is not None or cdebt_latest is not None:
        total_debt = (debt_latest or 0) + (cdebt_latest or 0)
        out["debt_to_equity"] = _ratio(total_debt, _latest(eq))

    # --- Balance sheet: share count change (dilution) over full series ---
    # Positive = dilution (more shares), negative = buybacks.
    out["share_change"] = _cagr(sh)  # CAGR of share count
    out["shares_latest"] = _latest(sh)  # for market-cap computation

    return out
