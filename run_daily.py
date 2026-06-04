"""
run_daily.py -- Project root

Daily automation script for the real-estate-intel platform.

What it does:
  1. Runs today's score snapshot (writes to score_history, detects tier upgrades)
  2. Prints a T1/T2/T3/TBD count + average score summary
  3. Writes a plain-text daily summary to data/daily_summary_YYYY-MM-DD.txt

Usage:
    python run_daily.py
"""
import sys
import os
from datetime import date

# ---------------------------------------------------------------------------
# Path setup -- ensure project root is importable as a package root
# ---------------------------------------------------------------------------
_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from ingestion.snapshot import run_snapshot
from db.schema import get_db


def build_summary(con, upgrades, today: date) -> str:
    """Build a multi-line summary string for today's run."""
    lines = []
    lines.append("=" * 60)
    lines.append(f"REAL-ESTATE-INTEL -- Daily Summary  [{today}]")
    lines.append("=" * 60)

    # ------------------------------------------------------------------ #
    # Tier counts and average score from property_scores                  #
    # ------------------------------------------------------------------ #
    tier_rows = con.execute("""
        SELECT
            knock_tier,
            COUNT(*)           AS cnt,
            AVG(motivation_score) AS avg_score
        FROM property_scores
        GROUP BY knock_tier
        ORDER BY knock_tier
    """).fetchall()

    lines.append("\nTier Breakdown (current scores):")
    lines.append(f"  {'Tier':<8} {'Count':>6}  {'Avg Score':>10}")
    lines.append(f"  {'-'*8} {'-'*6}  {'-'*10}")

    total_props  = 0
    total_score_sum = 0.0
    tier_counts  = {}

    for tier, cnt, avg in tier_rows:
        tier_counts[tier] = cnt
        total_props  += cnt
        total_score_sum += (avg or 0) * cnt
        lines.append(f"  {tier:<8} {cnt:>6}  {(avg or 0):>10.1f}")

    if total_props > 0:
        overall_avg = total_score_sum / total_props
    else:
        overall_avg = 0.0

    lines.append(f"\n  Total properties : {total_props}")
    lines.append(f"  Overall avg score: {overall_avg:.1f}")

    # ------------------------------------------------------------------ #
    # Top 5 highest-scoring properties                                    #
    # ------------------------------------------------------------------ #
    top5 = con.execute("""
        SELECT p.address, ps.knock_tier, ps.motivation_score, ps.primary_signal
        FROM property_scores ps
        JOIN properties p ON p.id = ps.id
        ORDER BY ps.motivation_score DESC
        LIMIT 5
    """).fetchall()

    lines.append("\nTop 5 Properties by Score:")
    for rank, (addr, tier, score, signal) in enumerate(top5, start=1):
        lines.append(f"  {rank}. [{tier}] {addr}  score={score}  signal={signal or 'n/a'}")

    # ------------------------------------------------------------------ #
    # Tier upgrades                                                        #
    # ------------------------------------------------------------------ #
    lines.append(f"\nTier Upgrades Detected Today: {len(upgrades)}")
    if upgrades:
        for u in upgrades:
            lines.append(
                f"  *** {u['address']}:  {u['old_tier']} -> {u['new_tier']}  (score={u['score']})"
            )
    else:
        lines.append("  (none)")

    lines.append("\n" + "=" * 60)
    lines.append("End of daily summary")
    lines.append("=" * 60)

    return "\n".join(lines)


