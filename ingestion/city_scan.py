"""
Full City Scan
==============
Queries MetroGIS for every residential parcel in a city, scores them all,
and loads T1/T2/T3 candidates into DuckDB.

This is what powers the 'draw a polygon anywhere in the city and see
all scored properties' feature -- run this once to pre-populate, then
the polygon query filters live from DuckDB.

Usage:
    python -m ingestion.city_scan                    # scan Blaine (default)
    python -m ingestion.city_scan --city "Ham Lake"  # scan another city
    python -m ingestion.city_scan --tier T1 T2       # only load T1/T2
"""

import sys, os, json, argparse, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from ingestion.metrogis import query_city
from db.schema import get_db
import pandas as pd

def load_scan_results(df: pd.DataFrame, db_path: str = None,
                       tiers: list = None, overwrite: bool = False) -> int:
    """Load scored city scan results into DuckDB properties + property_scores tables."""
    tiers = tiers or ["T1","T2","T3"]
    df_load = df[df["knock_tier"].isin(tiers)].copy()

    con = get_db(db_path)

    # Add scan_source column to properties if missing
    try:
        con.execute("ALTER TABLE properties ADD COLUMN scan_source VARCHAR DEFAULT 'manual'")
    except Exception:
        pass

    n = 0
    for _, row in df_load.iterrows():
        prop_id = re_clean(str(row.get("pin","") or row.get("address","")).upper())
        if not prop_id:
            continue

        addr = row.get("address","")

        # Skip if already in DB (from our 52 manually curated properties)
        exists = con.execute(
            "SELECT 1 FROM properties WHERE id = ? LIMIT 1", [prop_id]
        ).fetchone()
        if exists and not overwrite:
            continue

        try:
            con.execute("""
                INSERT OR IGNORE INTO properties
                (id, address, city, zip, lat, lng, owner_name, emv, est_value,
                 prior_sale_price, prior_sale_year, years_owned, homestead, owner_type,
                 anoka_pin, sqft, year_built, absentee_flag, scan_source, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)
            """, [
                prop_id, addr,
                row.get("city","Blaine"), row.get("zip","55449"),
                row.get("lat"), row.get("lng"),
                row.get("owner_name",""),
                row.get("emv"), row.get("est_value"),
                row.get("prior_sale_price"), row.get("prior_sale_year"),
                row.get("years_owned"),
                row.get("homestead",""), row.get("owner_type",""),
                row.get("pin",""),
                int(row["sqft"]) if row.get("sqft") and not pd.isna(row["sqft"]) else None,
                int(row["year_built"]) if row.get("year_built") and not pd.isna(row["year_built"]) else None,
                bool(row.get("absentee", False)),
                "metrogis_scan",
            ])

            con.execute("""
                INSERT OR REPLACE INTO property_scores
                (id, motivation_score, knock_tier, primary_signal, score_factors,
                 est_equity_usd, equity_pct, monthly_piti, updated_at)
                VALUES (?,?,?,?,?,?,?,?,current_timestamp)
            """, [
                prop_id,
                int(row.get("motivation_score",0)),
                row.get("knock_tier","T3"),
                row.get("primary_signal",""),
                row.get("score_factors","{}"),
                row.get("est_equity_usd"),
                row.get("equity_pct"),
                row.get("monthly_piti"),
            ])
            n += 1
        except Exception as e:
            pass

    con.close()
    return n


def re_clean(s: str) -> str:
    import re
    return re.sub(r"[^A-Z0-9]", "_", s)[:50]


def run_scan(city: str = "Blaine", tiers: list = None,
              db_path: str = None, verbose: bool = True) -> dict:
    """Full pipeline: MetroGIS → score → load → return summary."""
    tiers = tiers or ["T1","T2","T3"]
    t0 = time.time()

    if verbose:
        print(f"Scanning {city} -- querying MetroGIS 2025 parcel data...")

    df = query_city(city=city, residential_only=True)

    if df.empty:
        print("No results returned from MetroGIS.")
        return {}

    elapsed = time.time() - t0
    if verbose:
        print(f"Scored {len(df)} parcels in {elapsed:.0f}s")

    tier_counts = df.groupby("knock_tier").size().to_dict()
    if verbose:
        print("\nTier breakdown:")
        for tier in ["T1","T2","T3","SKIP"]:
            cnt = tier_counts.get(tier,0)
            if cnt:
                print(f"  {tier}: {cnt}")

        # Show top T1/T2
        top = df[df.knock_tier.isin(["T1","T2"])].head(20)
        if not top.empty:
            print(f"\nTop T1/T2 discoveries (showing {len(top)}):")
            for _, r in top.iterrows():
                eq = f"{r['equity_pct']:.0%}" if r.get("equity_pct") and not pd.isna(r.equity_pct) else "?"
                print(f"  [{r.knock_tier}] {r.address} | score={r.motivation_score} | "
                      f"signal={str(r.primary_signal or '')[:55]} | equity={eq}")

    n_loaded = load_scan_results(df, db_path=db_path, tiers=tiers)
    if verbose:
        print(f"\nLoaded {n_loaded} new properties to DuckDB.")

    return {
        "city":        city,
        "total":       len(df),
        "tier_counts": tier_counts,
        "t1_count":    tier_counts.get("T1",0),
        "t2_count":    tier_counts.get("T2",0),
        "loaded":      n_loaded,
        "elapsed_s":   round(time.time() - t0),
    }


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--city", default="Blaine")
    parser.add_argument("--tier", nargs="+", default=["T1","T2","T3"])
    parser.add_argument("--overwrite", action="store_true")
    args = parser.parse_args()

    result = run_scan(city=args.city, tiers=args.tier, verbose=True)
    print(f"\nScan complete: {result.get('t1_count',0)} T1, "
          f"{result.get('t2_count',0)} T2 in {args.city}.")
