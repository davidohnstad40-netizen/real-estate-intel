# Real Estate Seller Intelligence Platform

Draw a region on a map → discover every property inside it → score each one by motivated-seller probability → get AI-generated door-knock scripts and offer models → track your outreach.

**Built for:** Off-market real estate acquisition. Single-user, local-first, no subscription required.

---

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp .env.example .env
# Edit .env — add your ANTHROPIC_API_KEY for AI features

# 3. Load seed data (52 Lakes of Radisson homes)
python -m ingestion.load_sample

# 4. Run the app
python -m streamlit run app/main.py --server.port 8504
# Open http://localhost:8504
```

---

## What It Does

### 🗺️ Map Tab
- All properties plotted with color-coded knock priority (T1=red, T2=orange, T3=green)
- **Draw a polygon** → instantly filters to properties inside your region
- Click any marker → loads Property Detail

### 📋 Ranked List
- Sorted by 0–100 motivation score
- Filter by buy box (beds, price, equity %, tier)
- Click a row → loads Property Detail

### 📍 Property Detail
- Motivation score breakdown (which signals contributed how many points)
- Estimated equity & monthly PITI
- **🚪 Door-knock script** — AI-generated opener + follow-up pivot + letter
- **🤖 Seller thesis** — why this person is likely to sell
- **💰 Offer model** — retail / quick-sale / opening / walk-away prices
- Contact info (phone, email, DOB) after skip trace
- Score trend chart (updated daily)
- Human feedback logging

### 🤖 AI Analysis
- Batch generate theses for all T1/T2 in one click
- Batch generate door-knock scripts for all T1/T2
- Region intelligence summary (Claude Sonnet)

### 🚪 Outreach Page (sidebar)
- Today's knock list — T1/T2 sorted by proximity (nearest-neighbor route)
- Google Maps link for the full drive route
- Log contact attempts with outcome and follow-up date
- Follow-up queue (overdue highlighted in red)

### 🔧 Data Tab
- DB stats and re-score all properties
- **Skip trace** — download CSV for batchskiptracing.com ($0.18/record), import results
- Anoka County full parcel download
- Feedback history

---

## Motivation Score (0–100)

| Signal | Points |
|---|---|
| Post-purchase divorce (confirmed) | +40 |
| Investor LLC (no homestead) | +30 |
| No homestead — absentee/moved | +20 |
| Negative equity (underwater) | +20 |
| Owner age 70+ | +15 |
| Peak buyer 2020–22 (high-rate carry) | +12 |
| Thin equity (<10%) | +10 |
| Trust-owned (estate vehicle) | +8 |
| Long hold 15+ years (equity-rich) | +8 |
| Long hold 12–14 years | +5 |
| Possible divorce (unverified) | +20 |
| Prior divorce (pre-dates property) | +5 |
| Civil litigation | +4 |

**Knock Tiers:**
- **T1** (score ≥ 40): Verified strong signal — knock first
- **T2** (score ≥ 20): Moderate signal — knock next
- **T3** (score ≥ 5): No strong signal — cold knock
- **SKIP**: Active listing or just purchased

---

## Skip Tracing (Contact Info)

```bash
# Export all owners to BatchSkipTracing upload format
python -m agents.skip_trace export
# → data/skip_trace_upload.csv

# Upload at https://www.batchskiptracing.com
# Cost: ~$0.18/record. 52 properties = ~$9.36 total.
# Download their results CSV, then import:
python -m agents.skip_trace import data/batchskiptracing_results.csv

# Free alternative (FastPeopleSearch — lower quality, ~60% hit rate)
# Stop Streamlit first, then:
python -m agents.skip_trace scrape
```

---

## Scaling Beyond the 52 Seed Properties

```bash
# Download all Blaine MN parcels from MN Geospatial Commons (~250 MB, one-time)
# Stop Streamlit first, then:
python -m ingestion.anoka_county Blaine
# Loads ~8,000 Blaine parcels into data/parcels.duckdb
# Runs Tier-1 cheap scoring on all of them
```

Change `Blaine` to any MN city. The same shapefile covers the entire Twin Cities metro.

---

## Daily Automation

Runs automatically at 7 AM via Windows Task Scheduler (`REI-DailySnapshot`).

Manual run:
```bash
python run_daily.py
```

Output: `data/daily_summary_YYYY-MM-DD.txt` with tier counts, upgrades, and top properties.

---

## AI Features (requires ANTHROPIC_API_KEY)

- **Door-knock script**: personalized opener, follow-up pivot, leave-behind letter
- **Seller thesis**: 2-3 sentence narrative on why this owner may sell
- **Offer model**: retail / quick-sale / opening / walk-away prices with rationale
- **Region summary**: Claude Sonnet market intelligence summary

Models used: `claude-haiku-4-5` (per-property, fast/cheap), `claude-sonnet-4-5` (region summaries)

---

## Architecture

```
real-estate-intel/
├── app/
│   ├── main.py              # Streamlit UI (5 tabs + outreach page)
│   └── pages/outreach.py   # Outreach tracker + route optimizer
├── agents/
│   ├── seller_thesis.py     # Claude AI thesis + offer model + region summary
│   ├── doorknock_script.py  # Door-knock script + leave-behind letter generator
│   └── skip_trace.py        # Contact info pipeline (BatchSkipTracing + FastPeopleSearch)
├── db/
│   └── schema.py            # DuckDB schema (all tables)
├── ingestion/
│   ├── load_sample.py       # Excel → DuckDB (52 seed properties)
│   ├── anoka_county.py      # MN Geospatial Commons parcel download
│   └── snapshot.py          # Daily score snapshot + tier upgrade detection
├── scoring/
│   └── motivation.py        # 0-100 motivation score engine (fully explainable)
├── data/
│   ├── rei.duckdb           # Main database (held by Streamlit)
│   ├── parcels.duckdb       # Parcel data (separate, no conflict)
│   ├── skip_trace_upload.csv
│   └── daily_summary_*.txt
└── run_daily.py             # Daily automation entry point
```

**Multi-process note:** DuckDB takes an exclusive write lock. Streamlit holds `rei.duckdb`. CLI scripts that write must stop Streamlit first, or use `read_only=True` for read-only operations. Parcel data uses a separate `parcels.duckdb` to avoid conflicts.

---

## Repo

`github.com/davidohnstad40-netizen/real-estate-intel` (private)
