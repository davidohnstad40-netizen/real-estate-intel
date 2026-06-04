"""Smoke test all the key changes from the audit fixes."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# Test 1: FACTOR_LABELS exists in motivation.py
try:
    from scoring.motivation import FACTOR_LABELS, TIER_COLOR, TIER_LABEL
    assert "LISTED" in TIER_COLOR, "LISTED missing from TIER_COLOR"
    assert "peak_buyer_2020_22" in FACTOR_LABELS, "peak_buyer_2020_22 missing from FACTOR_LABELS"
    assert TIER_COLOR["LISTED"] == "#1565C0", f"LISTED color wrong: {TIER_COLOR['LISTED']}"
    print(f"OK: FACTOR_LABELS has {len(FACTOR_LABELS)} entries, LISTED tier = {TIER_COLOR['LISTED']}")
except Exception as e:
    print(f"FAIL: {e}")

# Test 2: DB overrides are correct
try:
    from db.schema import get_db
    con = get_db(read_only=True)
    overrides = con.execute("""
        SELECT p.address, ps.knock_tier, ps.motivation_score
        FROM property_scores ps
        JOIN properties p ON p.id = ps.id
        WHERE p.id IN ('3316_117TH_LN_NE','11725_NAPLES_CIR_NE',
                       '3448_117TH_LN_NE','11715_NAPLES_CIR_NE','11757_NAPLES_CIR_NE')
        ORDER BY p.address
    """).fetchall()
    con.close()
    print("\nDB tier verification:")
    expected = {
        "11715 Naples Cir NE": "LISTED",
        "11725 Naples Cir NE": "T1",
        "11757 Naples Cir NE": "T1",
        "3316 117th Ln NE":    "T1",
        "3448 117th Ln NE":    "T1",
    }
    all_ok = True
    for addr, tier, score in overrides:
        short = addr.split(",")[0].strip()
        want = expected.get(short, "?")
        status = "OK" if tier == want else "FAIL"
        if tier != want:
            all_ok = False
        print(f"  {status}: {short:30} -> {tier:6} (score={score}) [expected {want}]")
    if all_ok:
        print("  All overrides correct!")
except Exception as e:
    print(f"FAIL DB check: {e}")

# Test 3: export_snapshot works
try:
    from ingestion.export_snapshot import export_to_parquet
    print("\nOK: export_snapshot module imports correctly")
except Exception as e:
    print(f"FAIL export_snapshot: {e}")

# Test 4: cloud_app imports without DuckDB
try:
    import importlib.util
    spec = importlib.util.spec_from_file_location("cloud_app",
        os.path.join(os.path.dirname(__file__), "..", "app", "cloud_app.py"))
    # Just check it can be read -- don't execute (needs streamlit context)
    with open(os.path.join(os.path.dirname(__file__), "..", "app", "cloud_app.py")) as f:
        src = f.read()
    assert "duckdb" not in src, "cloud_app.py should not import duckdb"
    assert "parquet" in src, "cloud_app.py should read parquet files"
    assert "SHARE_PASSWORD" in src, "cloud_app.py should have password gate"
    print("OK: cloud_app.py is DuckDB-free, reads parquet, has password gate")
except Exception as e:
    print(f"FAIL cloud_app check: {e}")

print("\nSmoke test complete.")
