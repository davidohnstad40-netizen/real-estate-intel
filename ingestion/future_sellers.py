"""
Future Seller Watchlist
========================
Identifies buyers from 2022-2025 who are already underwater or near-negative equity.
These are your NEXT wave of T1/T2 sellers -- in 12-24 months they'll need to sell.

Logic: bought during/after rate peak → high mortgage rate + EMV hasn't caught up
      → every month they carry this is painful → eventual forced/motivated sale

Data source: MetroGIS 2025 parcel data (SALE_DATE + SALE_VALUE + EMV)
"""

import sys, os, json
from datetime import datetime, timezone
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import urllib.request, urllib.parse
import pandas as pd
from db.schema import get_db
from scoring.motivation import PropertyInput, score

BASE_URL = (
    "https://arcgis.metc.state.mn.us/data1/rest/services/"
    "parcels/Parcel_Points_2025/FeatureServer/0/query"
)

RATE_MAP = {
    2022: 0.0506, 2023: 0.0694, 2024: 0.0676, 2025: 0.0665
}

def _rem_balance(principal, rate, years_paid):
    r = rate / 12; n = 360; k = min(int(years_paid * 12), n - 1)
    if r == 0: return principal * (1 - k / n)
    return principal * ((1+r)**n - (1+r)**k) / ((1+r)**n - 1)

def find_underwater_buyers(city: str = "Blaine",
                            min_purchase: int = 400_000,
                            sale_year_start: int = 2022,
                            sale_year_end: int = 2025) -> pd.DataFrame:
    """
    Query MetroGIS for all recent buyers in a city and flag those
    who are underwater or near-negative equity.
    """
    # ArcGIS REST API supports SQL date literals in WHERE clause
    start_str = f"{sale_year_start}-01-01"
    end_str   = f"{sale_year_end}-12-31"

    where = (
        f"CTU_NAME='{city.upper()}' AND "
        f"USECLASS1 LIKE '1a%' AND "
        f"SALE_VALUE >= {min_purchase} AND "
        f"SALE_DATE >= DATE '{start_str}' AND SALE_DATE <= DATE '{end_str}' AND "
        f"EMV_TOTAL > 0"
    )
    fields = (
        "COUNTY_PIN,ANUMBER,ST_NAME,ST_POS_TYP,ST_POS_DIR,ZIP,"
        "CTU_NAME,OWNER_NAME,HOMESTEAD,"
        "EMV_TOTAL,SALE_DATE,SALE_VALUE,FIN_SQ_FT,YEAR_BUILT"
    )
    params = {
        "where":               where,
        "outFields":           fields,
        "outSR":               "4326",
        "returnGeometry":      "true",
        "f":                   "json",
        "resultRecordCount":   2000,
        "orderByFields":       "SALE_DATE DESC",
    }

    qs  = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{BASE_URL}?{qs}",
        headers={"User-Agent": "REI-Platform/1.0"},
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.loads(r.read())

    rows = []
    for f in data.get("features", []):
        a    = f["attributes"]
        geom = f.get("geometry", {})

        sale_ms  = a.get("SALE_DATE")
        sale_yr  = datetime.fromtimestamp(sale_ms/1000, tz=timezone.utc).year if sale_ms else None
        years_held = (2026 - sale_yr) if sale_yr else None

        num  = str(a.get("ANUMBER",""))
        st   = (a.get("ST_NAME","") or "").strip().title()
        typ  = (a.get("ST_POS_TYP","") or "").strip().title()
        dire = (a.get("ST_POS_DIR","") or "").strip()
        addr = " ".join(x for x in [num, st, typ, dire] if x)

        emv        = a.get("EMV_TOTAL") or 0
        sale_price = a.get("SALE_VALUE") or 0
        homestead  = (a.get("HOMESTEAD","") or "").strip()
        is_hmst    = homestead.upper() in ("YES","Y","TRUE","1")

        # Estimate remaining mortgage
        rate      = RATE_MAP.get(sale_yr, 0.065)
        principal = sale_price * 0.80
        bal       = _rem_balance(principal, rate, years_held or 1)

        est_value  = emv * 1.08
        equity_d   = est_value - bal
        equity_pct = equity_d / est_value if est_value > 0 else None

        # Monthly payment estimate
        r_mo = rate / 12
        n    = 360
        mo_pi = principal * r_mo * (1+r_mo)**n / ((1+r_mo)**n - 1) if r_mo > 0 else principal/n
        mo_tax = emv * (0.012 if is_hmst else 0.018) / 12
        mo_ins = sale_price * 0.005 / 12
        piti   = mo_pi + mo_tax + mo_ins

        # Underwater severity
        if equity_pct is None:
            severity = "unknown"
        elif equity_pct < -0.05:
            severity = "deeply_underwater"   # > 5% negative
        elif equity_pct < 0:
            severity = "underwater"
        elif equity_pct < 0.10:
            severity = "thin_equity"
        elif equity_pct < 0.20:
            severity = "moderate_equity"
        else:
            severity = "positive_equity"     # likely not a near-term motivated seller

        # Estimated time to motivated sale (rough heuristic)
        if severity in ("deeply_underwater", "underwater"):
            timeline = "6-18 months"
        elif severity == "thin_equity":
            timeline = "12-24 months"
        else:
            timeline = "24-36 months"

        rows.append({
            "pin":          (a.get("COUNTY_PIN","") or "").strip(),
            "address":      addr,
            "city":         city,
            "zip":          (a.get("ZIP","") or "55449").strip(),
            "lat":          geom.get("y"),
            "lng":          geom.get("x"),
            "owner":        (a.get("OWNER_NAME","") or "").strip(),
            "homestead":    homestead,
            "absentee":     not is_hmst,
            "sale_year":    sale_yr,
            "sale_price":   sale_price,
            "emv":          emv,
            "est_value":    round(est_value),
            "est_mortgage": round(bal),
            "equity_usd":   round(equity_d),
            "equity_pct":   round(equity_pct * 100, 1) if equity_pct else None,
            "severity":     severity,
            "est_piti":     round(piti),
            "sqft":         a.get("FIN_SQ_FT"),
            "timeline":     timeline,
        })

    if not rows:
        print("[future_sellers] No features returned from MetroGIS. Check date filter syntax.")
        return pd.DataFrame()

    df = pd.DataFrame(rows)
    if "severity" not in df.columns:
        return pd.DataFrame()
    df = df[df["severity"].isin(["deeply_underwater","underwater","thin_equity"])].copy()
    df = df.sort_values(["severity","sale_year"], ascending=[True, False])
    return df


