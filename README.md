# Mid-Cap Compounder Screener

Finds S&P MidCap 400 companies with the financial characteristics of potential
large-caps, using **free SEC EDGAR data** (no API key, no rate caps).

## Status: Phase 1 — universe foundation (working)

- Loads S&P 400 constituents (Wikipedia, with local-CSV fallback)
- Pulls EDGAR ticker→CIK map
- Attaches CIKs; reports unmatched tickers
- Polite EDGAR client (required User-Agent, ≤10 req/sec)
- Verified CompanyFacts JSON parser (clean annual series; the base for scoring)

## Setup

1. `pip install -r requirements.txt`
2. Set a real contact email (EDGAR requires it):
   `export SEC_USER_AGENT="MidCapScreener you@yourdomain.com"`
3. `python screener/universe.py` → writes `universe.csv`

## GitHub Actions (automated monthly run)

Add repo secret `SEC_USER_AGENT` (Settings → Secrets and variables → Actions),
then the workflow in `.github/workflows/screener.yml` runs on the 1st monthly
and commits results. Repo must allow Actions to push (Settings → Actions →
Workflow permissions → Read and write).

## Roadmap

- **Phase 2** — score reliable EDGAR factors (growth, margins+trend, ROE,
  leverage, dilution, valuation); output ranked candidates + a coverage column.
- **Phase 3** — review data coverage, decide if any factor needs a better source.
- **Phase 4** — qualitative shortlist checklist for the top names.
- **Phase 5** — demo paper portfolio + monthly price tracking + a viewer site.

Every output is a **research queue, not a buy list**. Not investment advice.
