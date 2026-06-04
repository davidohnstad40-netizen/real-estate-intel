"""
LinkedIn Job Change Signal
==========================
Searches LinkedIn for people in our target area who recently changed jobs
to an employer in a different city/metro. Job relocation = near-certain seller.

This is the single strongest off-market signal that public data provides.

Public data only: LinkedIn profiles and activity feeds are public by design.
People intentionally broadcast job changes to their network.

Usage:
    python -m agents.linkedin_jobs                    # checks all T1/T2 owners
    python -m agents.linkedin_jobs --name "John Smith" --city "Blaine"

Scoring:
    +35 pts: out-of-state job change (definitive relocation)
    +25 pts: out-of-metro job change (probable relocation)
    +15 pts: job change detected, same metro (possible remote / lifestyle change)

Note on access: LinkedIn requires a logged-in session. This module uses
Playwright with the user's existing Chrome session via CDP (remote debugging).
If Chrome is running with --remote-debugging-port=9222, it will connect to it.
Otherwise it launches a visible browser (user logs in manually once).
"""

import sys, os, re, json, asyncio, time
from typing import Optional
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db

# Twin Cities metro area employers (we consider same metro = lower signal)
TWIN_CITIES_CITIES = {
    "Minneapolis", "Saint Paul", "St. Paul", "Bloomington", "Plymouth",
    "Maple Grove", "Eagan", "Eden Prairie", "Woodbury", "Lakeville",
    "Blaine", "Coon Rapids", "Burnsville", "Apple Valley", "Minnetonka",
    "Edina", "Richfield", "St. Louis Park", "Hopkins", "Fridley",
    "Brooklyn Park", "Brooklyn Center", "Roseville", "Maplewood",
    "Columbia Heights", "Anoka", "Champlin", "Ramsey", "Andover",
    "Ham Lake", "Lino Lakes", "White Bear Lake", "Stillwater",
    "Chaska", "Shakopee", "Prior Lake", "Savage", "Chanhassen",
}
TWIN_CITIES_STATES = {"MN", "Minnesota"}

JOB_SIGNAL_DDL = """
CREATE TABLE IF NOT EXISTS linkedin_signals (
    property_id   VARCHAR,
    owner_name    VARCHAR,
    linkedin_url  VARCHAR,
    current_title VARCHAR,
    current_company VARCHAR,
    new_city      VARCHAR,
    new_state     VARCHAR,
    detected_date DATE DEFAULT current_date,
    signal_pts    INTEGER,
    signal_reason TEXT,
    raw_snippet   TEXT,
    PRIMARY KEY (property_id, detected_date)
)
"""


def classify_relocation(new_city: str, new_state: str) -> tuple[int, str]:
    """Return (signal_pts, reason) based on job location."""
    if not new_city and not new_state:
        return 15, "Job change detected (location unclear)"

    if new_state and new_state not in TWIN_CITIES_STATES:
        return 35, f"Out-of-state job change -> {new_city}, {new_state}"

    if new_city and new_city not in TWIN_CITIES_CITIES:
        return 25, f"Out-of-metro job change -> {new_city}"

    return 15, f"Same-metro job change -> {new_city or 'unknown'}"


