"""
New 10-property pressure test with fixed scoring engine.
Strategy: query MetroGIS for 3-10 year holders currently listed on market
by finding properties where SALE_DATE is 2015-2022 (the current owner bought
then and HASN'T sold yet) AND cross-referencing with known recent listings.

For properties already in our watchlist/previous tests, we check if they
actually sold/listed -- validating the model's predictions.
"""
import sys, os, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.metrogis import lookup_address
from scoring.motivation import PropertyInput, score

TIER_MAP = {"T1": "T1 KNOCK", "T2": "T2 KNOCK", "T3": "T3 Cold", "SKIP": "SKIP"}

# Fresh addresses: properties we HAVEN'T tested yet where the owner is known
# to have sold recently (validated from realtor.com / county records)
# Selection criteria: SALE_DATE 2019-2022, EMV > 400K, NO prior test
# These are longer-hold sellers where the EMV ratio test tells us if data is real
NEW_TARGETS = [
    # --- From Blaine MN 2024-2025 MLS data / county records ---
    ("4742 117th Lane NE",       "listed/sold 2025 ~$650K"),
    ("3455 117th Lane NE",       "sold 2024"),
    ("11234 Fillmore St NE",     "sold 2025 $580K"),
    ("4419 123rd Cir NE",        "sold 2025"),
    ("12218 Dunkirk St NE",      "listed 2025"),
    ("3308 115th Cir NE",        "sold 2025 $720K"),
    ("4651 128th Cir NE",        "sold 2024"),
    ("11325 Hanson Blvd NE",     "listed 2025"),
    ("2974 Aspen Lake Dr NE",    "sold 2025"),
    ("4718 131st Ct NE",         "listed 2026 active"),
]

print("=" * 70)
print("NEW 10-PROPERTY PRESSURE TEST (fixed scoring engine v1.4)")
print("=" * 70)

results = []
for addr, outcome in NEW_TARGETS:
    data = lookup_address(addr)
    time.sleep(0.3)

    if not data:
        print(f"  NOT FOUND: {addr}")
        results.append({"address": addr, "outcome": outcome, "verdict": "NO DATA"})
        continue

    yrs  = data.get("years_owned")
    sale_yr = data.get("prior_sale_year")
    emv  = data.get("emv") or 0
    sale = data.get("prior_sale_price") or 0
    is_new = yrs is not None and yrs <= 2

    if is_new:
        print(f"  NEW OWNER: {addr} (sale_yr={sale_yr})")
        results.append({
            "address": addr, "outcome": outcome,
            "verdict": "SOLD-NEW OWNER", "emv": emv,
            "sale_yr": sale_yr, "yrs_owned": yrs,
            "homestead": data.get("homestead"), "score": None, "tier": None,
        })
        continue

    pi = PropertyInput(
        address          = addr,
        emv              = emv,
        prior_sale_price = sale,
        prior_sale_year  = sale_yr,
        years_owned      = yrs,
        homestead        = data.get("homestead",""),
        owner_type       = data.get("owner_type",""),
        owner_name       = data.get("owner_name",""),
    )
    r = score(pi)

    # Check EMV lag flag to note data quality
    emv_lag = (sale > 0 and emv > 0 and emv/sale < 0.40
               and sale_yr and sale_yr >= 2022)
    lag_note = " [EMV_LAG suppressed]" if emv_lag else ""

    verdict = "CAUGHT (T1/T2)" if r.tier in ("T1","T2") else "MISSED (T3)"
    print(f"  {verdict:16}: {addr} | score={r.total} {r.tier} | "
          f"{', '.join(r.factors.keys()) or 'no factors'}{lag_note}")

    results.append({
        "address": addr, "outcome": outcome, "verdict": verdict,
        "score": r.total, "tier": r.tier, "factors": r.factors,
        "signal": r.primary_signal, "emv": emv, "sale_yr": sale_yr,
        "yrs_owned": yrs, "homestead": data.get("homestead"),
        "absentee": data.get("absentee"), "prior_price": sale,
    })

# ── Analysis ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("RESULTS TABLE")
print("=" * 70)

scoreable = [r for r in results if r["verdict"] not in ("NO DATA","SOLD-NEW OWNER")]
new_owner = [r for r in results if r["verdict"] == "SOLD-NEW OWNER"]
caught    = [r for r in scoreable if "CAUGHT" in r["verdict"]]

for r in results:
    v = r["verdict"][:9]
    s = str(r.get("score","--"))
    t = r.get("tier","--") or "--"
    emv_s  = "${:,.0f}".format(r["emv"]) if r.get("emv") else "?"
    price_s = "${:,.0f}".format(r["prior_price"]) if r.get("prior_price") else "?"
    yrs_s  = "{:.0f}yr".format(r["yrs_owned"]) if r.get("yrs_owned") else "?"
    facs   = ", ".join(r.get("factors",{}).keys()) if r.get("factors") else "--"
    print(f"[{v:9}] {r['address']:<28} | {s:>3} {t:<2} | "
          f"emv={emv_s:<10} bought={price_s:<10} ({yrs_s}) | {r['outcome']}")
    if facs != "--":
        print(f"            factors: {facs}")

# ── Combined with prior 33 ────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("CUMULATIVE SCORECARD (all 43 properties tested)")
print("=" * 70)
prior_caught     = 9   # 5 original + 4 from second batch (with new scoring)
prior_scoreable  = 20  # 11 + 9
new_scored       = len(scoreable)
new_caught       = len(caught)
total_caught     = prior_caught + new_caught
total_scoreable  = prior_scoreable + new_scored
total_no_data    = sum(1 for r in results if r["verdict"] == "NO DATA")
total_new_owner  = sum(1 for r in results if "SOLD" in r["verdict"])

print(f"  Scoreable:          {total_scoreable}")
print(f"  Caught (T1/T2):     {total_caught}  ({total_caught*100//max(total_scoreable,1)}%)")
print(f"  Missed (T3):        {total_scoreable - total_caught}")
print(f"  New owner/no data:  {total_no_data + total_new_owner + 2}  (unscorable retroactively)")
print()
print(f"  OVERALL HIT RATE: {total_caught}/{total_scoreable} = "
      f"{total_caught*100//max(total_scoreable,1)}%")
print()
print("SCORING ENGINE IMPROVEMENT (v1.3 -> v1.4):")
print("  Before fix: new construction false T1s (Zest St = score 52, was noise)")
print("  After fix:  new construction scores T2 on real signals only (score 32)")
print("  EMV lag guard prevents negative_equity from firing on 2022+ builds")
print("  Deduplication reduces city scan from 23,497 to unique addresses")
