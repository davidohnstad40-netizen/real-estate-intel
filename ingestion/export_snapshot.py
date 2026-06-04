"""
export_snapshot.py -- Export DuckDB state to Parquet files for cloud deployment.

Usage:
    python -m ingestion.export_snapshot          # uses default paths
    python -m ingestion.export_snapshot --db path/to/rei.duckdb --out data/snapshot
"""
import sys, os, json, argparse
from datetime import datetime, timezone

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def export_to_parquet(db_path=None, output_dir=None) -> dict:
    """
    Read from DuckDB (read_only) and export tables to Parquet.

    Returns a dict with:
        {
            "files": {"properties": <path>, "scores": <path>, ...},
            "row_counts": {"properties": N, "scores": N, ...},
            "meta_path": <path to snapshot_meta.json>,
            "generated_at": <ISO timestamp>,
        }
    """
    import duckdb
    import pandas as pd

    # -- resolve paths ---------------------------------------------------------
    if db_path is None:
        db_path = os.getenv("DB_PATH", "./data/rei.duckdb")
    db_path = os.path.abspath(db_path)

    if output_dir is None:
        output_dir = os.path.join(os.path.dirname(db_path), "snapshot")
    output_dir = os.path.abspath(output_dir)

    os.makedirs(output_dir, exist_ok=True)

    if not os.path.exists(db_path):
        raise FileNotFoundError(f"DuckDB file not found: {db_path}")

    print(f"Connecting to {db_path} (read_only) ...")
    con = duckdb.connect(db_path, read_only=True)

    results = {
        "files": {},
        "row_counts": {},
        "meta_path": None,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    # -- helper ----------------------------------------------------------------
    def export_table(table_name, file_name, query=None):
        """Export a single table (or query) to Parquet. Skips gracefully if table absent."""
        out_path = os.path.join(output_dir, file_name)
        try:
            sql = query or f"SELECT * FROM {table_name}"
            df = con.execute(sql).df()
            df.to_parquet(out_path, index=False, engine="pyarrow")
            results["files"][table_name] = out_path
            results["row_counts"][table_name] = len(df)
            print(f"  {table_name}: {len(df):,} rows -> {file_name}")
            return df
        except Exception as exc:
            msg = str(exc)
            if "does not exist" in msg or "Table" in msg or "Catalog Error" in msg:
                print(f"  {table_name}: table not found, skipping.")
            else:
                print(f"  {table_name}: ERROR - {exc}")
            return None

    # -- export core tables ----------------------------------------------------
    # properties + scores joined so cloud app only needs two files
    print("Exporting tables ...")

    df_props = export_table("properties", "properties.parquet")

    # Scores -- join primary_signal & score_factors onto properties query too
    df_scores = export_table(
        "property_scores",
        "scores.parquet",
        query="""
            SELECT ps.*
            FROM property_scores ps
        """,
    )

    # Optional tables -- silently skip if they don't exist yet
    export_table("contact_info",   "contacts.parquet")
    export_table("future_sellers", "future_sellers.parquet")

    # -- compute metadata counts -----------------------------------------------
    t1_count = 0
    t2_count = 0
    property_count = 0

    if df_props is not None:
        property_count = len(df_props)

    if df_scores is not None:
        try:
            t1_count = int((df_scores["knock_tier"] == "T1").sum())
            t2_count = int((df_scores["knock_tier"] == "T2").sum())
        except Exception:
            pass

    # -- write metadata JSON ---------------------------------------------------
    meta = {
        "generated_at": results["generated_at"],
        "property_count": property_count,
        "t1_count": t1_count,
        "t2_count": t2_count,
        "files": {k: os.path.basename(v) for k, v in results["files"].items()},
        "row_counts": results["row_counts"],
    }
    meta_path = os.path.join(output_dir, "snapshot_meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    results["meta_path"] = meta_path
    results["property_count"] = property_count
    results["t1_count"] = t1_count
    results["t2_count"] = t2_count

    con.close()
    return results


# ------------------------------------------------------------------------------
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Export DuckDB to Parquet snapshot")
    parser.add_argument("--db",  default=None, help="Path to rei.duckdb")
    parser.add_argument("--out", default=None, help="Output directory (default: data/snapshot)")
    args = parser.parse_args()

    try:
        res = export_to_parquet(db_path=args.db, output_dir=args.out)
    except FileNotFoundError as e:
        print(f"ERROR: {e}")
        sys.exit(1)

    print()
    print("=== Snapshot complete ===")
    print(f"  Generated at : {res['generated_at']}")
    print(f"  Properties   : {res['property_count']:,}")
    print(f"  T1 targets   : {res['t1_count']}")
    print(f"  T2 targets   : {res['t2_count']}")
    print(f"  Metadata     : {res['meta_path']}")
    print()
    print("Files written:")
    for tbl, path in res["files"].items():
        rows = res["row_counts"].get(tbl, 0)
        print(f"  {path}  ({rows:,} rows)")
