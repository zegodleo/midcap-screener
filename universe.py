"""
Phase 1 — Universe loader + EDGAR foundation
=============================================

What this module does (and ONLY this — later phases build on it):

  1. Loads the S&P MidCap 400 constituent list (the starting universe).
        - Primary source: Wikipedia's machine-readable constituents table.
        - Fallback: a local CSV you can drop in (sp400.csv) to pin the list.
  2. Pulls EDGAR's master ticker -> CIK map (one request, no key, no limit).
  3. Attaches each company's 10-digit zero-padded CIK (the key EDGAR needs).
  4. Provides a POLITE, rate-limited EDGAR fetcher (<=10 req/sec, required
     User-Agent) that the later phases will call to pull CompanyFacts.

Design constraints baked in:
  - Runs unattended in GitHub Actions: no interactive prompts, clear logging,
    fails loudly with actionable messages.
  - EDGAR courtesy rules: mandatory User-Agent, <=10 requests/second.
  - No paid APIs, no monthly caps anywhere in the data path.

IMPORTANT: set a real contact email in the User-Agent (EDGAR requires it and
will return 403 for a generic/empty agent). Use an env var so you don't commit
your email: set SEC_USER_AGENT in your GitHub Actions secrets.
"""

from __future__ import annotations

import io
import os
import time
import logging
from dataclasses import dataclass

import requests
import pandas as pd

# ----------------------------------------------------------------------
# Configuration
# ----------------------------------------------------------------------

# EDGAR requires a descriptive User-Agent with contact info, or it 403s.
# Set SEC_USER_AGENT in your environment / GitHub Actions secrets, e.g.:
#   "MidCapScreener your.name@example.com"
SEC_USER_AGENT = os.environ.get(
    "SEC_USER_AGENT",
    "MidCapScreener research example@example.com",  # <-- replace before real use
)

EDGAR_TICKER_URL = "https://www.sec.gov/files/company_tickers.json"
EDGAR_FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json"

WIKI_SP400_URL = "https://en.wikipedia.org/wiki/List_of_S%26P_400_companies"
LOCAL_SP400_CSV = "sp400.csv"  # optional fallback / pinned list

# EDGAR courtesy limit is 10 req/sec. We stay well under it.
EDGAR_MIN_INTERVAL = 0.15  # seconds between requests (~6.6 req/sec)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("universe")


# ----------------------------------------------------------------------
# A single shared session + rate limiter for all EDGAR/SEC traffic
# ----------------------------------------------------------------------

class SecClient:
    """Polite SEC/EDGAR HTTP client: required User-Agent + rate limiting."""

    def __init__(self, user_agent: str = SEC_USER_AGENT,
                 min_interval: float = EDGAR_MIN_INTERVAL):
        if "example.com" in user_agent:
            log.warning(
                "SEC_USER_AGENT still contains a placeholder email. "
                "EDGAR may reject requests. Set a real contact email."
            )
        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent})
        self.min_interval = min_interval
        self._last_request = 0.0

    def get(self, url: str, **kwargs) -> requests.Response:
        # enforce minimum spacing between requests
        elapsed = time.monotonic() - self._last_request
        if elapsed < self.min_interval:
            time.sleep(self.min_interval - elapsed)
        resp = self.session.get(url, timeout=30, **kwargs)
        self._last_request = time.monotonic()
        resp.raise_for_status()
        return resp


# ----------------------------------------------------------------------
# Step 1 — load the S&P 400 constituent list
# ----------------------------------------------------------------------

def load_sp400(client: SecClient) -> pd.DataFrame:
    """
    Returns a DataFrame with at least: ticker, company, sector.
    Tries Wikipedia first; falls back to a local CSV if present.
    """
    # Wikipedia uses a non-SEC host, so use a plain requests call with a
    # browser-ish UA (Wikipedia 403s on empty agents too).
    try:
        log.info("Fetching S&P 400 constituents from Wikipedia...")
        headers = {"User-Agent": "Mozilla/5.0 (MidCapScreener; research)"}
        html = requests.get(WIKI_SP400_URL, headers=headers, timeout=30).text
        tables = pd.read_html(io.StringIO(html))
        # The constituents table is the one with a 'Symbol' column.
        df = next(t for t in tables if "Symbol" in t.columns)
        df = df.rename(columns={
            "Symbol": "ticker",
            "Security": "company",
            "GICS Sector": "sector",
            "GICS Sub-Industry": "sub_industry",
        })
        df = df[["ticker", "company", "sector", "sub_industry"]].copy()
        df["ticker"] = df["ticker"].str.upper().str.strip()
        log.info("Loaded %d constituents from Wikipedia.", len(df))
        return df.reset_index(drop=True)
    except Exception as e:  # noqa: BLE001 — we want any failure to fall back
        log.warning("Wikipedia load failed (%s). Trying local CSV...", e)

    if os.path.exists(LOCAL_SP400_CSV):
        df = pd.read_csv(LOCAL_SP400_CSV)
        df["ticker"] = df["ticker"].str.upper().str.strip()
        log.info("Loaded %d constituents from %s.", len(df), LOCAL_SP400_CSV)
        return df

    raise RuntimeError(
        "Could not load S&P 400 list from Wikipedia or local CSV. "
        f"Provide a {LOCAL_SP400_CSV} with a 'ticker' column."
    )


