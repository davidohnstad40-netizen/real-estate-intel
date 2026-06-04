"""Restore MCRO-confirmed T1s and active listing SKIP after MetroGIS refresh."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db

con = get_db()

OVERRIDES = {
    "3316_117TH_LN_NE": (
        45, "T1",
        "Post-purchase DIVORCE on record -- strongest motivation signal",
        {"divorce_confirmed": 40, "long_hold_12plus": 5}
    ),
    "11725_NAPLES_CIR_NE": (
        45, "T1",
        "Post-purchase DIVORCE on record -- strongest motivation signal",
        {"divorce_confirmed": 40, "long_hold_12plus": 5}
    ),
    "3448_117TH_LN_NE": (
        35, "T1",
        "No homestead -- absentee/moved | Owner age 79 -- estate/care signal",
        {"no_homestead": 20, "owner_elderly": 15}
    ),
    "11715_NAPLES_CIR_NE": (
        5, "SKIP",
        "Active MLS listing $999,999 -- not an off-market target",
        {"skip": 5}
    ),
}

for prop_id, (score, tier, signal, factors) in OVERRIDES.items():
    con.execute(
        "UPDATE property_scores SET motivation_score=?, knock_tier=?, "
        "primary_signal=?, score_factors=?, updated_at=current_timestamp WHERE id=?",
        [score, tier, signal, json.dumps(factors), prop_id]
    )
    print(f"Restored: {prop_id} -> {tier} (score={score})")

# Print final T1 list
print("\nAll T1 properties:")
rows = con.execute(
    "SELECT p.address, ps.motivation_score, ps.knock_tier, ps.primary_signal "
    "FROM property_scores ps JOIN properties p ON p.id=ps.id "
    "WHERE ps.knock_tier='T1' ORDER BY ps.motivation_score DESC"
).fetchall()
for r in rows:
    print(f"  [{r[2]}] {r[0]}: score={r[1]} | {(r[3] or '')[:55]}")

con.close()
print("Done.")
