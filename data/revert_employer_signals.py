"""Revert employer signal bonuses -- Google News too diffuse for individual property scoring."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db

con = get_db()

# Remove bonuses from property_scores
props = con.execute("""
    SELECT id, motivation_score, knock_tier, score_factors
    FROM property_scores
    WHERE score_factors LIKE '%employer_layoff%'
       OR score_factors LIKE '%employer_closure%'
""").fetchall()

reverted = 0
for prop_id, score, tier, factors_raw in props:
    try:
        factors = json.loads(factors_raw or "{}")
        bonus = factors.pop("employer_layoff_large", 0) + factors.pop("employer_closure_small", 0)
        if bonus == 0:
            continue
        new_score = max(score - bonus, 0)
        new_tier = "T1" if new_score >= 40 else "T2" if new_score >= 20 else "T3"
        con.execute("""
            UPDATE property_scores SET motivation_score=?, knock_tier=?,
            score_factors=?, updated_at=current_timestamp WHERE id=?
        """, [new_score, new_tier, json.dumps(factors), prop_id])
        reverted += 1
    except Exception:
        pass

# Remove from signals table
try:
    con.execute(
        "DELETE FROM property_signals_v2 "
        "WHERE signal_name IN ('employer_layoff_large','employer_closure_small')"
    )
except Exception:
    pass

# Restore MCRO-confirmed T1s
OVERRIDES = {
    "3316_117TH_LN_NE":   (45, "T1"),
    "11725_NAPLES_CIR_NE": (45, "T1"),
    "3448_117TH_LN_NE":   (35, "T1"),
    "11715_NAPLES_CIR_NE": (5, "SKIP"),
}
for pid, (sc, ti) in OVERRIDES.items():
    con.execute(
        "UPDATE property_scores SET motivation_score=?, knock_tier=?, "
        "updated_at=current_timestamp WHERE id=?",
        [sc, ti, pid]
    )

con.close()
print(f"Reverted {reverted} employer signal bonuses from property scores")
print("MCRO-confirmed T1s restored")
print()
print("Google News = area context only (informational, not in scoring)")
print("The signal architecture is now:")
print("  Individual-level (high precision):")
print("    - MCRO divorce/probate    -> property score")
print("    - LinkedIn job change     -> property score")
print("    - Estate sale at address  -> property score")
print("    - Obituary name match     -> property score")
print("    - Bankruptcy filing       -> property score")
print("    - Facebook exact address  -> property score")
print("  Area-level (informational only):")
print("    - Google News layoffs     -> dashboard banner, NOT in scores")
print("    - Zillow listing monitor  -> SKIP flag only")
