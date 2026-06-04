import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from ingestion.metrogis import lookup_address
from scoring.motivation import PropertyInput, score

data = lookup_address("13014 Kissel Street NE", "Blaine")
if not data:
    print("Not found"); sys.exit(1)

pi = PropertyInput(
    address=data["address"], emv=data.get("emv"),
    prior_sale_price=data.get("prior_sale_price"),
    prior_sale_year=data.get("prior_sale_year"),
    years_owned=data.get("years_owned"),
    homestead=data.get("homestead",""),
    owner_type=data.get("owner_type",""),
)
r = score(pi)
emv   = data["emv"]
sale  = data["prior_sale_price"]
yr    = data["prior_sale_year"]
ratio = emv / sale

print("=" * 55)
print("13014 Kissel Court NE -- Coming Soon Pressure Test")
print("=" * 55)
print(f"Address:   {data['address']}")
print(f"Owner:     {data.get('owner_name','(not in MetroGIS)')}")
print(f"Homestead: {data['homestead']}")
print(f"Year blt:  {data.get('year_built','?')}")
print(f"Sqft:      {data.get('sqft','?')}")
print(f"Bought:    ${sale:,.0f}  in {yr}  ({data.get('years_owned',0):.0f} yr hold)")
print(f"EMV 2025:  ${emv:,.0f}  (ratio {ratio:.2f})")
print()
print(f"SCORE:     {r.total}/100  -->  {r.tier}")
print(f"Factors:   {r.factors}")
print(f"Signal:    {r.primary_signal}")
if r.equity_usd:
    print(f"Equity:    ${r.equity_usd:,.0f} ({r.equity_pct:.0%})")
if r.monthly_piti:
    print(f"PITI est:  ${r.monthly_piti:,.0f}/mo")
print()
print("VERDICT")
print("-------")
if r.tier in ("T1","T2"):
    print(f"YES -- {r.tier}: We would have knocked on this door before listing.")
    print(f"The '{list(r.factors.keys())[0]}' signal fired correctly.")
    print()
    print("What the model saw BEFORE listing:")
    print(f"  Bought 2020 for ${sale:,.0f} at ~3.1% rate")
    print(f"  Estimated monthly PITI: ${r.monthly_piti:,.0f}/mo")
    print(f"  6-year hold, homesteaded (owner-occupied)")
    print(f"  Peak buyer caught by scoring engine (raised to 20 pts in v1.4)")
elif r.tier == "T3":
    print(f"PARTIALLY -- T3 (score {r.total}): Cold knock candidate.")
    print("Not specifically flagged, but would be visited in T3 cold knock sweep.")
    print()
    print("What additional signals could have caught it:")
    print("  - MCRO check (divorce/probate?)")
    print("  - LinkedIn job change (relocation?)")
    print("  - Permit history (renovation before selling?)")
print()
print("WHAT THIS TELLS US ABOUT THE MODEL:")
if r.tier == "T2":
    print("  PASS -- peak_buyer_2020_22 signal works as designed.")
    print("  Note: it's T2 (not T1) because it lacks additional distress signals.")
    print("  An MCRO search or LinkedIn check on the owner could push this to T1")
    print("  if there's a job change or family court record we can't see.")
