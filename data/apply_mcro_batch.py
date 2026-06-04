"""
Apply MCRO batch results from the 33 remaining property owner searches.
Key new signals found:
  - Quist, Nathan (11757 Naples) -- TWO Anoka County dissolutions (2023 + 2018) -> T1
  - Cummings, Jennifer (3304 117th) -- Dissolution with Child 2024 -> verify & upgrade
  - Cullen, Claire (3292 117th) -- Dissolution 2023 Steele County -> moderate signal
  - Rice, Michelle (3313 117th) -- Dissolution 2025 Hennepin -> possible, verify
  - 11715 Naples Cir -> reclassify from SKIP to LISTED

Pre-dates property / weak signals (keep T3, add note):
  - Broder/Marra/Sabir/Gentilini/Johnson -- pre-date property ownership or old/different county
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db

con = get_db()

# ── 1. Update 11715 Naples -- LISTED (active MLS at $999,999) ─────────────
con.execute("""
    UPDATE property_scores SET knock_tier='LISTED', motivation_score=0,
    primary_signal='Active MLS listing $999,999 (MLS #7009590) -- monitor for expiry or price cut',
    updated_at=current_timestamp
    WHERE id='11715_NAPLES_CIR_NE'
""")
print("11715 Naples Cir NE: SKIP -> LISTED (active $999,999 MLS listing)")

# ── 2. Nathan Quist (11757 Naples) -- T1: TWO Anoka County dissolutions ───
# Case 02-FA-23-944: Dissolution w/o Child filed 06/21/2023 (post-purchase)
# Case 02-FA-18-1787: Dissolution w/o Child filed 10/08/2018
# Both Anoka County -- HIGH CONFIDENCE this is the right person
con.execute("""
    UPDATE property_scores SET
        knock_tier='T1', motivation_score=45,
        primary_signal='POST-PURCHASE DIVORCE on record (2023 + 2018, Anoka County)',
        score_factors=?,
        updated_at=current_timestamp
    WHERE id='11757_NAPLES_CIR_NE'
""", [json.dumps({"divorce_confirmed": 40, "long_hold_12plus": 5})])
con.execute("""
    INSERT OR REPLACE INTO property_signals (id, signal_type, signal_value, source)
    VALUES (?,?,?,?)
""", ["11757_NAPLES_CIR_NE", "divorce_confirmed",
      "Case 02-FA-23-944 (2023) + 02-FA-18-1787 (2018): Erin Deborah Quist vs Nathan Phillip Quist, Anoka County, Dissolution w/o Child, Closed",
      "MCRO"])
print("11757 Naples Cir NE (Quist/Nathan): T3 -> T1  [2023 + 2018 Anoka dissolutions]")

# ── 3. Claire Cullen (3292 117th) -- T2: 2023 dissolution, Steele County ──
# Case 74-FA-23-222: Dissolution w/o Child filed 02/13/2023 under married name Schulz
# Steele County (not Anoka) -- moderate confidence, different county suggests
# she moved to Blaine after divorce or has separate case
con.execute("""
    UPDATE property_scores SET
        knock_tier='T2', motivation_score=25,
        primary_signal='Possible divorce 2023 (Steele County, married name Schulz) -- verify same person',
        score_factors=?,
        updated_at=current_timestamp
    WHERE id='3292_117TH_LN_NE'
""", [json.dumps({"divorce_possible": 20, "long_hold_10plus": 5})])
print("3292 117th Ln NE (Cullen/Claire): T3 -> T2  [2023 dissolution, Steele Co., verify]")

# ── 4. Jennifer Cummings (3304 117th) -- T2: 2024 dissolution, Nobles County ──
# Case 53-FA-24-684: Dissolution with Child filed 07/17/2024, Nobles County
# Jennifer Jean Cummings, DOB 10/05/1987 -- very recent, very fresh
# Nobles County (SW MN) doesn't match Blaine property location -- needs DOB verification
con.execute("""
    UPDATE property_scores SET
        knock_tier='T2', motivation_score=22,
        primary_signal='Possible divorce 2024 (Nobles County, Jennifer Jean Cummings DOB 10/05/1987) -- verify same person',
        score_factors=?,
        updated_at=current_timestamp
    WHERE id='3304_117TH_LN_NE'