def main():
    today = date.today()
    print(f"[run_daily] Starting daily run for {today} ...\n")

    # ------------------------------------------------------------------ #
    # 0. Weekly tasks (run on Mondays only)                               #
    # ------------------------------------------------------------------ #
    if today.weekday() == 0:  # Monday
        print("[run_daily] Monday -- running weekly tasks...")

        # Refresh all tracked properties from MetroGIS (catches homestead changes,
        # EMV updates, and any properties that sold since last check)
        try:
            from ingestion.refresh_from_metrogis import main as refresh_main
            print("[run_daily] MetroGIS refresh starting...")
            refresh_main()
        except Exception as e:
            print(f"[run_daily] MetroGIS refresh error: {e}")

        # Update future seller watchlist (finds new underwater buyers)
        try:
            from ingestion.future_sellers import find_underwater_buyers, load_to_watchlist
            print("[run_daily] Scanning for new underwater buyers...")
            df_watch = find_underwater_buyers()
            n = load_to_watchlist(df_watch)
            print(f"[run_daily] Future sellers: {len(df_watch)} at-risk, {n} new entries added")
        except Exception as e:
            print(f"[run_daily] Future seller scan error: {e}")

        print("[run_daily] Weekly tasks complete.\n")

    # ------------------------------------------------------------------ #
    # 1. Run snapshot (also inserts into score_history)                   #
    # ------------------------------------------------------------------ #
    upgrades = run_snapshot()

    if upgrades:
        print(f"\n*** {len(upgrades)} TIER UPGRADE(S) DETECTED ***")
        for u in upgrades:
            print(f"  {u['address']}: {u['old_tier']} -> {u['new_tier']}  (score={u['score']})")
    else:
        print("[run_daily] No tier upgrades today.")

    # ------------------------------------------------------------------ #
    # 2. Connect again for summary queries                                 #
    # ------------------------------------------------------------------ #
    con = get_db()

    summary_text = build_summary(con, upgrades, today)
    print("\n" + summary_text)

    con.close()

    # ------------------------------------------------------------------ #
    # 3. Write daily summary to file                                      #
    # ------------------------------------------------------------------ #
    data_dir = os.path.join(_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    summary_path = os.path.join(data_dir, f"daily_summary_{today}.txt")

    with open(summary_path, "w", encoding="utf-8") as fh:
        fh.write(summary_text + "\n")

    print(f"\n[run_daily] Summary written -> {summary_path}")

    # ------------------------------------------------------------------ #
    # 4. Send email alert                                                  #
    # ------------------------------------------------------------------ #
    try:
        from agents.alerts import send_alert
        con2 = get_db()
        tier_rows = con2.execute("""
            SELECT knock_tier, COUNT(*), AVG(motivation_score)
            FROM property_scores GROUP BY knock_tier
        """).fetchall()
        top5 = con2.execute("""
            SELECT p.address, ps.knock_tier, ps.motivation_score, ps.primary_signal
            FROM property_scores ps JOIN properties p ON p.id = ps.id
            ORDER BY ps.motivation_score DESC LIMIT 5
        """).fetchall()
        con2.close()

        tier_stats = {t: c for t, c, _ in tier_rows}
        total = sum(c for _, c, _ in tier_rows)
        tier_stats["avg"] = (
            sum((a or 0) * c for _, c, a in tier_rows) / total if total else 0.0
        )
        send_alert(upgrades, tier_stats, top5)
    except Exception as e:
        print(f"[run_daily] Email alert error: {e}")

    # ------------------------------------------------------------------ #
    # 5. Export snapshot for cloud deployment                              #
    # ------------------------------------------------------------------ #
    try:
        from ingestion.export_snapshot import export_to_parquet
        export_to_parquet()

        # Auto-commit to GitHub so cloud app stays fresh
        import subprocess
        result = subprocess.run(
            ["git", "add", "data/snapshot/"],
            cwd=_ROOT, capture_output=True, text=True
        )
        commit_result = subprocess.run(
            ["git", "commit", "-m", f"snapshot: auto-update {today}"],
            cwd=_ROOT, capture_output=True, text=True
        )
        if "nothing to commit" not in commit_result.stdout:
            subprocess.run(["git", "push", "origin", "master"],
                          cwd=_ROOT, capture_output=True)
            print(f"[run_daily] Snapshot committed and pushed to GitHub")
        else:
            print(f"[run_daily] Snapshot unchanged, no commit needed")
    except Exception as e:
        print(f"[run_daily] Snapshot export error: {e}")

    print("[run_daily] Done.")


if __name__ == "__main__":
    main()
