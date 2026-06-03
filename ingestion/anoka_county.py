"""
Anoka County Parcel Ingestion
Downloads the MN Metro Regional Parcel dataset (public, free) and loads
parcels for a target city into DuckDB. Run once; re-run to refresh.

Source: MN Geospatial Commons
URL:    https://resources.gisdata.mn.gov/pub/gdrs/data/pub/us_mn_state_metrogis/
        plan_regional_parcels_open/shp_plan_regional_parcels_open.zip
Size:   ~250 MB compressed
"""

import os, sys, zipfile, tempfile, json, re, time
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import urllib.request
import duckdb
from db.schema import get_db

PARCEL_ZIP_URL = (
    "https://resources.gisdata.mn.gov/pub/gdrs/data/pub/us_mn_state_metrogis/"
    "plan_regional_parcels_open/shp_plan_regional_parcels_open.zip"
)

def download_with_progress(url: str, dest: str):
    print(f"Downloading {url}")
    print("(~250 MB — will take a few minutes on first run)")
    def progress(count, block_size, total_size):
        pct = count * block_size * 100 / total_size if total_size > 0 else 0
        print(f"\r  {min(pct,100):.1f}%", end="", flush=True)
    urllib.request.urlretrieve(url, dest, reporthook=progress)
    print()

def load_parcels(city: str = "Blaine", state: str = "MN", db_path: str = None,
                 parcel_db_path: str = None):
    """Download parcel shapefile and load into DuckDB parcels_raw table."""
    # Use a separate parcels.duckdb so this doesn't conflict with the
    # main rei.duckdb held by the Streamlit app
    parcel_db_path = parcel_db_path or os.path.join(
        os.path.dirname(__file__), "..", "data", "parcels.duckdb"
    )
    con = get_db(parcel_db_path)

    # Install spatial extension
    try:
        con.execute("INSTALL spatial; LOAD spatial;")
    except Exception as e:
        print(f"Spatial install note: {e}")
        try: con.execute("LOAD spatial;")
        except: pass

    # Create parcels_raw table
    con.execute("""
        CREATE TABLE IF NOT EXISTS parcels_raw (
            pin          VARCHAR PRIMARY KEY,
            address      VARCHAR,
            city         VARCHAR,
            state        VARCHAR,
            zip          VARCHAR,
            owner_name   VARCHAR,
            emv_total    DOUBLE,
            homestead    VARCHAR,
            land_use     VARCHAR,
            year_built   INTEGER,
            sqft         INTEGER,
            beds         INTEGER,
            sale_price   DOUBLE,
            sale_date    VARCHAR,
            lat          DOUBLE,
            lng          DOUBLE,
            geom_wkt     VARCHAR,
            last_updated TIMESTAMP DEFAULT current_timestamp
        )
    """)

    already = con.execute("SELECT COUNT(*) FROM parcels_raw WHERE city = ?", [city]).fetchone()[0]
    if already > 0:
        print(f"  {already} {city} parcels already loaded. Skipping download.")
        con.close()
        return

    # Download shapefile
    cache_dir = os.path.join(os.path.dirname(__file__), "..", "data", "cache")
    os.makedirs(cache_dir, exist_ok=True)
    zip_path  = os.path.join(cache_dir, "mn_parcels.zip")
    shp_dir   = os.path.join(cache_dir, "mn_parcels_shp")

    if not os.path.exists(zip_path):
        download_with_progress(PARCEL_ZIP_URL, zip_path)
    else:
        print(f"  Using cached zip: {zip_path}")

    if not os.path.exists(shp_dir):
        print("  Extracting shapefile...")
        with zipfile.ZipFile(zip_path, "r") as z:
            z.extractall(shp_dir)

    # Find the .shp file
    shp_files = [os.path.join(shp_dir, f) for f in os.listdir(shp_dir) if f.endswith(".shp")]
    if not shp_files:
        raise FileNotFoundError(f"No .shp file found in {shp_dir}")
    shp_path = shp_files[0]
    print(f"  Loading from: {shp_path}")

    # Read shapefile via DuckDB spatial
    # First inspect column names
    sample = con.execute(f"SELECT * FROM ST_Read('{shp_path}') LIMIT 1").df()
    print(f"  Shapefile columns: {list(sample.columns)}")

    # Map common MN parcel column names (vary by dataset version)
    col_map = {c.upper(): c for c in sample.columns}
    def col(name, fallback="NULL"):
        aliases = {
            "PIN":       ["PIN","PARCEL_ID","PARID","PID"],
            "ADDRESS":   ["ADD_FULL","ADDR_FULL","SITEADDR","SITUSADD","ADDRESS"],
            "CITY":      ["CITY","MUNI_NAME","MUNINAME"],
            "ZIP":       ["ZIP","ZIPCODE","ZIP5"],
            "OWNER":     ["OWNER","OWN1","OWNNAME","OWNER_NAME"],
            "EMV":       ["EMV_TOTAL","EMV","TOTALEMV","MKT_VAL"],
            "HOMESTEAD": ["HMSTD_CD1","HMSTD","HOMESTEAD"],
            "LANDUSE":   ["USE1_DESC","USECD","LUSE_DESC"],
            "YEARBUILT": ["YEAR_BUILT","YR_BLT","YRBUILT"],
            "SQFT":      ["SQFT_BLDG","BLDGSQFT","GBA"],
            "SALEPRICE": ["SALE1_AMT","SALE_PRICE","SALEPRICE"],
            "SALEDATE":  ["SALE1_DATE","SALE_DATE","SALEDATE"],
        }
        for alias in aliases.get(name, [name]):
            if alias in col_map: return col_map[alias]
        return fallback

    city_col = col("CITY")
    city_filter = f"UPPER({city_col}) = '{city.upper()}'" if city_col != "NULL" else "1=1"

    print(f"  Filtering for city={city}...")
    count = con.execute(f"""
        SELECT COUNT(*) FROM ST_Read('{shp_path}')
        WHERE {city_filter}
    """).fetchone()[0]
    print(f"  Found {count} {city} parcels")

    if count == 0:
        print("  WARNING: No parcels found. Check city name or column mapping.")
        con.close()
        return

    # Insert into parcels_raw
    con.execute(f"""
        INSERT OR IGNORE INTO parcels_raw
            (pin, address, city, state, zip, owner_name, emv_total, homestead,
             land_use, year_built, sqft, sale_price, sale_date, geom_wkt)
        SELECT
            CAST({col('PIN')} AS VARCHAR),
            {col('ADDRESS')},
            {col('CITY')},
            '{state}',
            CAST({col('ZIP')} AS VARCHAR),
            {col('OWNER')},
            TRY_CAST({col('EMV')} AS DOUBLE),
            CAST({col('HOMESTEAD')} AS VARCHAR),
            {col('LANDUSE')},
            TRY_CAST({col('YEARBUILT')} AS INTEGER),
            TRY_CAST({col('SQFT')} AS INTEGER),
            TRY_CAST({col('SALEPRICE')} AS DOUBLE),
            CAST({col('SALEDATE')} AS VARCHAR),
            ST_AsText(geom)
        FROM ST_Read('{shp_path}')
        WHERE {city_filter}
    """)

    loaded = con.execute("SELECT COUNT(*) FROM parcels_raw WHERE city ILIKE ?", [city]).fetchone()[0]
    print(f"  Loaded {loaded} {city} parcels into parcels_raw.")
    con.close()


