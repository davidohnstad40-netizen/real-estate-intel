"""
Zillow Listing Monitor
======================
Scrapes Zillow for new/recent listings in the target neighborhood.
Runs weekly. Cross-references against tracked properties to detect:
  - A tracked T1/T2/T3 property that just listed (validate our scoring!)
  - New off-market opportunities appearing near our targets
  - Price cuts on tracked properties

Results are stored in zillow_listings table and used to:
  - Auto-upgrade tracked properties to SKIP when they list
  - Validate the scoring model (did high scorers list first?)
  - Surface expired/withdrawn listings as re-engagement targets

Usage:
    python -m ingestion.zillow_monitor          # scrape + store
    python -m ingestion.zillow_monitor --check  # just check for tracked property listings
"""

import sys, os, re, json, asyncio, time
from datetime import datetime, date
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
LISTINGS_DDL = """
CREATE TABLE IF NOT EXISTS zillow_listings (
    zpid         VARCHAR PRIMARY KEY,
    address      VARCHAR,
    city         VARCHAR,
    state        VARCHAR,
    zip          VARCHAR,
    price        DOUBLE,
    beds         INTEGER,
    baths        DOUBLE,
    sqft         INTEGER,
    price_per_sqft DOUBLE,
    days_on_market INTEGER,
    list_date    DATE,
    status       VARCHAR,   -- 'For Sale', 'Pending', 'Sold', 'Off Market'
    zestimate    DOUBLE,
    lat          DOUBLE,
    lng          DOUBLE,
    url          VARCHAR,
    last_seen    TIMESTAMP DEFAULT current_timestamp
)
"""

MATCH_DDL = """
CREATE TABLE IF NOT EXISTS listing_matches (
    property_id    VARCHAR,
    zpid           VARCHAR,
    match_type     VARCHAR,   -- 'exact', 'proximity'
    distance_ft    DOUBLE,
    our_tier       VARCHAR,
    our_score      INTEGER,
    list_price     DOUBLE,
    list_date      DATE,
    detected_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (property_id, zpid)
)
"""

TARGET_URL = (
    "https://www.zillow.com/blaine-mn-55449/houses/"
    "?searchQueryState=%7B%22mapBounds%22%3A%7B%22west%22%3A-93.22%2C%22east%22%3A-93.17%2C"
    "%22south%22%3A45.155%2C%22north%22%3A45.19%7D%2C%22isMapVisible%22%3Atrue%2C"
    "%22filterState%22%3A%7B%22sort%22%3A%7B%22value%22%3A%22days%22%7D%7D%7D"
)


def _ensure_tables(con):
    con.execute(LISTINGS_DDL)
    con.execute(MATCH_DDL)


