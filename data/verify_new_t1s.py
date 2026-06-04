"""Verify new T1/T2 findings via free skip trace."""
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from agents.skip_trace import skip_trace_property

VERIFY = [
    ("11757_NAPLES_CIR_NE", "Nathan Quist",     "Two Anoka County dissolutions 2018+2023", "DOB unknown"),
    ("3304_117TH_LN_NE",    "Jennifer Cummings", "Nobles County dissolution 2024",          "DOB 10/05/1987"),
    ("3292_117TH_LN_NE",    "Claire Cullen",     "Steele County dissolution 2023 (Schulz)", "DOB unknown"),
]

print("=" * 60)
print("SKIP TRACE VERIFICATION -- New T1/T2 Properties")
print("=" * 60)

for prop_id, name, mcro_note, expected_dob in VERIFY:
    print(f"\n{name} ({prop_id.replace('_',' ')})")
    print(f"  MCRO: {mcro_note}")
    print(f"  Expected: {expected_dob}")
    print(f"  Running free skip trace (FastPeopleSearch)...")

    result, source = skip_trace_property(prop_id, force_paid=False)
    if result and (result.get("phone1") or result.get("email1")):
        print(f"  Source: {source}")
        fields = ["phone1","phone2","email1","dob","mailing_addr","relatives"]
        for k in fields:
            v = result.get(k)
            if v:
                print(f"  {k:15}: {v}")

        # DOB verification
        dob_found = result.get("dob","")
        if "1987" in str(dob_found) and "1987" in expected_dob:
            print(f"  *** DOB MATCH: {dob_found} -- SAME PERSON as MCRO case ***")
        elif dob_found and expected_dob != "DOB unknown":
            print(f"  *** DOB CHECK: found={dob_found}, expected={expected_dob} ***")

        # Mailing address check
        mail = result.get("mailing_addr","")
        if mail and "blaine" not in mail.lower() and "mn" not in mail.lower():
            print(f"  *** MAILING DIFFERS: {mail} -- ABSENTEE SIGNAL ***")
    else:
        print(f"  No results from free skip trace (source: {source})")
        print(f"  Try paid: python -m agents.skip_trace (requires BATCH_SKIP_API_KEY)")

print()
print("=" * 60)
print("INTERPRETATION:")
print("  DOB match -> high confidence same person -> upgrade to T1")
print("  Mailing address differs -> absentee -> additional signal")
print("  No results -> try paid skip trace ($0.18)")
