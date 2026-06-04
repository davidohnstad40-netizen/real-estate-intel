"""
Obituary Signal Agent
=====================
Scrapes Legacy.com and local newspaper obituaries to find property owners
who have recently passed away. A deceased homeowner = estate situation =
heirs want to liquidate (often quickly and below market).

This is the fastest way to find probate situations BEFORE they file in court.
MCRO probate filings can take 3-6 months after death; obituaries appear within days.

Data sources (all public):
  - Legacy.com: https://www.legacy.com/obituaries/name/
  - Star Tribune obituaries
  - Pioneer Press obituaries
  - Anoka County Union obituaries

Signal weight:
  +35 pts: exact address match in obituary
  +25 pts: same last name at property address in same city (probable match)
  +15 pts: same last name in same city (possible match -- verify)

Usage:
    python -m agents.obituaries                  # scan T1/T2 owner names
    python -m agents.obituaries --city "Blaine"  # scan specific city
    python -m agents.obituaries --years 2        # look back 2 years
"""

import sys, os, re, json, asyncio, time
from datetime import date, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db

OBIT_DDL = """
CREATE TABLE IF NOT EXISTS obituary_signals (
    id              VARCHAR,
    owner_name      VARCHAR,
    deceased_name   VARCHAR,
    obit_date       DATE,
    obit_source     VARCHAR,
    obit_city       VARCHAR,
    match_type      VARCHAR,   -- 'exact_address', 'last_name_city', 'name_city'
    signal_pts      INTEGER,
    survivors       TEXT,
    obit_url        VARCHAR,
    raw_snippet     TEXT,
    detected_at     TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (id, obit_date)
)
"""

MATCH_WEIGHTS = {
    "exact_address": 35,
    "last_name_city": 25,
    "name_city":      15,
}


async def search_legacy(page, last_name: str, city: str = "Blaine",
                         state: str = "MN", years_back: int = 2) -> list[dict]:
    """Search Legacy.com for obituaries matching last name in a city."""
    url = (f"https://www.legacy.com/obituaries/name/{last_name.lower()}-obituaries"
           f"?countryid=1&stateid=24&city={city.replace(' ','+')}%2C+{state}")

    results = []
    try:
        await page.goto(url, timeout=20_000)
        await page.wait_for_timeout(3_000)
        text = await page.inner_text("body")

        # Legacy.com listings contain: Name, Date, City
        # Look for matches
        lines = text.splitlines()
        cutoff = date.today() - timedelta(days=years_back * 365)

        for i, line in enumerate(lines):
            if last_name.lower() in line.lower():
                snippet = " ".join(lines[max(0,i-2):i+5])
                # Extract date if present
                date_match = re.search(r"(\d{4})\s*[-–]\s*(\d{4})", snippet)
                if date_match:
                    death_year = int(date_match.group(2))
                    if death_year < (date.today().year - years_back):
                        continue   # too old

                # Extract city
                city_match = re.search(r"\b" + re.escape(city) + r"\b",
                                        snippet, re.IGNORECASE)
                survivors  = re.findall(r"survived by[^.]+\.", snippet, re.IGNORECASE)

                if city_match:
                    results.append({
                        "deceased_name": line.strip()[:80],
                        "obit_date":     f"{death_year if date_match else '?'}-01-01",
                        "obit_city":     city,
                        "obit_source":   "legacy.com",
                        "obit_url":      url,
                        "survivors":     survivors[0][:200] if survivors else "",
                        "raw_snippet":   snippet[:400],
                    })
    except Exception as e:
        pass
    return results


async def search_star_tribune(page, last_name: str, city: str = "Blaine") -> list[dict]:
    """Search Star Tribune obituaries."""
    url = f"https://www.startribune.com/obituaries/?search={last_name}&region={city}"
    results = []
    try:
        await page.goto(url, timeout=15_000)
        await page.wait_for_timeout(2_000)
        text = await page.inner_text("body")

        if last_name.lower() in text.lower() and city.lower() in text.lower():
            lines = [l for l in text.splitlines() if last_name.lower() in l.lower()]
            for line in lines[:3]:
                results.append({
                    "deceased_name": line.strip()[:80],
                    "obit_date":     str(date.today()),
                    "obit_city":     city,
                    "obit_source":   "startribune.com",
                    "obit_url":      url,
                    "survivors":     "",
                    "raw_snippet":   line[:300],
                })
    except Exception:
        pass
    return results