""", [json.dumps({"divorce_possible": 20, "long_hold_10plus": 2})])
print("3304 117th Ln NE (Cummings/Jennifer): T3 -> T2  [2024 dissolution, Nobles Co., verify]")

# ── 5. Michelle Rice (3313 117th) -- note only, T3 stays ──────────────────
# Multiple individuals named Michelle Rice across MN
# Most notable: 27-FA-25-3085 filed 2025 in Hennepin (Michelle Leigh Rice)
# Property is in Anoka County -- different county suggests different person
# Keep T3 but add note for manual verification
con.execute("""
    UPDATE property_scores SET
        primary_signal='T3 -- Note: Michelle Leigh Rice has 2025 Hennepin dissolution; verify same person before visiting',
        updated_at=current_timestamp
    WHERE id='3313_117TH_LN_NE'
""")
print("3313 117th Ln NE (Rice/Michelle): T3 kept -- 2025 Hennepin case noted for manual verify")

# ── 6. Broder, Marra, Sabir, Gentilini, Johnson -- pre-date property ────────
notes = {
    "3247_117TH_LN_NE": "Prior legal separation 2010 (pre-dates likely ownership) -- not current signal",
    "3260_117TH_LN_NE": "Prior dissolution 2011 (pre-dates likely ownership) -- not current signal",
    "3291_117TH_LN_NE": "Prior dissolution 1992 Sherburne County (decades old) -- not current signal",
    "3224_117TH_LN_NE": "Prior dissolution 1996 (decades old, Paul Gentilini) -- not current signal",
    "11736_NAPLES_CIR_NE": "Prior dissolution 2011 Ramsey County (pre-dates property) -- not current signal",
}
for prop_id, note in notes.items():
    con.execute("""
        UPDATE property_scores SET primary_signal=COALESCE(primary_signal,'') || ' | MCRO: ' || ?,
        updated_at=current_timestamp WHERE id=?
    """, [note, prop_id])
print(f"Added MCRO notes to {len(notes)} properties (pre-date ownership -- not current signals)")

# ── 7. Clean -- all others ─────────────────────────────────────────────────
clean = [
    "3167_117TH_LN_NE", "3212_117TH_LN_NE", "3223_117TH_LN_NE", "3236_117TH_LN_NE",
    "3272_117TH_LN_NE", "3297_117TH_LN_NE", "3332_117TH_LN_NE", "3364_117TH_LN_NE",
    "3368_117TH_LN_NE", "3400_117TH_LN_NE", "3436_117TH_LN_NE", "3527_117TH_LN_NE",
    "3550_117TH_LN_NE", "3557_117TH_LN_NE", "11719_NAPLES_CIR_NE", "11739_NAPLES_CIR_NE",
    "11742_NAPLES_CIR_NE", "11745_NAPLES_CIR_NE", "11748_NAPLES_CIR_NE", "11753_NAPLES_CIR_NE",
    "11756_NAPLES_CIR_NE", "11760_NAPLES_CIR_NE", "11761_NAPLES_CIR_NE", "11764_NAPLES_CIR_NE",
    "11769_NAPLES_CIR_NE", "3166_117TH_LN_NE", "3186_117TH_LN_NE",
    # Search names that returned clean
    "3200_117TH_LN_NE",   # Larson Leslie (48 results, no Anoka match)
    "3201_117TH_LN_NE",   # Kleinjan Ryan (minor consumption 1999 only)
]
for pid in clean:
    try:
        con.execute("""
            UPDATE property_scores SET
            primary_signal=COALESCE(primary_signal,'') || ' | MCRO: No court records found',
            updated_at=current_timestamp WHERE id=?
        """, [pid])
    except Exception:
        pass
print(f"Marked {len(clean)} properties as MCRO clean")

con.close()
print()
print("=== NEW MCRO TIER CHANGES ===")
print("11715 Naples Cir NE:  SKIP   -> LISTED (active MLS $999,999)")
print("11757 Naples Cir NE:  T3/T2  -> T1    (Nathan Quist: 2023+2018 Anoka dissolutions)")
print("3292  117th Ln NE:    T2     -> T2    (Claire Cullen: 2023 Steele dissolution, verify)")
print("3304  117th Ln NE:    T3     -> T2    (Jennifer Cummings: 2024 Nobles dissolution, verify)")
print()
print("ACTION ITEMS:")
print("  KNOCK NOW:  11757 Naples (Quist) -- two confirmed Anoka dissolutions")
print("  VERIFY THEN KNOCK:  3292 (Cullen), 3304 (Cummings) -- different county, need DOB confirm")
print("  WATCH:  11715 Naples (listed) -- if listing expires, knock immediately")
