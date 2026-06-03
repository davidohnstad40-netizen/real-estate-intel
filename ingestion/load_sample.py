"""
One-time loader: reads the 52-home Excel spreadsheet → geocodes → inserts into DuckDB.
Run: python -m ingestion.load_sample
"""
import sys, os, re, time, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from geopy.geocoders import Nominatim
from geopy.exc import GeocoderTimedOut
from db.schema import get_db
from scoring.motivation import PropertyInput, score as compute_score

XLSX = r"C:\Users\d.ohnstad\OneDrive - Veeam Software Corporation\Documents\House search.xlsx"

def parse_price(val):
    if not val or str(val).strip() in ("nan", "", "None"): return None, None
    s = str(val)
    yr = re.search(r"\b(20\d{2}|19\d{2})\b", s)
    pr = re.search(r"\$([\d,]+)", s)
    year  = int(yr.group()) if yr else None
    price = float(pr.group(1).replace(",", "")) if pr else None
    return price, year

def parse_num(val):
    if val is None or str(val).strip() in ("nan", "", "None"): return None
    if isinstance(val, (int, float)): return float(val) if val > 0 else None
    s = re.sub(r"[^\d.]", "", str(val).split()[0])
    try: return float(s) if s else None
    except: return None

def geocode_address(geolocator, addr):
    full = f"{addr}, Blaine, MN 55449, USA"
    try:
        loc = geolocator.geocode(full, timeout=10)
        if loc:
            return loc.latitude, loc.longitude
    except GeocoderTimedOut:
        time.sleep(2)
        try:
            loc = geolocator.geocode(full, timeout=15)
            if loc: return loc.latitude, loc.longitude
        except: pass
    return None, None

def main():
    con = get_db()
    geo = Nominatim(user_agent="rei-platform-loader/1.0")

    # Read Realtor Summary (headers in row 2)
    df_sum = pd.read_excel(XLSX, sheet_name="Realtor Summary", header=1, dtype=str)
    # Read Research Data for MCRO + notes
    df_res = pd.read_excel(XLSX, sheet_name="Research Data", header=0, dtype=str)

    # Build mcro + notes lookup by address
    res_lookup = {}
    for _, row in df_res.iterrows():
        addr_raw = str(row.iloc[1] or "").strip()
        # Normalize: strip city/state suffix if present
        addr = addr_raw.split(",")[0].strip()
        if addr and addr != "nan":
            res_lookup[addr.upper()] = {
                "mcro":  str(row.iloc[11] or ""),
                "notes": str(row.iloc[12] or ""),
            }

    inserted = 0
    for _, row in df_sum.iterrows():
        addr_full = str(row.iloc[1] or "").strip()
        if not addr_full or addr_full == "nan":
            continue

        addr = addr_full.split(",")[0].strip()
        addr_key = addr.upper()

        # Parse all fields
        owner      = str(row.iloc[4]  or "")
        beds       = parse_num(row.iloc[5])
        baths      = parse_num(row.iloc[6])
        sqft       = parse_num(row.iloc[7])
        yr_built   = parse_num(row.iloc[8])
        yrs_owned  = parse_num(row.iloc[3])
        emv        = parse_num(row.iloc[11])
        prior_raw  = row.iloc[10]
        homestead  = str(row.iloc[14] or "Homestead")
        owner_type = str(row.iloc[14] or "Owner-Occupied")
        anoka_pin  = str(row.iloc[16] or "")
        school     = str(row.iloc[13] or "")
        flags      = str(row.iloc[15] or "")
        likelihood = str(row.iloc[2]  or "")

        # Motivation score & knock tier from computed columns (col T=19, col S=18)
        saved_score = parse_num(row.iloc[19]) if len(row) > 19 else None
        saved_tier  = str(row.iloc[18] or "TBD") if len(row) > 18 else "TBD"
        saved_signal = str(row.iloc[23] or "") if len(row) > 23 else ""

        price, yr = parse_price(prior_raw)

        # MCRO/notes from Research Data
        res = res_lookup.get(addr_key, {})
        mcro_text  = res.get("mcro",  "")
        notes_text = res.get("notes", "")

        # Geocode
        print(f"  Geocoding: {addr} ...", end=" ", flush=True)
        lat, lng = geocode_address(geo, addr)
        print(f"{lat:.4f}, {lng:.4f}" if lat else "no result")
        time.sleep(1.1)  # Nominatim rate limit: 1 req/sec

        prop_id = re.sub(r"[^A-Z0-9]", "_", addr_key)

        # Compute fresh score
        pi = PropertyInput(
            address=addr, owner_name=owner, emv=emv,
            prior_sale_price=price, prior_sale_year=yr,
            years_owned=yrs_owned, homestead=homestead,
            owner_type=owner_type, mcro_text=mcro_text,
            notes_text=notes_text, flags_text=flags,
            likelihood=likelihood,
        )
        result = compute_score(pi)

        # Use saved score if it's higher (already had human-verified MCRO data)
        final_score = int(saved_score) if saved_score else result.total
        # Determine tier from saved tier string
        if "T1" in saved_tier:    final_tier = "T1"
        elif "T2" in saved_tier:  final_tier = "T2"
        elif "SKIP" in saved_tier: final_tier = "SKIP"
        elif "T3" in saved_tier:   final_tier = "T3"
        else:                      final_tier = result.tier

        # Upsert property
        con.execute("""
            INSERT OR REPLACE INTO properties
            (id, address, lat, lng, owner_name, beds, baths, sqft, year_built,
             emv, est_value, prior_sale_price, prior_sale_year, years_owned,
             homestead, owner_type, anoka_pin, school_district, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)
        """, [prop_id, addr, lat, lng, owner,
              int(beds) if beds else None, baths,
              int(sqft) if sqft else None, int(yr_built) if yr_built else None,
              emv, emv * 1.08 if emv else None, price, yr,
              yrs_owned, homestead, owner_type, anoka_pin, school])

        # Upsert score
        con.execute("""
            INSERT OR REPLACE INTO property_scores
            (id, motivation_score, knock_tier, primary_signal, score_factors,
             est_equity_usd, equity_pct, monthly_piti, updated_at)
            VALUES (?,?,?,?,?,?,?,?,current_timestamp)
        """, [prop_id, final_score, final_tier,
              saved_signal or result.primary_signal,
              json.dumps(result.factors),
              result.equity_usd, result.equity_pct, result.monthly_piti])

        # Signals from MCRO
        if "divorce on record" in (mcro_text + flags).lower():
            con.execute("""
                INSERT OR IGNORE INTO property_signals (id, signal_type, signal_value, source)
                VALUES (?, 'divorce_confirmed', ?, 'MCRO')
            """, [prop_id, mcro_text[:500]])
        if "no homestead" in (homestead + owner_type + flags).lower():
            con.execute("""
                INSERT OR IGNORE INTO property_signals (id, signal_type, signal_value, source)
                VALUES (?, 'no_homestead', ?, 'County')
            """, [prop_id, homestead])

        inserted += 1

    print(f"\nLoaded {inserted} properties into DuckDB.")
    con.close()

if __name__ == "__main__":
    main()
