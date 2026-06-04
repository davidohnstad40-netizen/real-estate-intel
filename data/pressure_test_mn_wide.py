"""
MN-Wide Generalization Test (v2)
=================================
Tests the scoring model across different Anoka County cities AND attempts
Hennepin County via their own public ArcGIS endpoint.

Anoka County cities tested: Andover, Coon Rapids, Ramsey, Ham Lake,
  Lino Lakes, Fridley, Champlin, Anoka, East Bethel, Oak Grove

Hennepin County attempt: Maple Grove, Plymouth, Eden Prairie, Minnetonka
  (via https://gis.hennepin.us/arcgis)
"""

import sys, os, json, time, urllib.request, urllib.parse
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from scoring.motivation import PropertyInput, score

# ── Anoka County API ──────────────────────────────────────────────────────────
ANOKA_URL = ("https://arcgis.metc.state.mn.us/data1/rest/services/"
             "parcels/Parcel_Points_2025/FeatureServer/0/query")

# ── Hennepin County ArcGIS REST (public) ──────────────────────────────────────
# Hennepin County open data portal
HENN_URLS = [
    "https://gis.hennepin.us/arcgis/rest/services/HennepinData/TAXDATA/MapServer/0/query",
    "https://services.arcgis.com/ZOyb2t4B0UYuYNYH/arcgis/rest/services/Hennepin_Tax_Parcels/FeatureServer/0/query",
]

TIER_MAP = {"T1":"T1 KNOCK","T2":"T2 KNOCK","T3":"T3 Cold","SKIP":"SKIP"}

def _parse_ms(ms):
    if not ms: return None
    try: return datetime.fromtimestamp(ms/1000, tz=timezone.utc).year
    except: return None

