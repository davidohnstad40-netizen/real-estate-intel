"""
Expanded pressure test: 20 more recent Blaine MN 55449 listings/sales.
Queries MetroGIS, scores each, reports verdict + model gap analysis.
"""
import sys, os, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.metrogis import lookup_address
from scoring.motivation import PropertyInput, score

TARGETS = [
    # --- Recently Sold ---
    ("12662 Fraizer St NE",    "sold Jan 2025"),
    ("3777 112th Cir NE",      "sold Jun 2025"),
    ("2127 120th Ln NE",       "sold Jun 2025"),
    ("2170 120th Ln NE",       "sold Jun 2025"),
    ("12469 Midway Cir NE",    "sold May 2024"),
    ("4403 123rd Cir NE",      "sold Sep 2024"),
    ("4470 131st Ave NE",      "sold Dec 2024"),
    ("4817 132nd Ct NE",       "sold Jan 2025"),
    ("3076 131st Ct NE",       "sold Jan 2025"),
    ("2686 124th Dr NE",       "sold May 2025"),
    ("13084 Marmon Ct NE",     "sold Sep 2024"),
    ("4803 132nd Ct NE",       "sold/listed 2024-25"),
    # --- Active / Pending ---
    ("13157 Coral Sea Ct NE",  "listed 2025 active"),
    ("2025 131st Ct NE",       "active listing"),
    ("3105 123rd Ct NE",       "listed Mar 2026"),
    ("12614 Erskin St NE",     "listed May 2026"),
    ("10754 Coral Sea St NE",  "active 2025"),
    ("13175 Coral Sea Ct NE",  "active listing"),
    ("12205 Flanders St NE",   "listed/sold 2025"),
    ("4625 132nd Ln NE",       "sold Apr 2026"),
]

TIER_LABEL = {"T1":"T1 KNOCK","T2":"T2 KNOCK","T3":"T3 Cold","SKIP":"SKIP"}

print("=" * 70)
print("EXPANDED PRESSURE TEST -- 20 Recent Blaine MN 55449 Sales/Listings")
print("=" * 70)

results = []
for addr, outcome in TARGETS:
    data = lookup_address(addr)
    time.sleep(0.25)

    if not data:
        results.append({"address": addr, "outcome": outcome,
                        "verdict": "NO DATA", "score": None, "tier": None,
                        "emv": None, "sale_yr": None, "yrs_owned": None,
                        "homestead": None, "absentee": None,
                        "prior_price": None, "factors": {}})
        print(f"  NOT FOUND: {addr}")
        continue

    yrs  = data.get("years_owned")
    sale_yr = data.get("prior_sale_year")
    is_new  = yrs is not None and yrs <= 2

    if is_new:
        results.append({"address": addr, "outcome": outcome,
                        "verdict": "SOLD-NEW OWNER",
                        "score": None, "tier": None,
                        "emv": data.get("emv"), "sale_yr": sale_yr,
                        "yrs_owned": yrs, "homestead": data.get("homestead"),
                        "absentee": data.get("absentee"),
                        "prior_price": data.get("prior_sale_price"), "factors": {}})
        print(f"  NEW OWNER  : {addr} (sale_yr={sale_yr})")
        continue

    pi = PropertyInput(
        address          = addr,
        emv              = data.get("emv"),
        prior_sale_price = data.get("prior_sale_price"),
        prior_sale_year  = sale_yr,
        years_owned      = yrs,
        homestead        = data.get("homestead",""),
        owner_type       = data.get("owner_type",""),
        owner_name       = data.get("owner_name",""),
    )
    r = score(pi)

    verdict = "CAUGHT (T1/T2)" if r.tier in ("T1","T2") else "MISSED (T3)"
    print(f"  {verdict:16}: {addr} | score={r.total} {r.tier} | {', '.join(r.factors.keys()) or 'no factors'}")

    results.append({"address": addr, "outcome": outcome,
                    "verdict": verdict, "score": r.total, "tier": r.tier,
                    "emv": data.get("emv"), "sale_yr": sale_yr,
                    "yrs_owned": yrs, "homestead": data.get("homestead"),
                    "absentee": data.get("absentee"),
                    "prior_price": data.get("prior_sale_price"),
                    "factors": r.factors,
                    "signal": r.primary_signal})