# ---------------------------------------------------------------------------
# Scraper
# ---------------------------------------------------------------------------
async def _scrape_zillow(url: str, headless: bool = True) -> list[dict]:
    """
    Scrapes Zillow search results page for listings.
    Returns list of listing dicts.
    Zillow embeds listing data in a __NEXT_DATA__ JSON script tag.
    """
    from playwright.async_api import async_playwright

    listings = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1400, "height": 900},
        )
        page = await ctx.new_page()

        try:
            await page.goto(url, timeout=30_000)
            await page.wait_for_timeout(4_000)

            # Try to extract from __NEXT_DATA__ JSON
            raw_json = await page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? el.textContent : null;
                }
            """)

            if raw_json:
                data = json.loads(raw_json)
                # Navigate to listings in the nested structure
                try:
                    results = (
                        data.get("props", {})
                            .get("pageProps", {})
                            .get("searchPageState", {})
                            .get("cat1", {})
                            .get("searchResults", {})
                            .get("listResults", [])
                    )
                    for r in results:
                        listings.append({
                            "zpid":        str(r.get("zpid", "")),
                            "address":     r.get("addressStreet", ""),
                            "city":        r.get("addressCity", "Blaine"),
                            "state":       r.get("addressState", "MN"),
                            "zip":         r.get("addressZipcode", "55449"),
                            "price":       r.get("unformattedPrice"),
                            "beds":        r.get("beds"),
                            "baths":       r.get("baths"),
                            "sqft":        r.get("area"),
                            "price_per_sqft": r.get("hdpData", {}).get("homeInfo", {}).get("pricePerSquareFoot"),
                            "days_on_market": r.get("hdpData", {}).get("homeInfo", {}).get("daysOnZillow"),
                            "status":      r.get("statusText", "For Sale"),
                            "zestimate":   r.get("zestimate"),
                            "lat":         r.get("latLong", {}).get("latitude"),
                            "lng":         r.get("latLong", {}).get("longitude"),
                            "url":         "https://www.zillow.com" + r.get("detailUrl", ""),
                        })
                except Exception:
                    pass

            # Fallback: parse price cards from DOM
            if not listings:
                cards = await page.query_selector_all('[data-test="property-card"]')
                for card in cards[:30]:
                    try:
                        addr = await card.query_selector('[data-test="property-card-addr"]')
                        price_el = await card.query_selector('[data-test="property-card-price"]')
                        addr_text  = await addr.inner_text() if addr else ""
                        price_text = await price_el.inner_text() if price_el else ""
                        price_val  = float(re.sub(r"[^\d]", "", price_text)) if price_text else None
                        listings.append({
                            "zpid": f"dom_{hash(addr_text)}",
                            "address": addr_text.split(",")[0].strip(),
                            "city": "Blaine", "state": "MN", "zip": "55449",
                            "price": price_val, "status": "For Sale",
                            "lat": None, "lng": None, "url": "",
                        })
                    except Exception:
                        pass

        except Exception as e:
            print(f"[zillow_monitor] Scrape error: {e}")
        finally:
            await browser.close()

    return listings


# ---------------------------------------------------------------------------
# Match against tracked properties
# ---------------------------------------------------------------------------
def _match_listings(con, listings: list[dict]) -> list[dict]:
    """Find listings that match (or are near) our tracked properties."""
    import math

    tracked = con.execute("""
        SELECT p.id, p.address, p.lat, p.lng,
               s.knock_tier, s.motivation_score
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL
    """).fetchall()

    matches = []
    for listing in listings:
        l_lat = listing.get("lat")
        l_lng = listing.get("lng")
        l_addr = (listing.get("address") or "").upper().strip()

        for prop_id, p_addr, p_lat, p_lng, tier, score in tracked:
            # Exact address match
            p_addr_short = p_addr.split(",")[0].upper().strip()
            if l_addr and p_addr_short and (l_addr in p_addr_short or p_addr_short in l_addr):
                matches.append({
                    "property_id": prop_id, "zpid": listing["zpid"],
                    "match_type": "exact", "distance_ft": 0,
                    "our_tier": tier, "our_score": score,
                    "list_price": listing.get("price"),
                    "list_date": date.today(),
                    "address": p_addr,
                })
                continue

            # Proximity match (<300 ft)
            if l_lat and l_lng and p_lat and p_lng:
                dist_deg = math.sqrt((l_lat-p_lat)**2 + ((l_lng-p_lng)*math.cos(math.radians(p_lat)))**2)
                dist_ft  = dist_deg * 364_000  # approx ft per degree at this latitude
                if dist_ft < 300:
                    matches.append({
                        "property_id": prop_id, "zpid": listing["zpid"],
                        "match_type": "proximity", "distance_ft": round(dist_ft),
                        "our_tier": tier, "our_score": score,
                        "list_price": listing.get("price"),
                        "list_date": date.today(),
                        "address": p_addr,
                    })

    return matches


# ---------------------------------------------------------------------------
# Main entry points
# ---------------------------------------------------------------------------
def run_monitor(db_path: str = None, headless: bool = True) -> dict:
    """
    Scrape Zillow, store listings, cross-reference tracked properties.
    Returns summary dict.
    """
    con = get_db(db_path)
    _ensure_tables(con)

    print("[zillow_monitor] Scraping Zillow listings...")
    listings = asyncio.run(_scrape_zillow(TARGET_URL, headless=headless))
    print(f"[zillow_monitor] Found {len(listings)} listings")

    # Store listings
    stored = 0
    for l in listings:
        if not l.get("zpid"): continue
        try:
            con.execute("""
                INSERT OR REPLACE INTO zillow_listings
                (zpid, address, city, state, zip, price, beds, baths, sqft,
                 price_per_sqft, days_on_market, status, zestimate, lat, lng,
                 url, last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)
            """, [l.get("zpid"), l.get("address"), l.get("city","Blaine"),
                  l.get("state","MN"), l.get("zip","55449"),
                  l.get("price"), l.get("beds"), l.get("baths"), l.get("sqft"),
                  l.get("price_per_sqft"), l.get("days_on_market"),
                  l.get("status","For Sale"), l.get("zestimate"),
                  l.get("lat"), l.get("lng"), l.get("url","")])
            stored += 1
        except Exception: pass

    # Find matches
    matches = _match_listings(con, listings)
    new_matches = []
    for m in matches:
        try:
            con.execute("""
                INSERT OR IGNORE INTO listing_matches
                (property_id, zpid, match_type, distance_ft, our_tier,
                 our_score, list_price, list_date)
                VALUES (?,?,?,?,?,?,?,?)
            """, [m["property_id"], m["zpid"], m["match_type"],
                  m["distance_ft"], m["our_tier"], m["our_score"],
                  m["list_price"], m["list_date"]])
            if con.execute("SELECT changes()").fetchone()[0] > 0:
                new_matches.append(m)
        except Exception: pass

    # Auto-update SKIP tier for exact matches
    for m in new_matches:
        if m["match_type"] == "exact":
            con.execute("""
                UPDATE property_scores SET knock_tier='SKIP', updated_at=current_timestamp
                WHERE id=?
            """, [m["property_id"]])
            print(f"  [!] LISTED: {m['address']} (was {m['our_tier']}, score={m['our_score']}) "
                  f"at ${m['list_price']:,.0f}")

    con.close()

    result = {
        "listings_found": len(listings),
        "listings_stored": stored,
        "matches": new_matches,
        "new_listings_on_tracked": [m for m in new_matches if m["match_type"]=="exact"],
    }
    print(f"[zillow_monitor] Done. {stored} stored, {len(new_matches)} new matches.")
    return result


def get_recent_listings(db_path: str = None, days: int = 30) -> list[dict]:
    """Get listings scraped in the last N days."""
    con = get_db(db_path, read_only=True)
    try:
        _ensure_tables(con)
        rows = con.execute(f"""
            SELECT address, price, beds, baths, sqft, days_on_market,
                   status, last_seen, url
            FROM zillow_listings
            WHERE last_seen >= current_date - INTERVAL '{days} days'
            ORDER BY last_seen DESC
        """).df()
        return rows.to_dict("records")
    finally:
        con.close()


def get_tracked_listings(db_path: str = None) -> list[dict]:
    """Get listing_matches where one of our tracked properties appeared on Zillow."""
    con = get_db(db_path, read_only=True)
    try:
        _ensure_tables(con)
        rows = con.execute("""
            SELECT m.*, p.address as tracked_address, z.url, z.price as list_price,
                   z.days_on_market
            FROM listing_matches m
            LEFT JOIN properties p ON m.property_id = p.id
            LEFT JOIN zillow_listings z ON m.zpid = z.zpid
            ORDER BY m.detected_at DESC
        """).df()
        return rows.to_dict("records")
    finally:
        con.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Check existing data only")
    parser.add_argument("--visible", action="store_true", help="Show browser window")
    args = parser.parse_args()

    if args.check:
        matches = get_tracked_listings()
        if matches:
            print(f"\n*** {len(matches)} tracked property listing matches found: ***")
            for m in matches:
                print(f"  {m.get('tracked_address')} listed at ${m.get('list_price',0):,.0f} "
                      f"(was {m.get('our_tier')}, score={m.get('our_score')})")
        else:
            print("No tracked properties found on Zillow yet.")
    else:
        result = run_monitor(headless=not args.visible)
        if result["new_listings_on_tracked"]:
            print("\n*** ALERT: Tracked properties now on market! ***")
            for m in result["new_listings_on_tracked"]:
                print(f"  {m['address']} - ${m['list_price']:,.0f}")
