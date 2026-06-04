"""
Re-score all 18,792 city scan properties with the v1.4 engine.
Fixes EMV lag false positives (Zest St NE etc.) introduced before the guard was added.
Run: python -m ingestion.rescore_city_scan (stop Streamlit first)
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import pandas as pd
from db.schema import get_db
from scoring.motivation import PropertyInput, score as compute_score

def main():
    con = get_db()
    props = con.execute("""
        SELECT p.id, p.address, p.emv, p.prior_sale_price, p.prior_sale_year,
               p.years_owned, p.homestead, p.owner_type, p.owner_name
        FROM properties p
        WHERE p.scan_source = 'metrogis_scan'
    """).df()

    print(f"Re-scoring {len(props)} city scan properties with v1.4 engine...")

    batch_size = 500
    updated = t1_count = t2_count = t3_count = false_t1_fixed = 0

    for start in range(0, len(props), batch_size):
        batch = props.iloc[start:start+batch_size]
        for _, row in batch.iterrows():
            try:
                sale_yr = int(row.prior_sale_year) if pd.notna(row.prior_sale_year) else None
                pi = PropertyInput(
                    address          = str(row.address or ""),
                    emv              = float(row.emv) if pd.notna(row.emv) else None,
                    prior_sale_price = float(row.prior_sale_price) if pd.notna(row.prior_sale_price) else None,
                    prior_sale_year  = sale_yr,
                    years_owned      = float(row.years_owned) if pd.notna(row.years_owned) else None,
                    homestead        = str(row.homestead or ""),
                    owner_type       = str(row.owner_type or ""),
                    owner_name       = str(row.owner_name or ""),
                )
                r = compute_score(pi)

                # Check if this was a false T1 being fixed
                old = con.execute(
                    "SELECT knock_tier FROM property_scores WHERE id=?",
                    [row.id]
                ).fetchone()
                if old and old[0] == "T1" and r.tier != "T1":
                    false_t1_fixed += 1

                con.execute("""
                    INSERT OR REPLACE INTO property_scores
                    (id, motivation_score, knock_tier, primary_signal, score_factors,
                     est_equity_usd, equity_pct, monthly_piti, updated_at)
                    VALUES (?,?,?,?,?,?,?,?,current_timestamp)
                """, [row.id, r.total, r.tier, r.primary_signal,
                      json.dumps(r.factors), r.equity_usd, r.equity_pct, r.monthly_piti])

                if r.tier == "T1": t1_count += 1
                elif r.tier == "T2": t2_count += 1
                else: t3_count += 1
                updated += 1
            except Exception:
                pass

        pct = (start + len(batch)) * 100 // len(props)
        print(f"  {pct}% -- {start+len(batch)}/{len(props)} scored...", flush=True)

    con.close()
    print(f"\nDone. {updated} re-scored.")
    print(f"  T1: {t1_count}  T2: {t2_count}  T3: {t3_count}")
    print(f"  False T1s corrected: {false_t1_fixed}")
    print("City scan now reflects v1.4 engine (EMV lag fix applied).")

if __name__ == "__main__":
    main()
