"""
MetroGIS Parcel API
===================
Queries the Metropolitan Council's public ArcGIS REST API for Anoka County
parcel data. Returns owner, EMV, sale history, homestead, sqft for any
set of addresses OR all parcels within a drawn polygon.

This is the solution to dynamic polygon-based property discovery:
  1. User draws polygon on the Streamlit map
  2. This module converts it to an ArcGIS spatial query
  3. Returns all parcels inside -- scored and ready to display
  4. No shapefile download needed

Source: https://arcgis.metc.state.mn.us/data1/rest/services/parcels/Parcel_Points_2025/
Data: 2025 Anoka County assessor data (owner, EMV, sale, homestead, sqft)
Cost: Free, public, no API key required
"""

import sys, os, json, math
from datetime import datetime, timezone
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import urllib.request, urllib.parse
import pandas as pd

BASE_URL = (
    "https://arcgis.metc.state.mn.us/data1/rest/services/"
    "parcels/Parcel_Points_2025/FeatureServer/0/query"
)

FIELDS = (
    "COUNTY_PIN,ANUMBER,ST_PRE_DIR,ST_NAME,ST_POS_TYP,ST_POS_DIR,"
    "ZIP,CTU_NAME,OWNER_NAME,OWN_ADD_L1,OWN_ADD_L2,OWN_ADD_L3,"
    "HOMESTEAD,EMV_TOTAL,EMV_LAND,EMV_BLDG,SALE_DATE,SALE_VALUE,"
    "YEAR_BUILT,FIN_SQ_FT,DWELL_TYPE,TOTAL_TAX,USECLASS1,OWNERSHIP"
)

# ── Helpers ───────────────────────────────────────────────────────────────────

def _ms_to_year(ms) -> Optional[int]:
    """Convert ArcGIS epoch-ms timestamp to year."""
    if not ms: return None
    try:
        return datetime.fromtimestamp(ms / 1000, tz=timezone.utc).year
    except Exception:
        return None

def _parse_feature(f: dict) -> dict:
    """Normalize one ArcGIS feature into a clean property dict."""
    a    = f.get("attributes", {})
    geom = f.get("geometry", {})

    addr_num = str(a.get("ANUMBER", "") or "").strip()
    st_dir   = (a.get("ST_PRE_DIR","") or "").strip()
    st_name  = (a.get("ST_NAME","") or "").strip().title()
    st_type  = (a.get("ST_POS_TYP","") or "").strip().title()
    st_post  = (a.get("ST_POS_DIR","") or "").strip()
    parts    = [x for x in [addr_num, st_dir, st_name, st_type, st_post] if x]
    address  = " ".join(parts)

    # Owner mailing vs property — key absentee signal
    own_l1 = (a.get("OWN_ADD_L1","") or "").strip()
    own_l2 = (a.get("OWN_ADD_L2","") or "").strip()
    mailing  = f"{own_l1}, {own_l2}".strip(", ")
    absentee = bool(own_l1 and own_l1.upper() not in address.upper()[:20])

    sale_ms  = a.get("SALE_DATE")
    sale_yr  = _ms_to_year(sale_ms)
    years_owned = (2026 - sale_yr) if sale_yr else None

    homestead = (a.get("HOMESTEAD","") or "").strip()
    # Homestead is the definitive indicator — "Y"/"Yes" = owner lives here
    is_homestead = homestead.upper() in ("Y","YES","TRUE","1","HOMESTEAD")
    owner_type   = "Homestead" if is_homestead else "No Homestead"

    # Absentee: non-homestead OR mailing address is in a different city
    own_num  = own_l1.split()[0] if own_l1 else ""
    prop_num = str(a.get("ANUMBER","") or "")
    absentee = not is_homestead  # primary indicator is homestead exemption

    return {
        "pin":              (a.get("COUNTY_PIN","") or "").strip(),
        "address":          address,
        "city":             (a.get("CTU_NAME","") or "Blaine").strip(),
        "zip":              (a.get("ZIP","") or "55449").strip(),
        "owner_name":       (a.get("OWNER_NAME","") or "").strip(),
        "owner_mailing":    mailing,
        "absentee":         absentee,
        "homestead":        homestead,
        "owner_type":       owner_type,
        "emv":              a.get("EMV_TOTAL"),
        "emv_bldg":         a.get("EMV_BLDG"),
        "emv_land":         a.get("EMV_LAND"),
        "prior_sale_price": a.get("SALE_VALUE"),
        "prior_sale_year":  sale_yr,
        "years_owned":      years_owned,
        "year_built":       a.get("YEAR_BUILT"),
        "sqft":             a.get("FIN_SQ_FT"),
        "dwell_type":       a.get("DWELL_TYPE","").strip(),
        "use_class":        a.get("USECLASS1","").strip(),
        "total_tax":        a.get("TOTAL_TAX"),
        "lat":              geom.get("y"),
        "lng":              geom.get("x"),
    }


