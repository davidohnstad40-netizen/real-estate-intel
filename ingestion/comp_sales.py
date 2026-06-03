"""
Comparable Sales Engine
Queries parcels_raw (in parcels.duckdb) for recent sales near a target property.
Falls back to EMV-based estimate if parcel data not available.
"""
import os
import sys
import math
from datetime import datetime, timedelta

import pandas as pd

# ---------------------------------------------------------------------------
# Path setup -- allow running from any cwd
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

_DEFAULT_PARCELS_DB = os.path.join(_ROOT, "data", "parcels.duckdb")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _haversine_miles(lat1: float, lng1: float, lat2: float, lng2: float) -> float:
    """
    Fast Euclidean-approximation distance in miles (accurate within ~1% for
    short distances typical in a metro area).

    dist = 69 * sqrt((lat2-lat1)^2 + ((lng2-lng1)*cos(radians(lat1)))^2)
    """
    dlat = lat2 - lat1
    dlng = (lng2 - lng1) * math.cos(math.radians(lat1))
    return 69.0 * math.sqrt(dlat ** 2 + dlng ** 2)


def _parcels_db_path(db_path=None) -> str:
    return db_path or _DEFAULT_PARCELS_DB


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def get_comps(
    lat: float,
    lng: float,
    sqft: float = None,
    db_path: str = None,
    radius_miles: float = 0.5,
    years_back: int = 3,
    limit: int = 8,
) -> pd.DataFrame:
    """
    Find recent comparable sales near a target property.

    Parameters
    ----------
    lat, lng        : Target property coordinates.
    sqft            : Target sqft (used only to enrich context; not a filter).
    db_path         : Path to parcels.duckdb (defaults to data/parcels.duckdb).
    radius_miles    : Search radius in miles (default 0.5).
    years_back      : How many years of sales history to include (default 3).
    limit           : Max comps to return after distance sort (default 8).

    Returns
    -------
    pd.DataFrame with columns:
        address, sale_price, sale_date, sqft, price_per_sqft, dist_miles, year_built
    Returns an empty DataFrame if parcels.duckdb does not exist or has no data.
    """
    path = _parcels_db_path(db_path)

    # Guard: DB file might not exist yet
    if not os.path.exists(path):
        return pd.DataFrame(
            columns=["address", "sale_price", "sale_date", "sqft",
                     "price_per_sqft", "dist_miles", "year_built"]
        )

    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
    except Exception:
        return pd.DataFrame(
            columns=["address", "sale_price", "sale_date", "sqft",
                     "price_per_sqft", "dist_miles", "year_built"]
        )

    cutoff_date = (datetime.today() - timedelta(days=years_back * 365)).strftime("%Y-%m-%d")

    try:
        raw = con.execute("""
            SELECT
                address,
                sale_price,
                sale_date,
                sqft,
                year_built,
                lat,
                lng
            FROM parcels_raw
            WHERE
                sale_price > 10000
                AND sale_date IS NOT NULL
                AND sale_date >= ?
                AND lat IS NOT NULL
                AND lng IS NOT NULL
        """, [cutoff_date]).fetchdf()
    except Exception:
        con.close()
        return pd.DataFrame(
            columns=["address", "sale_price", "sale_date", "sqft",
                     "price_per_sqft", "dist_miles", "year_built"]
        )
    finally:
        con.close()

    if raw.empty:
        return pd.DataFrame(
            columns=["address", "sale_price", "sale_date", "sqft",
                     "price_per_sqft", "dist_miles", "year_built"]
        )

    # Calculate distance in Python (haversine approximation)
    raw["dist_miles"] = raw.apply(
        lambda row: _haversine_miles(lat, lng, row["lat"], row["lng"]), axis=1
    )

    # Filter to radius
    nearby = raw[raw["dist_miles"] <= radius_miles].copy()

    if nearby.empty:
        return pd.DataFrame(
            columns=["address", "sale_price", "sale_date", "sqft",
                     "price_per_sqft", "dist_miles", "year_built"]
        )

    # Compute price per sqft (only where sqft > 0)
    nearby["price_per_sqft"] = nearby.apply(
        lambda row: (row["sale_price"] / row["sqft"])
        if (row["sqft"] and row["sqft"] > 0) else None,
        axis=1,
    )

    # Sort by distance, take top N
    nearby = nearby.sort_values("dist_miles").head(limit)

    # Return only the requested columns
    result = nearby[
        ["address", "sale_price", "sale_date", "sqft",
         "price_per_sqft", "dist_miles", "year_built"]
    ].reset_index(drop=True)

    return result


