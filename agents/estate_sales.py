"""
Estate Sales Signal Agent
=========================
Scrapes EstateSales.net for estate sales in target neighborhoods.
An estate sale at or near a property address = near-certain imminent home sale.

This catches estate/probate situations even faster than obituaries
because families often schedule the estate sale within 30-60 days of death.

Sources:
  - EstateSales.net (largest estate sale listing site)
  - estatesales.org (secondary)

Signal weights:
  +40 pts: estate sale at the EXACT property address (house is selling)
  +25 pts: estate sale within 500 ft of a tracked property (neighbor may sell too)
  +15 pts: estate sale in same neighborhood / street

Usage:
    python -m agents.estate_sales                      # scan all cities
    python -m agents.estate_sales --city "Blaine, MN"
    python -m agents.estate_sales --zip "55449"
"""

import sys, os, re, json, math, asyncio, time
from datetime import date
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db

ESTATE_DDL = """
CREATE TABLE IF NOT EXISTS estate_sale_signals (
    sale_id        VARCHAR PRIMARY KEY,
    sale_address   VARCHAR,
    sale_city      VARCHAR,
    sale_date      DATE,
    sale_lat       DOUBLE,
    sale_lng       DOUBLE,
    nearby_prop_id VARCHAR,
    nearby_address VARCHAR,
    distance_ft    DOUBLE,
    match_type     VARCHAR,   -- 'exact', 'proximity_500ft', 'same_street'
    signal_pts     INTEGER,
    source_url     VARCHAR,
    detected_at    TIMESTAMP DEFAULT current_timestamp
)
"""

ESTATE_SALES_URL = "https://www.estatesales.net/MN/{city}/{zip}/estate-sales.html"


def haversine_ft(lat1, lng1, lat2, lng2) -> float:
    """Distance in feet between two lat/lng points."""
    R = 20_902_520  # Earth radius in feet
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng/2)**2)
    return R * 2 * math.asin(math.sqrt(a))


async def scrape_estate_sales(page, city: str = "Blaine",
                               state: str = "MN",
                               zip_code: str = "55449") -> list[dict]:
    """Scrape EstateSales.net for upcoming/recent estate sales in an area."""
    results = []

    urls = [
        f"https://www.estatesales.net/{state}/{city.replace(' ','-')}/{zip_code}/estate-sales.html",
        f"https://www.estatesales.net/{state}/{city.replace(' ','-')}/estate-sales.html",
    ]

    for url in urls:
        try:
            await page.goto(url, timeout=20_000)
            await page.wait_for_timeout(3_000)
            text = await page.inner_text("body")

            # EstateSales.net embeds address + date in listing cards
            # Pattern: address, city/state, date range
            addr_pattern = r"(\d+\s+[A-Za-z0-9\s]+(?:St|Ave|Ln|Dr|Ct|Blvd|Rd|Way|Cir|Pl|Ter)\s*(?:NE|NW|SE|SW)?)"
            date_pattern  = r"(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d+"

            addresses = re.findall(addr_pattern, text)
            dates     = re.findall(date_pattern, text)

            for i, addr in enumerate(addresses[:20]):
                results.append({
                    "sale_address": addr.strip(),
                    "sale_city":    city,
                    "sale_date":    str(date.today()),   # approximate
                    "sale_lat":     None,
                    "sale_lng":     None,
                    "source_url":   url,
                    "sale_id":      f"es_{hash(addr)}_{date.today()}",
                })
            if results:
                break
        except Exception:
            continue

    return results


async def match_against_properties(
    estate_sales: list[dict], db_path: str = None
) -> list[dict]:
    """Cross-reference estate sales against our tracked properties."""
    if not estate_sales:
        return []

    con = get_db(db_path, read_only=True)
    props = con.execute(
        "SELECT p.id, p.address, p.lat, p.lng FROM properties p "
        "WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL"
    ).df()
    con.close()

    matches = []
    for sale in estate_sales:
        sale_addr_upper = sale["sale_address"].upper().strip()
        sale_num = sale["sale_address"].split()[0] if sale["sale_address"] else ""

        for _, prop in props.iterrows():
            prop_addr = str(prop["address"] or "").upper()
            prop_num  = prop["address"].split()[0] if prop.get("address") else ""

            # Exact address match
            if sale_num and sale_num == prop_num:
                if any(word in sale_addr_upper for word in prop_addr.split()[1:3]):
                    matches.append({
                        **sale,
                        "nearby_prop_id": prop["id"],
                        "nearby_address": prop["address"],
                        "distance_ft":    0,
                        "match_type":     "exact",
                        "signal_pts":     40,
                    })
                    continue

            # Proximity match if we have coordinates
            if (sale.get("sale_lat") and sale.get("sale_lng") and
                    prop.get("lat") and prop.get("lng")):
                dist = haversine_ft(
                    sale["sale_lat"], sale["sale_lng"],
                    prop["lat"], prop["lng"]
                )
                if dist <= 500:
                    matches.append({
                        **sale,
                        "nearby_prop_id": prop["id"],
                        "nearby_address": prop["address"],
                        "distance_ft":    round(dist),
                        "match_type":     "proximity_500ft",
                        "signal_pts":     25,
                    })

    return matches


async def scan_async(city: str = "Blaine", state: str = "MN",
                      zip_code: str = "55449", db_path: str = None):
    """Full scan: scrape estate sales and match against properties."""
    from playwright.async_api import async_playwright

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx  = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        print(f"Scraping estate sales in {city}, {state} {zip_code}...")
        sales = await scrape_estate_sales(page, city, state, zip_code)
        print(f"Found {len(sales)} estate sales listed")

        matches = await match_against_properties(sales, db_path)

        await browser.close()

    if matches:
        write_con = get_db(db_path)
        write_con.execute(ESTATE_DDL)
        for m in matches:
            write_con.execute("""
                INSERT OR REPLACE INTO estate_sale_signals
                (sale_id, sale_address, sale_city, sale_date,
                 sale_lat, sale_lng, nearby_prop_id, nearby_address,
                 distance_ft, match_type, signal_pts, source_url)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?)
            """, [m["sale_id"], m["sale_address"], m["sale_city"],
                  m["sale_date"], m.get("sale_lat"), m.get("sale_lng"),
                  m["nearby_prop_id"], m["nearby_address"],
                  m["distance_ft"], m["match_type"],
                  m["signal_pts"], m["source_url"]])
        write_con.close()
        print(f"\n{len(matches)} property matches found and saved.")
        for m in matches:
            print(f"  [{m['match_type']}] {m['nearby_address']}: "
                  f"+{m['signal_pts']}pts "
                  f"(estate sale at {m['sale_address']}, {m['distance_ft']}ft away)")
    else:
        print("No property matches found.")

    return matches


def scan(city="Blaine", state="MN", zip_code="55449", db_path=None):
    return asyncio.run(scan_async(city=city, state=state,
                                   zip_code=zip_code, db_path=db_path))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--city",  default="Blaine")
    parser.add_argument("--state", default="MN")
    parser.add_argument("--zip",   default="55449")
    args = parser.parse_args()
    scan(city=args.city, state=args.state, zip_code=args.zip)
