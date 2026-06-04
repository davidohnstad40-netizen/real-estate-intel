import sys, os, json
sys.path.insert(0, '.')
from db.schema import get_db
con = get_db()
OVERRIDES = {
    "3316_117TH_LN_NE":   (45,"T1","Post-purchase DIVORCE on record -- strongest motivation signal",{"divorce_confirmed":40,"long_hold_12plus":5}),
    "11725_NAPLES_CIR_NE":(45,"T1","Post-purchase DIVORCE on record -- strongest motivation signal",{"divorce_confirmed":40,"long_hold_12plus":5}),
    "3448_117TH_LN_NE":   (35,"T1","No homestead -- absentee/moved | Owner age 79 -- estate/care signal",{"no_homestead":20,"owner_elderly":15}),
    "11715_NAPLES_CIR_NE":(0,"LISTED","Active MLS listing $999,999 (MLS #7009590) -- monitor for expiry or price cut",{"on_mls":0}),
    "11757_NAPLES_CIR_NE":(45,"T1","Post-purchase DIVORCE on record (2023 + 2018, Anoka County)",{"divorce_confirmed":40,"long_hold_12plus":5}),
}
for pid,(sc,ti,sig,fac) in OVERRIDES.items():
    con.execute("UPDATE property_scores SET motivation_score=?,knock_tier=?,primary_signal=?,score_factors=?,updated_at=current_timestamp WHERE id=?",
                [sc,ti,sig,json.dumps(fac),pid])
    print(f"Set: {pid} -> {ti} (score={sc})")
con.close()
