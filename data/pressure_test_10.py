"""
Pressure test against 10 recently listed/sold homes in Blaine MN 55449.
Queries MetroGIS for live county data, scores through engine, reports.
"""
import sys, os, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.metrogis import lookup_address
from scoring.motivation import PropertyInput, score

# 10 recently listed / recently sold homes in Blaine 55449
TARGETS = [
    ("3094 Aspen Lake Dr NE",  "sold Apr 2026 $900K"),
    ("2866 121st Ct NE",       "sold Apr 2026 $940K"),
    ("2812 Aspen Lake Dr NE",  "sold Apr 2025 ~$700K"),
    ("3564 117th Ln NE",       "sold Oct 2024 $875K"),   # adjacent to our 52
    ("3901 125th Ave NE",      "sold 2025 $875K"),
    ("4779 131st Ct NE",       "listed 2026 $935K"),      # new construction
    ("4550 131st Ave NE",      "sold May 2025 $661K"),
    ("12745 Stutz Ct NE",      "listed 2025/26 $1M"),
    ("11871 Flanders Cir NE",  "pending 2026 $830K"),
    ("1912 130th Ln NE",       "sold Jun 2025 $495K"),
]

TIER_MAP = {"T1": "T1 KNOCK", "T2": "T2 KNOCK", "T3": "T3 Cold", "SKIP": "SKIP"}

print("=" * 70)
print("PRESSURE TEST -- 10 Recently Listed/Sold Blaine MN 55449 Homes")
print("=" * 70)
print()

caught_t1t2 = 0
results = []

for addr, known_outcome in TARGETS:
    print(f"Looking up: {addr}...", end=" ", flush=True)
    data = lookup_address(addr)
    time.sleep(0.3)  # polite rate limiting

    if not data:
        print("NOT FOUND in MetroGIS")
        results.append({
            "address": addr, "outcome": known_outcome,
            "verdict": "NO DATA", "score": None, "tier": None,
            "notes": "Address not found in MetroGIS 2025 parcel data"
        })
        continue

    sale_yr = data.get("prior_sale_year")
    yrs_owned = data.get("years_owned")
    is_new_buyer = yrs_owned is not None and yrs_owned <= 2

    if is_new_buyer:
        # MetroGIS now shows the NEW buyer (seller already sold)
        note = (f"MetroGIS shows NEW buyer (sale_yr={sale_yr}, "
                f"yrs_owned={yrs_owned:.0f}). Previous seller's signals not scoreable.")
        print(f"NEW BUYER DATA (sale_yr={sale_yr})")
        results.append({
            "address": addr, "outcome": known_outcome,
            "verdict": "SOLD - NEW OWNER", "score": None, "tier": None,
            "notes": note,
            "homestead": data.get("homestead"),
            "emv": data.get("emv"),
        })
        continue

    # Current owner is still the seller -- score them
    pi = PropertyInput(
        address          = addr,
        emv              = data.get("emv"),
        prior_sale_price = data.get("prior_sale_price"),
        prior_sale_year  = sale_yr,
        years_owned      = yrs_owned,
        homestead        = data.get("homestead",""),
        owner_type       = data.get("owner_type",""),
        owner_name       = data.get("owner_name",""),
    )
    r = score(pi)

    verdict = "CAUGHT (T1/T2)" if r.tier in ("T1","T2") else "MISSED (T3)"
    if r.tier in ("T1","T2"):
        caught_t1t2 += 1

    print(f"score={r.total} {r.tier} -- {verdict}")
    results.append({
        "address":     addr,
        "outcome":     known_outcome,
        "verdict":     verdict,
        "score":       r.total,
        "tier":        r.tier,
        "factors":     r.factors,
        "signal":      r.primary_signal,
        "homestead":   data.get("homestead"),
        "emv":         data.get("emv"),
        "sale_yr":     sale_yr,
        "yrs_owned":   yrs_owned,
        "absentee":    data.get("absentee"),
        "notes":       "",
    })

print()
print("=" * 70)
print("DETAILED RESULTS")
print("=" * 70)

scoreable = [r for r in results if r["verdict"] not in ("NO DATA","SOLD - NEW OWNER")]
new_owner = [r for r in results if r["verdict"] == "SOLD - NEW OWNER"]
no_data   = [r for r in results if r["verdict"] == "NO DATA"]

for r in results:
    print(f"\n[{r['verdict']}] {r['address']}")
    print(f"  Known outcome: {r['outcome']}")
    if r["score"] is not None:
        print(f"  Score: {r['score']}/100 -> {r['tier']} ({TIER_MAP.get(r['tier'],'')})")
        print(f"  Factors: {r['factors']}")
        print(f"  Signal: {(r['signal'] or '')[:80]}")
        yrs_s = f"{r['yrs_owned']:.0f} yrs" if r.get("yrs_owned") else "?"
        emv_s = f"${r['emv']:,.0f}" if r.get("emv") else "?"
        print(f"  Homestead: {r['homestead']} | EMV: {emv_s} | "
              f"Bought: {r['sale_yr']} | Held: {yrs_s} | "
              f"Absentee: {r['absentee']}")
    elif r["verdict"] == "SOLD - NEW OWNER":
        print(f"  {r['notes']}")
        print(f"  (Homestead of NEW owner: {r.get('homestead','?')} | EMV: ${r.get('emv',0):,.0f})")
    else:
        print(f"  {r['notes']}")

print()
print("=" * 70)
print("SUMMARY")
print("=" * 70)
print(f"Total tested:      {len(TARGETS)}")
print(f"Scoreable (still current owner): {len(scoreable)}")
print(f"Already sold (new owner in MetroGIS): {len(new_owner)}")
print(f"Not found: {len(no_data)}")
print(f"Caught as T1/T2 (of scoreable): {caught_t1t2}/{len(scoreable)}")
print()
print("MODEL INSIGHTS:")
print()

# Combined with previous 3
total_catch = caught_t1t2 + 2  # 2/3 from previous test
total_tested = len(scoreable) + 3
if total_tested:
    print(f"Combined with earlier test: {total_catch}/{total_tested} scoreable properties flagged T1/T2")
print()
print("KEY FINDING ON 'SOLD - NEW OWNER' CASES:")
print("  When a property sells, MetroGIS immediately updates to show the new buyer.")
print("  This means we can't retroactively score the SELLER's signals.")
print("  The right approach: run the model BEFORE they list, not after they sell.")
print("  The platform needs ONGOING monitoring, not one-time snapshots.")
