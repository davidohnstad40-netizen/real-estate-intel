"""
ingestion/snapshot.py

Daily score snapshot + tier upgrade detection.

- Reads all current scores from property_scores joined with properties (for address)
- Inserts into score_history if not already snapshotted today (INSERT OR IGNORE)
- Compares today vs yesterday to find tier upgrades
- Returns list of upgrade dicts: {address, old_tier, new_tier, score}
"""
import sys
import os
from datetime import date, timedelta
from typing import List, Dict, Any

# Ensure project root is on the path so `db` is importable when run directly
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.schema import get_db

TIER_RANK = {"TBD": 0, "T3": 1, "T2": 2, "T1": 3}


def run_snapshot(db_path: str = None) -> List[Dict[str, Any]]:
    """
    Snapshot today's scores and return a list of tier-upgrade dicts.

    Each upgrade dict has keys: address, old_tier, new_tier, score
    """
    con = get_db(db_path)
    today = date.today()
    yesterday = today - timedelta(days=1)

    # ------------------------------------------------------------------ #
    # 1. Pull current scores with address                                  #
    # ------------------------------------------------------------------ #
    rows = con.execute("""
        SELECT
            ps.id,
            p.address,
            ps.motivation_score,
            ps.knock_tier,
            ps.primary_signal,
            ps.score_factors
        FROM property_scores ps
        JOIN properties p ON p.id = ps.id
    """).fetchall()

    if not rows:
        print("[snapshot] No properties found -- nothing to snapshot.")
        return []

    # ------------------------------------------------------------------ #
    # 2. Insert today's snapshot rows (skip duplicates via PRIMARY KEY)    #
    # ------------------------------------------------------------------ #
    # DuckDB doesn't support INSERT OR IGNORE syntax; use INSERT … SELECT
    # … WHERE NOT EXISTS instead.
    inserted = 0
    for row in rows:
        prop_id, address, score, tier, signal, factors = row
        existing = con.execute(
            "SELECT 1 FROM score_history WHERE id = ? AND snapshot_date = ?",
            [prop_id, today]
        ).fetchone()
        if existing is None:
            con.execute(
                """
                INSERT INTO score_history
                    (id, snapshot_date, motivation_score, knock_tier, primary_signal, score_factors)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                [prop_id, today, score, tier, signal, factors]
            )
            inserted += 1

    print(f"[snapshot] Inserted {inserted} new snapshot rows for {today}.")

    # ------------------------------------------------------------------ #
    # 3. Detect tier upgrades vs yesterday                                 #
    # ------------------------------------------------------------------ #
    upgrades: List[Dict[str, Any]] = []

    # Fetch yesterday's snapshot for comparison
    yesterday_rows = {
        r[0]: r[1]  # id -> knock_tier
        for r in con.execute(
            "SELECT id, knock_tier FROM score_history WHERE snapshot_date = ?",
            [yesterday]
        ).fetchall()
    }

    if not yesterday_rows:
        print(f"[snapshot] No yesterday snapshot found ({yesterday}); skipping upgrade detection.")
    else:
        today_map = {r[0]: (r[1], r[2], r[3]) for r in rows}  # id -> (address, score, tier)
        for prop_id, (address, score, new_tier) in today_map.items():
            old_tier = yesterday_rows.get(prop_id)
            if old_tier is None:
                continue  # brand-new property; no comparison available
            old_rank = TIER_RANK.get(old_tier, 0)
            new_rank = TIER_RANK.get(new_tier, 0)
            if new_rank > old_rank:
                upgrades.append({
                    "address": address,
                    "old_tier": old_tier,
                    "new_tier": new_tier,
                    "score": score,
                })

    con.close()
    return upgrades


# -------------------------------------------------------------------------- #
# CLI entry point                                                             #
# -------------------------------------------------------------------------- #
if __name__ == "__main__":
    print("=" * 60)
    print("REAL-ESTATE-INTEL -- Daily Snapshot")
    print("=" * 60)

    upgrades = run_snapshot()

    if upgrades:
        print(f"\n*** {len(upgrades)} TIER UPGRADE(S) DETECTED ***")
        for u in upgrades:
            print(f"  {u['address']}: {u['old_tier']} -> {u['new_tier']}  (score={u['score']})")
    else:
        print("\nNo tier upgrades detected today.")
    print("\nDone.")