def _extract_location(text: str) -> tuple[str, str]:
    """Extract city, state from text like 'Chicago, IL' or 'Remote - Denver, CO'."""
    patterns = [
        r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*([A-Z]{2})\b",  # City, ST
        r"([A-Z][a-z]+(?:\s[A-Z][a-z]+)*),\s*(Minnesota|California|Illinois|Texas|Florida|New York)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return m.group(1), m.group(2)
    return "", ""


async def search_linkedin_for_person(
    page, first_name: str, last_name: str, city: str = "Blaine MN"
) -> Optional[dict]:
    """
    Search LinkedIn for a person by name + city, check for recent job changes.
    Returns dict with signal info or None.
    """
    query = f"{first_name} {last_name} {city}"
    url   = f"https://www.linkedin.com/search/results/people/?keywords={query.replace(' ', '%20')}"

    try:
        await page.goto(url, timeout=20_000)
        await page.wait_for_timeout(3_000)

        # Get page text to look for the person
        text = await page.inner_text("body")

        # Look for "Started new position" or job change indicators
        job_change_patterns = [
            r"started\s+(?:a\s+)?new\s+(?:position|role|job)\s+(?:at|with)\s+([^\n]+)",
            r"is\s+now\s+(?:a|an)?\s+[^.]+\s+at\s+([^\n]+)",
            r"joined\s+([^\n]+)\s+as\s+",
        ]

        for pat in job_change_patterns:
            m = re.search(pat, text, re.IGNORECASE)
            if m:
                company_text = m.group(1)[:100]
                new_city, new_state = _extract_location(text)

                # Find LinkedIn profile URL if visible
                profile_url = ""
                links = await page.query_selector_all("a[href*='/in/']")
                for link in links[:3]:
                    href = await link.get_attribute("href")
                    if href and "/in/" in href:
                        profile_url = href
                        break

                pts, reason = classify_relocation(new_city, new_state)
                return {
                    "company_text": company_text,
                    "new_city":     new_city,
                    "new_state":    new_state,
                    "profile_url":  profile_url,
                    "signal_pts":   pts,
                    "signal_reason": reason,
                    "raw_snippet":  text[:500],
                }
    except Exception as e:
        pass
    return None


async def check_owners_async(property_ids: list[str] = None,
                               db_path: str = None) -> list[dict]:
    """
    Check LinkedIn for all T1/T2 property owners.
    Requires LinkedIn to be accessible (will open browser for login if needed).
    """
    from playwright.async_api import async_playwright
    from ingestion.metrogis import _parse_name

    con = get_db(db_path if db_path else None, read_only=True)
    con.execute(JOB_SIGNAL_DDL.replace("CREATE TABLE IF NOT EXISTS",
                                        "CREATE TABLE IF NOT EXISTS"))
    # Load targets
    if property_ids:
        placeholders = ",".join("?" * len(property_ids))
        targets = con.execute(
            f"SELECT p.id, p.owner_name, p.city FROM properties p "
            f"LEFT JOIN property_scores s ON p.id=s.id "
            f"WHERE p.id IN ({placeholders}) AND p.owner_name IS NOT NULL",
            property_ids
        ).fetchall()
    else:
        targets = con.execute(
            "SELECT p.id, p.owner_name, p.city FROM properties p "
            "LEFT JOIN property_scores s ON p.id=s.id "
            "WHERE s.knock_tier IN ('T1','T2') AND p.owner_name IS NOT NULL "
            "AND p.owner_name NOT LIKE '%LLC%' AND p.owner_name NOT LIKE '%Trust%' "
            "ORDER BY s.motivation_score DESC"
        ).fetchall()
    con.close()

    print(f"Checking LinkedIn for {len(targets)} property owners...")
    results = []

    async with async_playwright() as pw:
        # Try to connect to existing Chrome first (if running with --remote-debugging-port=9222)
        try:
            browser = await pw.chromium.connect_over_cdp("http://localhost:9222")
            print("Connected to existing Chrome browser")
        except Exception:
            # Launch a new visible browser -- user will need to log in to LinkedIn
            print("Launching new browser -- please log in to LinkedIn if prompted")
            browser = await pw.chromium.launch(headless=False, slow_mo=500)

        ctx  = await browser.new_context()
        page = await ctx.new_page()

        # Navigate to LinkedIn first to check login status
        await page.goto("https://www.linkedin.com/feed/", timeout=15_000)
        await page.wait_for_timeout(2_000)

        if "login" in page.url.lower() or "sign-in" in page.url.lower():
            print("Not logged in to LinkedIn. Please log in now...")
            print("Waiting up to 60 seconds for you to log in...")
            for _ in range(60):
                await page.wait_for_timeout(1_000)
                if "feed" in page.url.lower():
                    print("Logged in successfully!")
                    break

        for prop_id, owner_name, city in targets:
            first, last = _parse_name(owner_name or "")
            if not first or not last:
                continue

            print(f"  Searching: {first} {last} ({city})...", end=" ", flush=True)
            result = await search_linkedin_for_person(page, first, last, city or "Blaine MN")

            if result:
                print(f"JOB CHANGE DETECTED! {result['signal_pts']}pts - {result['signal_reason']}")
                results.append({"property_id": prop_id, "owner_name": owner_name,
                                 **result})

                # Save to DB
                write_con = get_db(db_path)
                write_con.execute(JOB_SIGNAL_DDL)
                write_con.execute("""
                    INSERT OR REPLACE INTO linkedin_signals
                    (property_id, owner_name, linkedin_url, current_company,
                     new_city, new_state, signal_pts, signal_reason, raw_snippet)
                    VALUES (?,?,?,?,?,?,?,?,?)
                """, [prop_id, owner_name, result["profile_url"],
                      result["company_text"], result["new_city"], result["new_state"],
                      result["signal_pts"], result["signal_reason"],
                      result["raw_snippet"][:500]])
                write_con.close()
            else:
                print("no signal")

            await asyncio.sleep(3)  # respectful rate limiting

        await browser.close()

    return results


def check_owners(property_ids: list[str] = None, db_path: str = None):
    """Sync wrapper."""
    return asyncio.run(check_owners_async(property_ids=property_ids, db_path=db_path))


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", help="Single person: 'First Last'")
    parser.add_argument("--city", default="Blaine MN")
    args = parser.parse_args()

    if args.name:
        parts = args.name.split()
        first = parts[0]; last = " ".join(parts[1:])
        async def single():
            from playwright.async_api import async_playwright
            async with async_playwright() as pw:
                browser = await pw.chromium.launch(headless=False)
                page    = await (await browser.new_context()).new_page()
                await page.goto("https://www.linkedin.com")
                print("Log in to LinkedIn, then press Enter...")
                input()
                result = await search_linkedin_for_person(page, first, last, args.city)
                print(json.dumps(result, indent=2) if result else "No signal found")
                await browser.close()
        asyncio.run(single())
    else:
        results = check_owners()
        print(f"\n{len(results)} job-change signals found across T1/T2 owners.")
