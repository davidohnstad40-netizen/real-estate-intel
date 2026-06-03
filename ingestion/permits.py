"""
Anoka County Building Permit Scraper
=====================================
Searches Anoka County's Accela Citizen Access portal for building permits
at specific property addresses.

Recent renovation permits (12-36 months before today) signal a seller
preparing to sell -- a strong motivated-seller indicator.

Usage:
    python -m ingestion.permits
    # or import and call check_permits(address, city)
"""

import sys, os, re, asyncio
from datetime import datetime, date, timedelta
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ACCELA_URL     = "https://aca-prod.accela.com/ANOKA/"
ANOKA_PLANNING = "https://www.anokacounty.us/planning/permits"
TODAY          = date.today()

MAJOR_PERMIT_KEYWORDS = [
    "kitchen", "bath", "bathroom", "addition", "roof", "roofing",
    "basement", "finish", "remodel", "renovation", "hvac", "electrical",
    "plumbing", "window", "door", "garage", "deck", "egress", "foundation",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Internal: Playwright scraper
# ---------------------------------------------------------------------------
async def _scrape_accela(address: str, city: str) -> list[dict]:
    """
    Try to navigate Accela Citizen Access for Anoka County and search
    for permits at the given address.  Returns raw permit dicts.
    """
    from playwright.async_api import async_playwright

    permits = []
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 900},
        )
        page = await ctx.new_page()

        try:
            # ----------------------------------------------------------------
            # 1. Load Accela Citizen Access landing page
            # ----------------------------------------------------------------
            await page.goto(ACCELA_URL, timeout=20_000)
            await page.wait_for_timeout(2_000)

            # Look for "Building" or "Permits" section / navigation link
            # Accela's layout varies by jurisdiction but usually has a nav bar
            for selector in [
                "a:has-text('Building')",
                "a:has-text('Permits')",
                "a:has-text('Search')",
                "#A6GovMenu_MenuButton_Building",
                ".A6-global-nav a",
            ]:
                try:
                    el = await page.query_selector(selector)
                    if el:
                        await el.click()
                        await page.wait_for_timeout(1_500)
                        break
                except Exception:
                    pass

            # ----------------------------------------------------------------
            # 2. Try to reach the permit search form
            # ----------------------------------------------------------------
            # Some Accela instances expose a direct URL for permit search
            permit_search_urls = [
                f"{ACCELA_URL}Cap/CapHome.aspx?module=Building&TabName=Building",
                f"{ACCELA_URL}Search/GenericSearch.aspx?module=Building",
                f"{ACCELA_URL}Cap/CapSearch.aspx",
            ]

            form_found = False
            for url in permit_search_urls:
                try:
                    await page.goto(url, timeout=15_000)
                    await page.wait_for_timeout(1_500)
                    # Check if a search form is present
                    for field in ["#ctl00_PlaceHolderMain_generalSearchForm_txtGSStreetName",
                                  "input[name*='StreetName']",
                                  "input[placeholder*='Street']",
                                  "input[id*='Street']"]:
                        el = await page.query_selector(field)
                        if el:
                            form_found = True
                            break
                    if form_found:
                        break
                except Exception:
                    continue

            if not form_found:
                # Try the county planning page as fallback
                await page.goto(ANOKA_PLANNING, timeout=15_000)
                await page.wait_for_timeout(2_000)

            # ----------------------------------------------------------------
            # 3. Fill out the address search form
            # ----------------------------------------------------------------
            # Parse the address: "3316 117th Ln NE" -> number + street name
            addr_match = re.match(r"(\d+)\s+(.*)", address.strip())
            if not addr_match:
                return permits
            house_num  = addr_match.group(1)
            street_raw = addr_match.group(2).strip()

            # Try various field selectors for Accela street number / name
            num_selectors = [
                "#ctl00_PlaceHolderMain_generalSearchForm_txtGSStreetNo",
                "input[name*='StreetNo']",
                "input[id*='StreetNo']",
                "input[placeholder*='Number']",
            ]
            name_selectors = [
                "#ctl00_PlaceHolderMain_generalSearchForm_txtGSStreetName",
                "input[name*='StreetName']",
                "input[id*='StreetName']",
                "input[placeholder*='Street Name']",
            ]

            for sel in num_selectors:
                el = await page.query_selector(sel)
                if el:
                    await el.fill(house_num)
                    break

            for sel in name_selectors:
                el = await page.query_selector(sel)
                if el:
                    # Use just the first word of the street name for broader matching
                    await el.fill(street_raw.split()[0])
                    break

            # Submit the form
            for btn_sel in [
                "#ctl00_PlaceHolderMain_btnNewSearch",
                "input[value='Search']",
                "button:has-text('Search')",
                "a:has-text('Search')",
            ]:
                el = await page.query_selector(btn_sel)
                if el:
                    await el.click()
                    break

            await page.wait_for_timeout(3_000)

            # ----------------------------------------------------------------
            # 4. Parse results table
            # ----------------------------------------------------------------
            # Accela results are typically in a table with class "ACA_Grid_Caption"
            # or similar. Extract rows.
            rows = await page.query_selector_all("table.ACA_Grid_Caption tr, table.aca-grid tr, #ctl00_PlaceHolderMain_dgvPermitList tr")
            if not rows:
                rows = await page.query_selector_all("tr")

            headers: list[str] = []
            for row in rows:
                cells = await row.query_selector_all("td, th")
                cell_texts = []
                for c in cells:
                    cell_texts.append((await c.inner_text()).strip())

                if not cell_texts:
                    continue

                # Detect header row
                joined = " ".join(cell_texts).lower()
                if ("permit" in joined or "record" in joined) and ("date" in joined or "type" in joined):
                    headers = cell_texts
                    continue

                if headers and len(cell_texts) >= 2:
                    row_dict = dict(zip(headers, cell_texts))
                    # Look for address match in the row
                    row_str = " ".join(cell_texts).upper()
                    if house_num in row_str or street_raw.split()[0].upper() in row_str:
                        # Try to find issue_date, permit_type, status, value
                        permit = _parse_permit_row(row_dict, cell_texts)
                        if permit:
                            permits.append(permit)

        except Exception as e:
            print(f"[permits] Accela scrape error: {e}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return permits


def _parse_permit_row(row_dict: dict, cells: list[str]) -> Optional[dict]:
    """
    Attempt to extract permit fields from a row dict or raw cell list.
    Returns a normalized permit dict or None if parsing fails.
    """
    # Try to find date in any cell
    date_str = None
    for val in list(row_dict.values()) + cells:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", str(val))
        if m:
            date_str = m.group(1)
            break

    # Try to parse the date
    issue_date = None
    if date_str:
        for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
            try:
                issue_date = datetime.strptime(date_str, fmt).date()
                break
            except ValueError:
                pass

    # Extract description / type from the row
    description = ""
    permit_type = ""
    for key in ["Permit Type", "Record Type", "Type", "Description", "Work Description"]:
        val = row_dict.get(key, "")
        if val:
            if not permit_type:
                permit_type = val
            description += " " + val

    if not permit_type and cells:
        # Heuristic: second non-date cell is often the type
        for c in cells:
            if c and not re.search(r"\d{1,2}/\d{1,2}/\d{4}", c):
                permit_type = c
                break

    # Status
    status = ""
    for key in ["Status", "Record Status"]:
        val = row_dict.get(key, "")
        if val:
            status = val
            break

    # Value
    value = None
    for key in ["Valuation", "Value", "Job Value", "Permit Value"]:
        val = row_dict.get(key, "")
        if val:
            clean = re.sub(r"[^\d.]", "", val)
            if clean:
                try:
                    value = float(clean)
                except ValueError:
                    pass
            break

    return {
        "permit_type":  permit_type.strip(),
        "issue_date":   issue_date.isoformat() if issue_date else date_str or "",
        "description":  description.strip(),
        "status":       status.strip(),
        "value":        value,
    }


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def check_permits(address: str, city: str = "Blaine") -> list[dict]:
    """
    Search for building permits at the given address via Anoka County's
    Accela Citizen Access portal.

    Args:
        address: Street address, e.g. "3316 117th Ln NE"
        city:    City name, default "Blaine"

    Returns:
        list of dicts: {permit_type, issue_date, description, status, value}
        Empty list if no permits found or site unavailable.
    """
    try:
        permits = asyncio.run(_scrape_accela(address, city))
        return permits
    except Exception as e:
        print(f"[permits] check_permits failed for '{address}': {e}")
        return []


def score_permit_signal(permits: list[dict]) -> int:
    """
    Score permit activity as a motivated-seller signal.

    Scoring:
        12-15 pts  Major renovation in last 24 months (kitchen/bath/roof/addition/HVAC)
        6-11  pts  Any permit in last 24 months
        3-5   pts  Any permit 24-48 months ago
        0     pts  No permits, empty list, or all permits > 4 years ago

    Returns:
        Integer score 0-15.
    """
    if not permits:
        return 0

    cutoff_24mo = TODAY - timedelta(days=730)
    cutoff_48mo = TODAY - timedelta(days=1460)

    best_score = 0

    for p in permits:
        # Parse issue_date
        raw_date = p.get("issue_date", "")
        issue_date: Optional[date] = None
        if raw_date:
            for fmt in ("%Y-%m-%d", "%m/%d/%Y"):
                try:
                    issue_date = datetime.strptime(raw_date, fmt).date()
                    break
                except (ValueError, TypeError):
                    pass

        if issue_date is None:
            continue

        # Check if it's a major renovation permit
        combined = (
            (p.get("permit_type") or "") + " " +
            (p.get("description") or "")
        ).lower()

        is_major = any(kw in combined for kw in MAJOR_PERMIT_KEYWORDS)

        if issue_date >= cutoff_24mo:
            pts = 15 if is_major else 8
        elif issue_date >= cutoff_48mo:
            pts = 4
        else:
            pts = 0

        if pts > best_score:
            best_score = pts

    return best_score


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_address = "3316 117th Ln NE"
    test_city    = "Blaine"

    print(f"[permits] Searching permits for: {test_address}, {test_city}")
    print("-" * 60)

    results = check_permits(test_address, test_city)

    if results:
        print(f"Found {len(results)} permit(s):")
        for p in results:
            print(f"  [{p.get('issue_date','?')}] {p.get('permit_type','?')} "
                  f"-- {p.get('description','')[:60]} | status={p.get('status','')} "
                  f"| value=${p.get('value') or 0:,.0f}")
    else:
        print("No permits found (site may be unavailable or no permits on record).")

    score = score_permit_signal(results)
    print(f"\nPermit signal score: {score} / 15")