def load_to_watchlist(df: pd.DataFrame, db_path: str = None) -> int:
    """Store future seller watchlist in DuckDB."""
    con = get_db(db_path)
    con.execute("""
        CREATE TABLE IF NOT EXISTS future_sellers (
            pin            VARCHAR PRIMARY KEY,
            address        VARCHAR,
            city           VARCHAR,
            zip            VARCHAR,
            lat            DOUBLE,
            lng            DOUBLE,
            owner          VARCHAR,
            homestead      VARCHAR,
            absentee       BOOLEAN,
            sale_year      INTEGER,
            sale_price     DOUBLE,
            emv            DOUBLE,
            est_value      DOUBLE,
            est_mortgage   DOUBLE,
            equity_usd     DOUBLE,
            equity_pct     DOUBLE,
            severity       VARCHAR,
            est_piti       DOUBLE,
            sqft           INTEGER,
            timeline       VARCHAR,
            check_after    DATE,
            notes          TEXT,
            added_at       TIMESTAMP DEFAULT current_timestamp
        )
    """)

    n = 0
    for _, row in df.iterrows():
        # Set check_after date based on timeline
        from datetime import date, timedelta
        check_months = 12 if "6-18" in (row.get("timeline","")) else 18
        check_dt = date.today() + timedelta(days=check_months*30)

        try:
            con.execute("""
                INSERT OR IGNORE INTO future_sellers
                (pin, address, city, zip, lat, lng, owner, homestead, absentee,
                 sale_year, sale_price, emv, est_value, est_mortgage, equity_usd,
                 equity_pct, severity, est_piti, sqft, timeline, check_after)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """, [
                row["pin"], row["address"], row["city"], row["zip"],
                row["lat"], row["lng"], row["owner"],
                row["homestead"], bool(row["absentee"]),
                int(row["sale_year"]) if row.get("sale_year") else None,
                row["sale_price"], row["emv"], row["est_value"],
                row["est_mortgage"], row["equity_usd"],
                float(row["equity_pct"]) if row.get("equity_pct") else None,
                row["severity"], row["est_piti"],
                int(row["sqft"]) if row.get("sqft") else None,
                row["timeline"], check_dt,
            ])
            n += 1
        except Exception as e:
            pass

    con.close()
    return n


if __name__ == "__main__":
    print("Finding underwater/thin-equity buyers in Blaine (2022-2025)...")
    df = find_underwater_buyers()
    print(f"\nFound {len(df)} at-risk buyers:")
    print()

    for sev in ["deeply_underwater", "underwater", "thin_equity"]:
        subset = df[df.severity == sev]
        if subset.empty:
            continue
        label = {"deeply_underwater":"DEEPLY UNDERWATER",
                 "underwater":"UNDERWATER",
                 "thin_equity":"THIN EQUITY"}.get(sev, sev)
        print(f"\n-- {label} ({len(subset)}) --")
        for _, r in subset.iterrows():
            eq = f"{r['equity_pct']:+.1f}%" if r.get("equity_pct") else "?"
            print(f"  {r['address']}: paid ${r['sale_price']:,.0f} in {r['sale_year']} "
                  f"| EMV ${r['emv']:,.0f} | equity {eq} | PITI ~${r['est_piti']:,.0f}/mo "
                  f"| timeline: {r['timeline']}")

    n = load_to_watchlist(df)
    print(f"\nLoaded {n} new entries to future_sellers watchlist.")