def classify_match(obit: dict, owner_last: str, address: str, city: str) -> tuple[str, int]:
    """Classify the match type and return (match_type, signal_pts)."""
    snippet = (obit.get("raw_snippet","") + " " + obit.get("survivors","")).lower()
    addr_num = address.split()[0] if address else ""

    # Exact address match in obituary text
    if addr_num and addr_num in snippet:
        return "exact_address", MATCH_WEIGHTS["exact_address"]

    # Last name + city match
    if owner_last.lower() in obit.get("deceased_name","").lower():
        return "last_name_city", MATCH_WEIGHTS["last_name_city"]

    return "name_city", MATCH_WEIGHTS["name_city"]


async def scan_async(city: str = "Blaine", state: str = "MN",
                      years_back: int = 2, db_path: str = None) -> list[dict]:
    """
    Scan obituaries for all T1/T2 owner last names in a city.
    Returns list of signal matches.
    """
    from playwright.async_api import async_playwright

    con = get_db(db_path, read_only=True)
    owners = con.execute(
        "SELECT p.id, p.owner_name, p.address FROM properties p "
        "LEFT JOIN property_scores s ON p.id=s.id "
        "WHERE (s.knock_tier IN ('T1','T2','T3') OR s.knock_tier IS NULL) "
        "AND p.owner_name IS NOT NULL AND p.owner_name NOT LIKE '%LLC%' "
        "AND p.owner_name NOT LIKE '%Trust%' AND p.city ILIKE ?"
        "ORDER BY s.motivation_score DESC NULLS LAST LIMIT 200",
        [city]
    ).fetchall()
    con.close()

    # Get unique last names (avoid redundant searches)
    last_names = {}
    for prop_id, owner_name, address in owners:
        parts = (owner_name or "").strip().split()
        last = parts[-1] if parts else ""
        if len(last) >= 3 and last not in ("Jr", "Sr", "II", "III"):
            if last not in last_names:
                last_names[last] = []
            last_names[last].append((prop_id, owner_name, address))

    print(f"Searching obituaries for {len(last_names)} unique last names in {city}...")
    matches = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
        ctx  = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        for last_name, props in list(last_names.items())[:50]:  # cap at 50 names
            obits = await search_legacy(page, last_name, city, state, years_back)
            if not obits:
                obits = await search_star_tribune(page, last_name, city)
            await asyncio.sleep(1.5)

            for obit in obits:
                for prop_id, owner_name, address in props:
                    match_type, pts = classify_match(obit, last_name, address, city)

                    print(f"  MATCH [{match_type}] {owner_name} @ {address}: "
                          f"{obit.get('deceased_name','')} +{pts}pts")

                    matches.append({
                        "property_id":  prop_id,
                        "owner_name":   owner_name,
                        "address":      address,
                        "match_type":   match_type,
                        "signal_pts":   pts,
                        **{k: obit[k] for k in obit if k in
                           ("deceased_name","obit_date","obit_source",
                            "obit_city","survivors","obit_url","raw_snippet")},
                    })

        await browser.close()

    # Save matches
    if matches:
        write_con = get_db(db_path)
        write_con.execute(OBIT_DDL)
        for m in matches:
            write_con.execute("""
                INSERT OR REPLACE INTO obituary_signals
                (id, owner_name, deceased_name, obit_date, obit_source, obit_city,
                 match_type, signal_pts, survivors, obit_url, raw_snippet)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
            """, [m["property_id"], m["owner_name"], m.get("deceased_name",""),
                  m.get("obit_date",""), m.get("obit_source",""),
                  m.get("obit_city",""), m["match_type"], m["signal_pts"],
                  m.get("survivors",""), m.get("obit_url",""),
                  m.get("raw_snippet","")[:500]])
        write_con.close()

    print(f"\n{len(matches)} obituary matches found.")
    return matches


def scan(city: str = "Blaine", state: str = "MN",
          years_back: int = 2, db_path: str = None):
    """Sync wrapper."""
    return asyncio.run(scan_async(city=city, state=state,
                                   years_back=years_back, db_path=db_path))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--city",  default="Blaine")
    parser.add_argument("--state", default="MN")
    parser.add_argument("--years", type=int, default=2)
    args = parser.parse_args()
    scan(city=args.city, state=args.state, years_back=args.years)
