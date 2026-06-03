"""Generate seller theses and offer models for all T1/T2 properties."""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db
from agents.seller_thesis import generate_thesis, generate_offer_model

con = get_db(read_only=True)
props = con.execute(
    "SELECT p.id, p.address, p.owner_name, p.years_owned, s.motivation_score, "
    "s.knock_tier, s.primary_signal, s.score_factors, s.est_equity_usd, "
    "s.equity_pct, s.monthly_piti, p.emv, p.est_value, p.homestead "
    "FROM properties p LEFT JOIN property_scores s ON p.id = s.id "
    "WHERE s.knock_tier IN ('T1','T2') ORDER BY s.motivation_score DESC"
).df()
con.close()

results = {}
for _, row in props.iterrows():
    print(f"  {row['address']}...", end=" ", flush=True)
    d = row.to_dict()
    try:
        results[row["id"]] = {
            "thesis":  generate_thesis(d),
            "offer":   generate_offer_model(d),
            "address": row["address"],
            "tier":    row["knock_tier"],
        }
        print("done")
    except Exception as e:
        print(f"ERROR: {e}")

out = os.path.join(os.path.dirname(__file__), "theses.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(results, f, indent=2)
print(f"\nSaved {len(results)} theses -> {out}")