# ── Analysis ──────────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("FULL RESULTS TABLE")
print("=" * 70)

scoreable = [r for r in results if r["verdict"] not in ("NO DATA","SOLD-NEW OWNER")]
new_owner = [r for r in results if r["verdict"] == "SOLD-NEW OWNER"]
no_data   = [r for r in results if r["verdict"] == "NO DATA"]
caught    = [r for r in scoreable if "CAUGHT" in r["verdict"]]
missed    = [r for r in scoreable if "MISSED" in r["verdict"]]

for r in results:
    verdict_icon = ("NO DATA  " if r["verdict"]=="NO DATA"
                    else "NEW OWNER" if "SOLD-NEW" in r["verdict"]
                    else "CAUGHT   " if "CAUGHT" in r["verdict"]
                    else "MISSED   ")
    emv_s  = "${:,.0f}".format(r["emv"])  if r.get("emv")  else "?"
    price_s = "${:,.0f}".format(r["prior_price"]) if r.get("prior_price") else "?"
    yrs_s  = "{:.0f}yr".format(r["yrs_owned"]) if r.get("yrs_owned") else "?"
    score_s = str(r["score"]) if r.get("score") is not None else "--"
    print(f"[{verdict_icon}] {r['address']:<28} | score={score_s:>3} "
          f"| emv={emv_s:>10} | bought={price_s:>10} ({yrs_s}) "
          f"| hmst={str(r.get('homestead','?')):<3}"
          f"| {r.get('outcome','')}")

# ── Factor frequency analysis ─────────────────────────────────────────────────
print("\n" + "=" * 70)
print("FACTOR FREQUENCY (what signals fired on CAUGHT properties)")
print("=" * 70)
factor_counts = {}
for r in caught:
    for f in r.get("factors",{}).keys():
        factor_counts[f] = factor_counts.get(f,0) + 1
for f, c in sorted(factor_counts.items(), key=lambda x:-x[1]):
    print(f"  {f}: {c}x")

# ── Miss profile analysis ─────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("MISS PROFILE ANALYSIS (what the model didn't see)")
print("=" * 70)
hmst_misses  = sum(1 for r in missed if str(r.get("homestead","")).upper() in ("YES","Y"))
abst_misses  = sum(1 for r in missed if r.get("absentee"))
yrs_15plus   = sum(1 for r in missed if r.get("yrs_owned") and r["yrs_owned"] >= 15)
yrs_5_14     = sum(1 for r in missed if r.get("yrs_owned") and 5 <= r["yrs_owned"] < 15)
peak_buyers  = sum(1 for r in missed if r.get("sale_yr") and 2020 <= r["sale_yr"] <= 2022)
no_sale_data = sum(1 for r in missed if not r.get("sale_yr"))

print(f"  Misses total:               {len(missed)}")
print(f"  Homesteaded misses:         {hmst_misses} ({hmst_misses*100//max(len(missed),1)}%)")
print(f"  Absentee misses:            {abst_misses}")
print(f"  Long hold 15+ yr misses:   {yrs_15plus}")
print(f"  Mid hold 5-14 yr misses:   {yrs_5_14}")
print(f"  Peak buyer 2020-22 misses: {peak_buyers}")
print(f"  No sale date in MetroGIS:  {no_sale_data}")

# ── Combined scorecard ────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("COMBINED SCORECARD (all 33 properties: 13 prior + 20 new)")
print("=" * 70)

prior_caught = 5
prior_scoreable = 11
total_caught    = prior_caught + len(caught)
total_scoreable = prior_scoreable + len(scoreable)
total_new_owner = 2 + len(new_owner)  # 2 from prior test
total_no_data   = len(no_data)

print(f"  Scoreable:        {total_scoreable}")
print(f"  Caught (T1/T2):   {total_caught} ({total_caught*100//max(total_scoreable,1)}%)")
print(f"  Missed (T3):      {total_scoreable - total_caught}")
print(f"  New owner (unscorable): {total_new_owner}")
print(f"  No MetroGIS data: {total_no_data}")
print()
print(f"  OVERALL T1/T2 HIT RATE: {total_caught}/{total_scoreable} = "
      f"{total_caught*100//max(total_scoreable,1)}%")