def _query(params: dict) -> list[dict]:
    """Execute one ArcGIS REST query, handle pagination up to 2000 records."""
    params.setdefault("outFields", FIELDS)
    params.setdefault("outSR", "4326")      # WGS-84 lat/lng
    params.setdefault("returnGeometry", "true")
    params.setdefault("f", "json")
    params.setdefault("resultRecordCount", 2000)

    results = []
    offset  = 0
    while True:
        params["resultOffset"] = offset
        qs  = urllib.parse.urlencode(params)
        req = urllib.request.Request(
            f"{BASE_URL}?{qs}",
            headers={"User-Agent": "REI-Platform/1.0 (real-estate-intel; personal)"},
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read())

        features = data.get("features", [])
        results.extend(features)
        if not data.get("exceededTransferLimit") or len(features) == 0:
            break
        offset += len(features)

    parsed = [_parse_feature(f) for f in results]

    # Deduplicate by address: keep the record with highest EMV (most complete assessment)
    # This collapses condo/townhouse clusters that share the same street address
    seen: dict[str, dict] = {}
    for p in parsed:
        addr_key = p.get("address","").upper().strip()
        if not addr_key:
            continue
        if addr_key not in seen:
            seen[addr_key] = p
        else:
            # Keep the one with higher EMV (better assessed)
            existing_emv = seen[addr_key].get("emv") or 0
            this_emv     = p.get("emv") or 0
            if this_emv > existing_emv:
                seen[addr_key] = p

    return list(seen.values())


# ── Public API ────────────────────────────────────────────────────────────────

def lookup_address(address: str, city: str = "Blaine") -> Optional[dict]:
    """
    Look up a single address. Returns a property dict or None.
    address: '2882 Aspen Lake Dr NE'
    """
    # Parse into number + street
    parts = address.strip().split()
    if not parts or not parts[0].isdigit():
        return None
    num  = int(parts[0])
    rest = " ".join(parts[1:]).upper()
    # Remove trailing directional for street name match
    rest_core = rest.replace(" NE","").replace(" NW","").replace(" SE","").replace(" SW","").strip()

    where = (
        f"CTU_NAME='{city.upper()}' AND "
        f"ANUMBER={num} AND "
        f"UPPER(ST_NAME) LIKE '{rest_core.split()[0]}%'"
    )
    rows = _query({"where": where})
    # Best match: check full address similarity
    for row in rows:
        if str(num) in row["address"] and rest_core.split()[0].title() in row["address"]:
            return row
    return rows[0] if rows else None


