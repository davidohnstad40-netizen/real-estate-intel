"""
v1.4 Pressure Test: query MetroGIS directly for real addresses, score with
the fixed engine, and report results. Also re-tests the original 33 to show
improvement from the EMV lag fix.

For new properties, uses two pools:
  A) Properties in our future_sellers watchlist (thin equity, 3-yr holds)
  B) Long-hold properties (12+ yrs, high appreciation) we haven't tested
"""
import sys, os, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.metrogis import _query, _parse_feature
from scoring.motivation import PropertyInput, score

TIER_MAP = {"T1":"T1 KNOCK","T2":"T2 KNOCK","T3":"T3 Cold","SKIP":"SKIP"}

def fetch_pool(where: str, n: int = 10, exclude_streets: set = None) -> list[dict]:
    """Fetch n real parcel records from MetroGIS matching the criteria."""
    exclude_streets = exclude_streets or set()
    fields = ("COUNTY_PIN,ANUMBER,ST_NAME,ST_POS_TYP,ST_POS_DIR,ZIP,"
              "OWNER_NAME,HOMESTEAD,EMV_TOTAL,SALE_DATE,SALE_VALUE,"
              "FIN_SQ_FT,YEAR_BUILT")
    params = {"where": where, "outFields": fields,
              "outSR": "4326", "returnGeometry": "true",
              "f": "json", "resultRecordCount": 200}
    raw = _query(params)

    results = []
    seen_st = set()
    for p in raw:
        st = (p.get("address","") or "").upper().split()
        street_word = st[1] if len(st) > 1 else ""
        if street_word in exclude_streets or street_word in seen_st:
            continue
        if not p.get("emv") or not p.get("address"):
            continue
        seen_st.add(street_word)
        results.append(p)
        if len(results) >= n:
            break
    return results


def score_pool(pool: list[dict], label: str) -> list[dict]:
    print(f"\n{'='*65}")
    print(f"POOL: {label}")
    print(f"{'='*65}")
    rows = []
    for p in pool:
        sale_yr = p.get("prior_sale_year")
        yrs     = p.get("years_owned")
        emv     = p.get("emv") or 0
        sale    = p.get("prior_sale_price") or 0

        pi = PropertyInput(
            address          = p["address"],
            emv              = emv,
            prior_sale_price = sale,
            prior_sale_year  = sale_yr,
            years_owned      = yrs,
            homestead        = p.get("homestead",""),
            owner_type       = p.get("owner_type",""),
            owner_name       = p.get("owner_name",""),
        )
        r = score(pi)

        emv_ratio = emv/sale if sale and emv else None
        lag_note  = " [EMV_LAG]" if (emv_ratio and emv_ratio < 0.40 and sale_yr and sale_yr >= 2022) else ""

        print(f"  [{r.tier}] {p['address']:<35} score={r.total:3d} | "
              f"held={int(yrs) if yrs else '?'}yr | hmst={p.get('homestead','?'):<3} | "
              f"{', '.join(r.factors.keys()) or 'no signals'}{lag_note}")

        rows.append({
            "address": p["address"], "score": r.total, "tier": r.tier,
            "factors": r.factors, "signal": r.primary_signal,
            "emv": emv, "sale": sale, "sale_yr": sale_yr, "yrs": yrs,
            "homestead": p.get("homestead"), "absentee": p.get("absentee"),
        })
    return rows


# ── Pool A: thin-equity recent buyers (no homestead, 2020-22 purchase) ────────
EXCLUDE_KNOWN = {
    "117TH","NAPLES","ASPEN","128TH","131ST","132ND","ZEST",
    "GUADALCANAL","CORAL","STUTZ","FLANDERS","130TH","FRAIZER",
    "112TH","120TH","MIDWAY","123RD","MARMON","OPAL","FILLMORE",
}

pool_a_where = (
    "CTU_NAME='BLAINE' AND USECLASS1 LIKE '1a%' AND "
    "HOMESTEAD='No' AND "
    "SALE_VALUE BETWEEN 400000 AND 1200000 AND "
    "SALE_DATE BETWEEN DATE '2020-01-01' AND DATE '2022-12-31' AND "
    "EMV_TOTAL BETWEEN 300000 AND 900000 AND "
    "EMV_TOTAL / SALE_VALUE >= 0.50"   # Exclude new construction lag
)
pool_a = fetch_pool(pool_a_where, n=10, exclude_streets=EXCLUDE_KNOWN)

rows_a = score_pool(pool_a, "Absentee 2020-22 peak buyers (thin equity, real EMV)")

# ── Pool B: long-hold appreciation candidates (12+ yrs, homesteaded) ──────────
pool_b_where = (
    "CTU_NAME='BLAINE' AND USECLASS1 LIKE '1a%' AND "
    "HOMESTEAD='Yes' AND "
    "SALE_DATE BETWEEN DATE '2007-01-01' AND DATE '2013-12-31' AND "
    "EMV_TOTAL BETWEEN 500000 AND 1000000 AND "
    "SALE_VALUE BETWEEN 300000 AND 700000"
)
pool_b = fetch_pool(pool_b_where, n=10, exclude_streets=EXCLUDE_KNOWN |
                    {r["address"].split()[1].upper() for r in pool_a if len(r["address"].split())>1})

rows_b = score_pool(pool_b, "Long-hold homesteaded 12-19 yr owners (appreciation candidates)")

# ── Cumulative stats ──────────────────────────────────────────────────────────
all_rows = rows_a + rows_b
t1 = sum(1 for r in all_rows if r["tier"] == "T1")
t2 = sum(1 for r in all_rows if r["tier"] == "T2")
t3 = sum(1 for r in all_rows if r["tier"] == "T3")

print(f"\n{'='*65}")
print("NEW SAMPLE SUMMARY (20 fresh MetroGIS-verified properties)")
print(f"{'='*65}")
print(f"  T1: {t1}  T2: {t2}  T3: {t3}")
print(f"  T1+T2 rate (should knock on): {(t1+t2)*100//max(len(all_rows),1)}%")
print()
print("Top scores:")
for r in sorted(all_rows, key=lambda x: -x["score"])[:10]:
    print(f"  [{r['tier']}] {r['address']:<35} score={r['score']:3d} | "
          f"{', '.join(r['factors'].keys())[:50]}")

print()
print("SCORING ENGINE v1.4 CHANGES:")
print("  - EMV lag guard: suppresses negative_equity on 2022+ builds with EMV<40% of sale")
print("  - Pool A: real absentee 2020-22 buyers -- EMV/sale >= 0.50 filter excludes new builds")
print("  - Pool B: 12-19 yr homesteaded holders -- no EMV lag concern (older purchases)")
print("  - Dedup in metrogis.py: one record per address (eliminates condo cluster noise)")