def fetch_anoka(city: str, no_homestead: bool = False,
                sale_start: int = 2019, sale_end: int = 2023) -> dict | None:
    """Fetch one property from an Anoka County city."""
    hmst = "AND HOMESTEAD='No'" if no_homestead else "AND HOMESTEAD='Yes'"
    where = (f"CTU_NAME='{city.upper()}' AND USECLASS1 LIKE '1a%' AND "
             f"SALE_VALUE >= 300000 AND EMV_TOTAL >= 250000 AND "
             f"SALE_DATE >= DATE '{sale_start}-01-01' AND "
             f"SALE_DATE <= DATE '{sale_end}-12-31' {hmst}")
    params = {
        "where": where,
        "outFields": "ANUMBER,ST_NAME,ST_POS_TYP,ST_POS_DIR,OWNER_NAME,"
                     "HOMESTEAD,EMV_TOTAL,SALE_DATE,SALE_VALUE,FIN_SQ_FT",
        "outSR": "4326", "returnGeometry": "true",
        "f": "json", "resultRecordCount": 50,
        "orderByFields": "SALE_DATE DESC",
    }
    try:
        qs = urllib.parse.urlencode(params)
        req = urllib.request.Request(f"{ANOKA_URL}?{qs}",
                                     headers={"User-Agent":"REI/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
    except Exception as e:
        return None

    for f in data.get("features", []):
        a = f["attributes"]; geom = f.get("geometry",{})
        emv = a.get("EMV_TOTAL") or 0
        sale = a.get("SALE_VALUE") or 0
        if not emv or not sale or sale > 3_000_000: continue
        if emv / sale < 0.40: continue   # skip EMV lag

        num = str(a.get("ANUMBER",""))
        st  = (a.get("ST_NAME","") or "").strip().title()
        typ = (a.get("ST_POS_TYP","") or "").strip().title()
        dir_= (a.get("ST_POS_DIR","") or "").strip()
        addr = " ".join(x for x in [num, st, typ, dir_] if x)
        if not addr.strip(): continue
        sale_yr = _parse_ms(a.get("SALE_DATE"))
        hmst_v  = (a.get("HOMESTEAD","") or "").strip()
        return {
            "address": addr, "city": city,
            "homestead": hmst_v,
            "owner_type": "No Homestead" if hmst_v.upper() in ("NO","N") else "Owner-Occupied",
            "emv": emv, "prior_sale_price": sale, "prior_sale_year": sale_yr,
            "years_owned": (2026 - sale_yr) if sale_yr else None,
            "emv_ratio": round(emv/sale, 2),
        }
    return None


def try_hennepin(city: str) -> dict | None:
    """Try Hennepin County ArcGIS endpoints."""
    for url in HENN_URLS:
        try:
            params = {
                "where": f"CITY='{city.upper()}' AND HOMESTEAD_CD='N' AND MKT_VAL_TOT > 400000",
                "outFields": "*", "f": "json", "resultRecordCount": 5,
            }
            qs = urllib.parse.urlencode(params)
            req = urllib.request.Request(f"{url}?{qs}",
                                         headers={"User-Agent":"REI/1.0"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = json.loads(r.read())
            if data.get("features"):
                return {"city": city, "found_via": url,
                        "count": len(data["features"])}
        except Exception:
            continue
    return None


# ── ANOKA COUNTY TEST ─────────────────────────────────────────────────────────
ANOKA_CITIES = [
    ("Andover",    True,  2020, 2022),    # no-homestead peak buyer
    ("Andover",    False, 2007, 2011),    # long-hold homestead
    ("Coon Rapids",True,  2020, 2022),
    ("Coon Rapids",False, 2007, 2011),
    ("Ramsey",     True,  2020, 2022),
    ("Ham Lake",   False, 2007, 2011),
    ("Lino Lakes", True,  2020, 2022),
    ("Lino Lakes", False, 2007, 2011),
    ("Champlin",   True,  2020, 2022),
    ("Anoka",      False, 2007, 2011),
]

print("=" * 70)
print("MN-WIDE GENERALIZATION TEST")
print("Anoka County (10 properties) + Hennepin County attempt")
print("=" * 70)

results = []
for city, no_hmst, s_yr, e_yr in ANOKA_CITIES:
    label = "No-hmst" if no_hmst else "Long-hold"
    p = fetch_anoka(city, no_homestead=no_hmst, sale_start=s_yr, sale_end=e_yr)
    time.sleep(0.3)

    if not p:
        print(f"\n[{label}] {city}: NO DATA")
        results.append({"city": city, "label": label, "verdict": "NO DATA"})
        continue

    pi = PropertyInput(
        address=p["address"], emv=p["emv"],
        prior_sale_price=p["prior_sale_price"],
        prior_sale_year=p["prior_sale_year"],
        years_owned=p["years_owned"],
        homestead=p["homestead"], owner_type=p["owner_type"],
    )
    r = score(pi)

    emv = p["emv"]; sale = p["prior_sale_price"]; yrs = p["years_owned"]
    gain = f"+{(emv/sale-1)*100:.0f}%" if emv and sale else "?"
    factors_str = ", ".join(r.factors.keys()) if r.factors else "no signals"

    print(f"\n[{label}] {city}")
    print(f"  Address:   {p['address']}")
    print(f"  Score:     {r.total}/100  ->  {r.tier}  ({TIER_MAP.get(r.tier,r.tier)})")
    print(f"  Factors:   {factors_str}")
    print(f"  Data:      emv=${emv:,.0f} | bought=${sale:,.0f} ({p['prior_sale_year']}) | "
          f"held={yrs}yr | gain={gain} | ratio={p['emv_ratio']:.2f}")

    results.append({
        "city": city, "label": label, "address": p["address"],
        "score": r.total, "tier": r.tier, "factors": r.factors,
        "signal": r.primary_signal, "emv": emv, "sale": sale,
        "years_owned": yrs, "gain": gain, "emv_ratio": p["emv_ratio"],
    })

# ── HENNEPIN COUNTY PROBE ─────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("HENNEPIN COUNTY API PROBE")
print("=" * 70)
for hcity in ["Maple Grove", "Plymouth", "Eden Prairie"]:
    result = try_hennepin(hcity)
    if result:
        print(f"  FOUND: {hcity} via {result['found_via']} ({result['count']} records)")
    else:
        print(f"  NO ACCESS: {hcity} - Hennepin County API not publicly accessible")

# ── FINAL ANALYSIS ────────────────────────────────────────────────────────────
print("\n" + "=" * 70)
print("ANALYSIS: Does the model generalize across cities?")
print("=" * 70)

scored = [r for r in results if r.get("score") is not None]
no_hmst_rs = [r for r in scored if r["label"] == "No-hmst"]
long_hold_rs = [r for r in scored if r["label"] == "Long-hold"]

print(f"\nNo-homestead 2020-22 peak buyers ({len(no_hmst_rs)} properties):")
for r in no_hmst_rs:
    print(f"  [{r['tier']:2}] {r['city']:<15} score={r['score']:3d} | "
          f"gain={r['gain']:>7} | {', '.join((r.get('factors') or {}).keys())[:40]}")

print(f"\nLong-hold 2007-2011 homesteaded ({len(long_hold_rs)} properties):")
for r in long_hold_rs:
    print(f"  [{r['tier']:2}] {r['city']:<15} score={r['score']:3d} | "
          f"gain={r['gain']:>7} | {', '.join((r.get('factors') or {}).keys())[:40]}")

t1t2_no_hmst  = sum(1 for r in no_hmst_rs if r["tier"] in ("T1","T2"))
t1t2_long_hld = sum(1 for r in long_hold_rs if r["tier"] in ("T1","T2"))

print(f"\nNo-homestead T1+T2 rate: {t1t2_no_hmst}/{len(no_hmst_rs)}")
print(f"Long-hold T1+T2 rate:    {t1t2_long_hld}/{len(long_hold_rs)}")

print("\nMODEL CONSISTENCY CHECK:")
if all(r["tier"] in ("T1","T2") for r in no_hmst_rs if r.get("tier")):
    print("  PASS: no-homestead 2020-22 buyers consistently score T1/T2 across cities")
else:
    misses = [r for r in no_hmst_rs if r.get("tier") not in ("T1","T2")]
    print(f"  INCONSISTENT: {len(misses)} no-homestead properties scored T3:")
    for r in misses:
        print(f"    {r['city']}: score={r['score']} factors={r.get('factors')}")

if all(r["tier"] in ("T2","T3") for r in long_hold_rs if r.get("tier")):
    print("  PASS: long-hold homesteaded owners consistently score T2/T3 across cities")
else:
    t1_lh = [r for r in long_hold_rs if r.get("tier") == "T1"]
    if t1_lh:
        print(f"  NOTE: {len(t1_lh)} long-hold homesteaded scored T1 (check factors):")
        for r in t1_lh:
            print(f"    {r['city']}: {r.get('factors')}")

print("\nDATA QUALITY CHECK (EMV ratio should be 0.60-1.20 for real sales):")
bad_ratio = [r for r in scored if r.get("emv_ratio",1) < 0.50 or r.get("emv_ratio",1) > 1.50]
if bad_ratio:
    print(f"  WARNING: {len(bad_ratio)} properties with suspicious EMV ratio:")
    for r in bad_ratio:
        print(f"    {r['city']} {r['address']}: ratio={r['emv_ratio']:.2f}")
else:
    print("  PASS: all EMV ratios in expected range (0.50-1.50)")