def score_parcels_from_raw(db_path: str = None, limit: int = None):
    """
    Run Tier-1 cheap scoring on all parcels_raw rows not yet in properties.
    Tier 1 = hold duration + homestead + land use only (no MCRO, no Zillow).
    """
    from scoring.motivation import PropertyInput, score as compute_score
    con = get_db(db_path)

    q = """
        SELECT pin, address, city, zip, owner_name, emv_total, homestead,
               land_use, year_built, sale_price, sale_date, lat, lng
        FROM parcels_raw
        WHERE pin NOT IN (SELECT id FROM properties)
    """
    if limit: q += f" LIMIT {limit}"
    df = con.execute(q).df()
    print(f"Scoring {len(df)} new parcels...")

    for _, row in df.iterrows():
        addr = str(row.address or "")
        prop_id = re.sub(r"[^A-Z0-9]", "_", str(row.pin or addr).upper())

        # Estimate years owned from sale date
        yrs = None
        if row.sale_date:
            m = re.search(r"(20\d{2}|19\d{2})", str(row.sale_date))
            if m: yrs = 2026 - int(m.group())

        pi = PropertyInput(
            address       = addr,
            owner_name    = str(row.owner_name or ""),
            emv           = float(row.emv_total) if row.emv_total else None,
            prior_sale_price = float(row.sale_price) if row.sale_price else None,
            years_owned   = yrs,
            homestead     = str(row.homestead or ""),
            owner_type    = "No Homestead" if "N" in str(row.homestead or "").upper() else "Owner-Occupied",
        )
        result = compute_score(pi)

        con.execute("""
            INSERT OR IGNORE INTO properties
            (id, address, city, zip, owner_name, emv, est_value, years_owned,
             homestead, owner_type, anoka_pin, lat, lng, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)
        """, [prop_id, addr, str(row.city or ""), str(row.zip or ""),
              str(row.owner_name or ""),
              float(row.emv_total) if row.emv_total else None,
              float(row.emv_total)*1.08 if row.emv_total else None,
              yrs, str(row.homestead or ""), pi.owner_type,
              str(row.pin or ""),
              float(row.lat) if row.lat else None,
              float(row.lng) if row.lng else None])

        con.execute("""
            INSERT OR REPLACE INTO property_scores
            (id, motivation_score, knock_tier, primary_signal, score_factors, updated_at)
            VALUES (?,?,?,?,?,current_timestamp)
        """, [prop_id, result.total, result.tier, result.primary_signal,
              json.dumps(result.factors)])

    con.close()
    print(f"Done. {len(df)} parcels scored at Tier 1.")


if __name__ == "__main__":
    city = sys.argv[1] if len(sys.argv) > 1 else "Blaine"
    load_parcels(city=city)
    score_parcels_from_raw()