def estimate_value(
    lat: float,
    lng: float,
    sqft: float,
    emv: float = None,
    db_path: str = None,
) -> dict:
    """
    Estimate market value for a property using comp-based or EMV-based methods.

    Returns
    -------
    dict with keys:
        est_value     : float  -- estimated market value in dollars
        median_ppsf   : float or None -- median $/sqft from comps
        comp_count    : int
        method        : str -- 'comp_based' or 'emv_based'
        comps_df      : pd.DataFrame -- the raw comps used
    """
    comps = get_comps(lat=lat, lng=lng, sqft=sqft, db_path=db_path)

    # Drop rows without price_per_sqft for median calculation
    valid_comps = comps.dropna(subset=["price_per_sqft"])

    if not valid_comps.empty and sqft and sqft > 0:
        median_ppsf = float(valid_comps["price_per_sqft"].median())
        est_value = median_ppsf * sqft
        method = "comp_based"
    else:
        median_ppsf = None
        if emv and emv > 0:
            est_value = emv * 1.08
        else:
            est_value = 0.0
        method = "emv_based"

    return {
        "est_value": est_value,
        "median_ppsf": median_ppsf,
        "comp_count": len(valid_comps),
        "method": method,
        "comps_df": comps,
    }


def get_neighborhood_stats(city: str = "Blaine", db_path: str = None) -> dict:
    """
    Aggregate market statistics for all recent sales in a given city.

    Parameters
    ----------
    city     : City name to filter on (default 'Blaine').
    db_path  : Path to parcels.duckdb.

    Returns
    -------
    dict with keys:
        median_price       : float or None
        median_ppsf        : float or None
        avg_days_since_sale: float or None
        total_sales        : int
        median_sqft        : float or None
    """
    _empty = {
        "median_price": None,
        "median_ppsf": None,
        "avg_days_since_sale": None,
        "total_sales": 0,
        "median_sqft": None,
    }

    path = _parcels_db_path(db_path)
    if not os.path.exists(path):
        return _empty

    try:
        import duckdb
        con = duckdb.connect(path, read_only=True)
    except Exception:
        return _empty

    cutoff_date = (datetime.today() - timedelta(days=3 * 365)).strftime("%Y-%m-%d")

    try:
        df = con.execute("""
            SELECT
                sale_price,
                sale_date,
                sqft
            FROM parcels_raw
            WHERE
                LOWER(city) = LOWER(?)
                AND sale_price > 10000
                AND sale_date IS NOT NULL
                AND sale_date >= ?
        """, [city, cutoff_date]).fetchdf()
    except Exception:
        con.close()
        return _empty
    finally:
        con.close()

    if df.empty:
        return _empty

    total_sales = len(df)

    median_price = float(df["sale_price"].median()) if not df["sale_price"].isna().all() else None

    # price per sqft
    valid_ppsf = df[(df["sqft"].notna()) & (df["sqft"] > 0)].copy()
    valid_ppsf["ppsf"] = valid_ppsf["sale_price"] / valid_ppsf["sqft"]
    median_ppsf = float(valid_ppsf["ppsf"].median()) if not valid_ppsf.empty else None

    median_sqft = float(df["sqft"].median()) if not df["sqft"].isna().all() else None

    # days since sale
    try:
        df["sale_date"] = pd.to_datetime(df["sale_date"], errors="coerce")
        today = pd.Timestamp.today()
        df["days_since"] = (today - df["sale_date"]).dt.days
        avg_days_since_sale = float(df["days_since"].dropna().mean()) if not df["days_since"].isna().all() else None
    except Exception:
        avg_days_since_sale = None

    return {
        "median_price": median_price,
        "median_ppsf": median_ppsf,
        "avg_days_since_sale": avg_days_since_sale,
        "total_sales": total_sales,
        "median_sqft": median_sqft,
    }
