"""Check 13014 Kissel Street -- would we have caught it before listing?"""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.metrogis import lookup_address, _query
from scoring.motivation import PropertyInput, score, TIER_LABEL
import urllib.parse

print("Looking up 13014 Kissel Street NE, Blaine MN...")

# Try multiple variants
variants = [
    ("13014 Kissel Street NE", "Blaine"),
    ("13014 Kissel St NE",     "Blaine"),
    ("13014 Kissel",           "Blaine"),
]

data = None
for addr, city in variants:
    data = lookup_address(addr, city)
    if data:
        print(f"Found via: '{addr}'")
        break

if not data:
    # Try a raw MetroGIS query by house number + street name fragment
    print("Standard lookup failed -- trying raw MetroGIS query...")
    params = {
        "where":       "CTU_NAME='BLAINE' AND ANUMBER=13014 AND UPPER(ST_NAME) LIKE 'KISSEL%'",
        "outFields":   "COUNTY_PIN,ANUMBER,ST_NAME,ST_POS_TYP,ST_POS_DIR,OWNER_NAME,"
                       "HOMESTEAD,EMV_TOTAL,SALE_DATE,SALE_VALUE,FIN_SQ_FT,YEAR_BUILT",
        "outSR":       "4326",
        "returnGeometry": "true",
        "f":           "json",
        "resultRecordCount": 5,
    }
    from ingestion.metrogis import _parse_feature
    results = _query(params)
    if results:
        data = results[0]
        print(f"Found via raw query: {data.get('address','?')}")

if not data:
    print()
    print("NOT FOUND in MetroGIS 2025 parcel data for Blaine.")
    print("Checking neighboring cities...")
    for city in ["Ham Lake", "Anoka", "Champlin", "Coon Rapids"]:
        data = lookup_address("13014 Kissel Street NE", city)
        if data:
            print(f"Found in: {city}")
            break
    if not data:
        print("Not found in any Anoka County city via MetroGIS.")
        print("Possible: Hennepin County address, or very new construction not yet in 2025 assessment")
        sys.exit(0)

print()
print("=" * 60)
print("PROPERTY DATA FROM METROGIS 2025")
print("=" * 60)
for k, v in data.items():
    if v is not None and str(v).strip() not in ("", "None"):
        print(f"  {k:<20}: {v}")

print()
print("=" * 60)
print("SCORING ENGINE RESULT (pre-listing simulation)")
print("=" * 60)

pi = PropertyInput(
    address          = data.get("address", "13014 Kissel Street NE"),
    emv              = data.get("emv"),
    prior_sale_price = data.get("prior_sale_price"),
    prior_sale_year  = data.get("prior_sale_year"),
    years_owned      = data.get("years_owned"),
    homestead        = data.get("homestead", ""),
    owner_type       = data.get("owner_type", ""),
    owner_name       = data.get("owner_name", ""),
)
r = score(pi)

TIER_COLORS = {"T1": "\033[91m", "T2": "\033[93m", "T3": "\033[92m",
               "LISTED": "\033[94m", "SKIP": "\033[90m"}
RESET = "\033[0m"
color = TIER_COLORS.get(r.tier, "")

print(f"  Score:          {r.total}/100")
print(f"  Tier:           {color}{r.tier}  ({TIER_LABEL.get(r.tier, r.tier)}){RESET}")
print(f"  Factors:        {r.factors}")
print(f"  Signal:         {r.primary_signal}")

if r.equity_usd:
    print(f"  Est. Equity:    ${r.equity_usd:,.0f} ({r.equity_pct:.0%})")
if r.monthly_piti:
    print(f"  Monthly PITI:   ${r.monthly_piti:,.0f}/mo")

# EMV lag check
emv = data.get("emv") or 0
sale = data.get("prior_sale_price") or 0
yr   = data.get("prior_sale_year") or 0
if emv and sale:
    ratio = emv / sale
    lag = ratio < 0.40 and yr >= 2022
    print(f"  EMV/Sale ratio: {ratio:.2f}  {'[EMV lag -- new construction]' if lag else '[real data]'}")

print()
print("=" * 60)
print("VERDICT: Would we have caught it before listing?")
print("=" * 60)

if r.tier == "T1":
    print("  YES -- T1: We would have knocked on this door FIRST.")
    print(f"  Reason: {r.primary_signal}")
elif r.tier == "T2":
    print("  YES -- T2: We would have knocked on this door in the second wave.")
    print(f"  Reason: {r.primary_signal}")
elif r.tier == "T3":
    print("  PARTIALLY -- T3 (cold knock candidate, not specifically flagged).")
    print(f"  Score: {r.total}/100. Would have been visited eventually as a cold knock.")
    print(f"  What would have helped: MCRO check, LinkedIn job change, or skip trace")
    print(f"  revealing relocation signals.")
elif r.tier in ("LISTED", "SKIP"):
    print(f"  {r.tier}: {r.primary_signal}")
else:
    print(f"  Score {r.total}/100 -- {r.tier}")

# What signals WOULD have caught it
print()
print("ADDITIONAL SIGNALS THAT COULD HAVE CAUGHT IT:")
signals_to_check = []
if not r.factors.get("no_homestead") and data.get("homestead","").upper() in ("NO","N"):
    signals_to_check.append("No homestead (absentee) -- already in model")
if not any("divorce" in k for k in r.factors):
    signals_to_check.append("MCRO court check -- could reveal divorce/probate")
if not r.factors.get("linkedin_out_of_state"):
    signals_to_check.append("LinkedIn job change -- relocation signal")
if not r.factors.get("estate_sale_exact"):
    signals_to_check.append("Estate sale / obituary scan")
if signals_to_check:
    for s in signals_to_check:
        print(f"  - {s}")
else:
    print("  Model already captured key signals.")
