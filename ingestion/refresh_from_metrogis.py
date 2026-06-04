"""
Refresh all 52 existing properties in DuckDB with live MetroGIS 2025 data.
Updates: EMV, sale price/year, homestead, sqft, year built, absentee flag.
Re-scores all properties with the fresh data.
Run: python -m ingestion.refresh_from_metrogis (stop Streamlit first)
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db
from ingestion.metrogis import lookup_address
from scoring.motivation import PropertyInput, score as compute_score

def main():
    con = get_db()

    # Add new columns if missing — check existence first to avoid aborting transaction
    def has_column(c, table, col):
        try:
            c.execute(f"SELECT {col} FROM {table} LIMIT 0")
            return True
        except Exception:
            c.execute("ROLLBACK")
            return False

    if not has_column(con, "properties", "absentee_flag"):
        con.execute("ALTER TABLE properties ADD COLUMN absentee_flag BOOLEAN DEFAULT FALSE")
        print("Added absentee_flag column")

    if not has_column(con, "properties", "owner_mailing"):
        con.execute("ALTER TABLE properties ADD COLUMN owner_mailing VARCHAR")
        print("Added owner_mailing column")

    if not has_column(con, "properties", "scan_source"):
        con.execute("ALTER TABLE properties ADD COLUMN scan_source VARCHAR DEFAULT 'manual'")
        print("Added scan_source column")

    # Only refresh the 52 manually curated properties (not the 18K city scan entries)
    props = con.execute(
        "SELECT id, address, city FROM properties "
        "WHERE scan_source IS NULL OR scan_source = 'manual' "
        "ORDER BY address"
    ).fetchall()

    print(f"Refreshing {len(props)} properties from MetroGIS 2025 API...")
    print()

    updated   = 0
    not_found = 0
    changes   = []

    for prop_id, address, city in props:
        addr_short = address.split(",")[0].strip()
        city_name  = city or "Blaine"

        result = lookup_address(addr_short, city_name)
        if not result:
            print(f"  NOT FOUND: {addr_short}")
            not_found += 1
            continue

        # Detect changes vs current data
        cur = con.execute(
            "SELECT emv, prior_sale_price, prior_sale_year, homestead, sqft, year_built "
            "FROM properties WHERE id = ?", [prop_id]
        ).fetchone()

        old_emv  = cur[0] if cur else None
        new_emv  = result["emv"]
        emv_diff = ""
        if old_emv and new_emv and abs(old_emv - new_emv) > 1000:
            emv_diff = f" [EMV: ${old_emv:,.0f} -> ${new_emv:,.0f}]"

        # Update property record
        con.execute("""
            UPDATE properties SET
                emv              = ?,
                est_value        = ?,
                prior_sale_price = COALESCE(?, prior_sale_price),
                prior_sale_year  = COALESCE(?, prior_sale_year),
                years_owned      = ?,
                homestead        = ?,
                owner_type       = ?,
                sqft             = COALESCE(?, sqft),
                year_built       = COALESCE(?, year_built),
                absentee_flag    = ?,
                owner_mailing    = ?,
                updated_at       = current_timestamp
            WHERE id = ?
        """, [
            new_emv,
            new_emv * 1.08 if new_emv else None,
            result.get("prior_sale_price"),
            result.get("prior_sale_year"),
            result.get("years_owned"),
            result.get("homestead",""),
            result.get("owner_type",""),
            result.get("sqft"),
            result.get("year_built"),
            result.get("absentee", False),
            result.get("owner_mailing"),
            prop_id,
        ])

        # Re-score with fresh data
        cur2 = con.execute(
            "SELECT owner_name, owner_type, homestead, emv, prior_sale_price, "
            "prior_sale_year, years_owned FROM properties WHERE id=?", [prop_id]
        ).fetchone()

        # Pull ALL confirmed signals from property_signals table
        # (MCRO divorce/probate signals must survive a MetroGIS refresh)
        signal_rows = con.execute(
            "SELECT signal_type, signal_value FROM property_signals WHERE id=?",
            [prop_id]
        ).fetchall()
        mcro_text = " ".join(
            sv for st, sv in signal_rows
            if st in ("divorce_confirmed","divorce_possible","divorce_prior","probate")
            and sv
        )

        # Also preserve existing primary_signal text for signal parsing
        old_score_row = con.execute(
            "SELECT primary_signal, knock_tier, motivation_score FROM property_scores WHERE id=?",
            [prop_id]
        ).fetchone()
        existing_signal = old_score_row[0] if old_score_row else ""
        existing_tier   = old_score_row[1] if old_score_row else None
        existing_score  = old_score_row[2] if old_score_row else None

        pi = PropertyInput(
            address          = addr_short,
            emv              = cur2[3],
            prior_sale_price = cur2[4],
            prior_sale_year  = cur2[5],
            years_owned      = cur2[6],
            homestead        = cur2[2] or "",
            owner_type       = cur2[1] or "",
            owner_name       = cur2[0] or "",
            mcro_text        = mcro_text,
            flags_text       = existing_signal or "",
        )
        r = compute_score(pi)

        # Check for tier change
        old_tier = con.execute(
            "SELECT knock_tier, motivation_score FROM property_scores WHERE id=?",
            [prop_id]
        ).fetchone()

        tier_change = ""
        if old_tier and old_tier[0] != r.tier:
            tier_change = f"  [TIER CHANGED: {old_tier[0]} -> {r.tier}]"
            changes.append({
                "address": addr_short, "old_tier": old_tier[0], "new_tier": r.tier,
                "old_score": old_tier[1], "new_score": r.total,
            })

        # GUARD: never downgrade a T1 that has confirmed MCRO signals
        # (MetroGIS doesn't know about divorces or court records)
        final_score = r.total
        final_tier  = r.tier
        final_signal = r.primary_signal
        final_factors = r.factors

        has_confirmed_mcro = any(
            st in ("divorce_confirmed","probate") for st, _ in signal_rows
        )
        if has_confirmed_mcro and existing_tier == "T1" and r.tier != "T1":
            print(f"    [MCRO GUARD] Keeping T1 for {addr_short} -- confirmed MCRO signal")
            final_score  = max(r.total, existing_score or 40)
            final_tier   = "T1"
            final_signal = existing_signal  # preserve MCRO-sourced signal text
            final_factors = json.loads(old_score_row[1]) if old_score_row else r.factors

        con.execute("""
            UPDATE property_scores SET
                motivation_score = ?,
                knock_tier       = ?,
                primary_signal   = ?,
                score_factors    = ?,
                est_equity_usd   = ?,
                equity_pct       = ?,
                monthly_piti     = ?,
                updated_at       = current_timestamp
            WHERE id = ?
        """, [final_score, final_tier, final_signal,
              json.dumps(final_factors), r.equity_usd,
              r.equity_pct, r.monthly_piti, prop_id])

        absentee_flag = "  [ABSENTEE]" if result.get("absentee") else ""
        print(f"  {addr_short}: score={r.total} {r.tier}{emv_diff}{tier_change}{absentee_flag}")
        updated += 1

    print(f"\nDone. Updated: {updated} | Not found: {not_found}")

    if changes:
        print(f"\n*** TIER CHANGES ({len(changes)}) ***")
        for c in changes:
            print(f"  {c['address']}: {c['old_tier']} ({c['old_score']}) -> "
                  f"{c['new_tier']} ({c['new_score']})")
    else:
        print("\nNo tier changes from data refresh.")

    con.close()

if __name__ == "__main__":
    main()