def query_polygon(geojson_polygon: dict, city: str = "Blaine") -> pd.DataFrame:
    """
    Return all parcels inside a GeoJSON polygon (as drawn by the Streamlit map).

    geojson_polygon: {'type': 'Polygon', 'coordinates': [[[lng, lat], ...]]}

    Uses ArcGIS spatial query — no shapefile needed.
    Returns a DataFrame sorted by motivation_score DESC.
    """
    from scoring.motivation import PropertyInput, score as compute_score

    # Convert GeoJSON polygon to ArcGIS geometry
    coords = geojson_polygon.get("coordinates", [[]])
    rings  = [[{"x": c[0], "y": c[1]} for c in ring] for ring in coords]
    geom   = json.dumps({"rings": rings, "spatialReference": {"wkid": 4326}})

    params = {
        "geometry":       geom,
        "geometryType":   "esriGeometryPolygon",
        "spatialRel":     "esriSpatialRelContains",
        "inSR":           "4326",
        "where":          "USECLASS1 LIKE '1a%'",   # residential single-unit only
    }

    print(f"[metrogis] Querying parcels in polygon...")
    features = _query(params)
    print(f"[metrogis] Found {len(features)} residential parcels")

    # Score each one
    rows = []
    for prop in features:
        if not prop.get("emv"):
            continue
        pi = PropertyInput(
            address          = prop["address"],
            emv              = prop["emv"],
            prior_sale_price = prop.get("prior_sale_price"),
            prior_sale_year  = prop.get("prior_sale_year"),
            years_owned      = prop.get("years_owned"),
            homestead        = prop.get("homestead",""),
            owner_type       = prop.get("owner_type",""),
            owner_name       = prop.get("owner_name",""),
        )
        r = compute_score(pi)

        rows.append({
            **prop,
            "motivation_score": r.total,
            "knock_tier":       r.tier,
            "primary_signal":   r.primary_signal,
            "score_factors":    json.dumps(r.factors),
            "est_value":        r.est_value,
            "est_equity_usd":   r.equity_usd,
            "equity_pct":       r.equity_pct,
            "monthly_piti":     r.monthly_piti,
        })

    df = pd.DataFrame(rows).sort_values("motivation_score", ascending=False)
    return df


def query_city(city: str = "Blaine", residential_only: bool = True) -> pd.DataFrame:
    """
    Fetch and score ALL parcels in a city. Slow (~30s) but comprehensive.
    Use query_polygon() for interactive polygon-based discovery.
    """
    where = f"CTU_NAME='{city.upper()}'"
    if residential_only:
        where += " AND USECLASS1 LIKE '1a%'"

    from scoring.motivation import PropertyInput, score as compute_score

    print(f"[metrogis] Fetching all {city} residential parcels...")
    features = _query({"where": where})
    print(f"[metrogis] Processing {len(features)} parcels...")

    rows = []
    for prop in features:
        if not prop.get("emv"):
            continue
        pi = PropertyInput(
            address=prop["address"], emv=prop["emv"],
            prior_sale_price=prop.get("prior_sale_price"),
            prior_sale_year=prop.get("prior_sale_year"),
            years_owned=prop.get("years_owned"),
            homestead=prop.get("homestead",""),
            owner_type=prop.get("owner_type",""),
        )
        r = compute_score(pi)
        rows.append({
            **prop,
            "motivation_score": r.total,
            "knock_tier":       r.tier,
            "primary_signal":   r.primary_signal,
            "score_factors":    json.dumps(r.factors),
            "est_value":        r.est_value,
            "est_equity_usd":   r.equity_usd,
            "equity_pct":       r.equity_pct,
        })

    return pd.DataFrame(rows).sort_values("motivation_score", ascending=False)


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        addr = " ".join(sys.argv[1:])
        print(f"Looking up: {addr}")
        result = lookup_address(addr)
        if result:
            for k, v in result.items():
                print(f"  {k}: {v}")
        else:
            print("  Not found.")
    else:
        # Default: show the 3 coming-soon properties
        for addr in ["2882 Aspen Lake Dr NE", "3348 128th Ln NE", "3578 128th Ct NE"]:
            r = lookup_address(addr)
            if r:
                print(f"\n{addr}: owner={r['owner_name']!r}, emv=${r['emv']:,}, "
                      f"homestead={r['homestead']}, sale_yr={r['prior_sale_year']}, "
                      f"absentee={r['absentee']}")