# ----------------------------------------------------------------------
# Step 2 — EDGAR ticker -> CIK map
# ----------------------------------------------------------------------

def load_cik_map(client: SecClient) -> dict[str, str]:
    """
    Returns {TICKER: '0000320193'} with 10-digit zero-padded CIKs.
    One request to EDGAR's master ticker file.
    """
    log.info("Fetching EDGAR ticker->CIK map...")
    data = client.get(EDGAR_TICKER_URL).json()
    # The file is {"0": {"cik_str": 320193, "ticker": "AAPL", "title": ...}, ...}
    cik_map: dict[str, str] = {}
    for row in data.values():
        ticker = str(row["ticker"]).upper().strip()
        cik = str(row["cik_str"]).zfill(10)
        cik_map[ticker] = cik
    log.info("Loaded %d ticker->CIK mappings.", len(cik_map))
    return cik_map


# ----------------------------------------------------------------------
# Step 3 — build the universe (constituents + CIKs)
# ----------------------------------------------------------------------

@dataclass
class Universe:
    df: pd.DataFrame          # the joined table
    matched: int              # how many got a CIK
    unmatched: list[str]      # tickers with no CIK match


def build_universe(client: SecClient) -> Universe:
    """Join the S&P 400 list with EDGAR CIKs. This is the Phase 1 output."""
    sp400 = load_sp400(client)
    cik_map = load_cik_map(client)

    def resolve_cik(ticker: str) -> str | None:
        t = ticker.upper().strip()
        if t in cik_map:
            return cik_map[t]
        # Share-class fallback: index lists use dots (MOG.A), EDGAR uses
        # hyphens (MOG-A). Try the hyphenated form before giving up.
        alt = t.replace(".", "-")
        return cik_map.get(alt)

    sp400["cik"] = sp400["ticker"].apply(resolve_cik)
    matched = sp400["cik"].notna().sum()
    unmatched = sp400.loc[sp400["cik"].isna(), "ticker"].tolist()

    if unmatched:
        log.warning(
            "%d tickers had no EDGAR CIK match (often share-class suffixes "
            "like BRK.B vs EDGAR's BRK-B, or recent index changes): %s",
            len(unmatched), ", ".join(unmatched[:15]) + ("..." if len(unmatched) > 15 else ""),
        )

    return Universe(df=sp400, matched=int(matched), unmatched=unmatched)


# ----------------------------------------------------------------------
# Step 4 — EDGAR CompanyFacts fetcher (foundation for later phases)
# ----------------------------------------------------------------------

def fetch_company_facts(client: SecClient, cik: str) -> dict | None:
    """
    Fetch one company's full XBRL fact set. ONE request returns the entire
    financial history. Returns parsed JSON, or None if EDGAR has no facts
    for that CIK (happens for some foreign filers / non-XBRL entities).
    """
    url = EDGAR_FACTS_URL.format(cik=cik)
    try:
        return client.get(url).json()
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 404:
            return None  # no XBRL facts for this filer
        raise


# ----------------------------------------------------------------------
# Run as a script: produce the Phase 1 universe file
# ----------------------------------------------------------------------

def main() -> None:
    client = SecClient()
    uni = build_universe(client)

    out_dir = os.environ.get("OUTPUT_DIR", ".")
    os.makedirs(out_dir, exist_ok=True)
    out_path = os.path.join(out_dir, "universe.csv")
    uni.df.to_csv(out_path, index=False)

    log.info("=" * 60)
    log.info("Phase 1 complete.")
    log.info("  Constituents loaded : %d", len(uni.df))
    log.info("  Matched to a CIK    : %d", uni.matched)
    log.info("  Unmatched           : %d", len(uni.unmatched))
    log.info("  Universe written to : %s", out_path)
    log.info("=" * 60)


if __name__ == "__main__":
    main()
