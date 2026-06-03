"""
Anoka County Deed Transfer Tracker
====================================
Searches Anoka County Recorder records for recent deed transfers at
specific property addresses.

Properties transferred via estate/death deeds (Trustee's Deed, Personal
Representative's Deed, Affidavit of Survivorship, Transfer on Death) signal
motivated heir-sellers who often want to liquidate quickly.

Primary URL: https://recorder.anokacounty.us/
Fallback:    https://www.anokacounty.us/recorder

Usage:
    python -m ingestion.deeds
    # or import and call search_deed_transfers(address, city)
"""

import sys, os, re, asyncio
from datetime import datetime, date
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
RECORDER_URLS = [
    "https://recorder.anokacounty.us/",
    "https://www.anokacounty.us/recorder",
    "https://aca-prod.accela.com/ANOKA/",  # Accela may host recorder too
]

# Deed types that strongly indicate estate/death transfer
ESTATE_DEED_TYPES = [
    "Trustee's Deed",
    "Trustee Deed",
    "Personal Representative's Deed",
    "Personal Representative Deed",
    "Affidavit of Survivorship",
    "Transfer on Death",
    "TOD Deed",
    "Quit Claim Deed",          # sometimes used in estate settlements
    "Warranty Deed",            # include for completeness; filter on grantee
    "Court Referee's Deed",
    "Sheriff's Deed",           # foreclosure = distress signal
    "Executor's Deed",
    "Administrator's Deed",
]

ESTATE_KEYWORDS = [
    "estate of",
    "personal representative",
    "trustee",
    "survivor",
    "transfer on death",
    "tod",
    "revocable trust",
    "living trust",
    "pr deed",
]

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


# ---------------------------------------------------------------------------
# Internal: Playwright scraper
# ---------------------------------------------------------------------------
async def _scrape_recorder(address: str, city: str, years_back: int) -> list[dict]:
    """
    Navigate Anoka County Recorder portal and search for deed transfers
    at the given address.
    """
    from playwright.async_api import async_playwright

    deeds: list[dict] = []

    # Parse address
    addr_match = re.match(r"(\d+)\s+(.*)", address.strip())
    if not addr_match:
        return deeds
    house_num  = addr_match.group(1)
    street_raw = addr_match.group(2).strip()

    cutoff_year = date.today().year - years_back

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

        success = False
        for recorder_url in RECORDER_URLS:
            try:
                await page.goto(recorder_url, timeout=20_000)
                await page.wait_for_timeout(2_000)

                # Check if we got a real page (not 404 / redirect)
                title = await page.title()
                if "not found" in title.lower() or "404" in title:
                    continue

                success = True
                print(f"[deeds] Connected to recorder at: {recorder_url}")
                break
            except Exception as e:
                print(f"[deeds] Could not reach {recorder_url}: {e}")
                continue

        if not success:
            print("[deeds] All recorder URLs failed. Returning empty list.")
            await browser.close()
            return deeds

        try:
            # ----------------------------------------------------------------
            # 1. Navigate to the property/document search
            # ----------------------------------------------------------------
            # Common Recorder portal link patterns
            search_patterns = [
                "a:has-text('Search')",
                "a:has-text('Property Search')",
                "a:has-text('Document Search')",
                "a:has-text('Land Records')",
                "a:has-text('Real Estate')",
                "a:has-text('Recorded Documents')",
                "#searchLink",
                ".search-link",
            ]
            for sel in search_patterns:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.click()
                        await page.wait_for_timeout(2_000)
                        break
                except Exception:
                    pass

            # ----------------------------------------------------------------
            # 2. Fill in the address search form
            # ----------------------------------------------------------------
            street_word = street_raw.split()[0]  # e.g. "117th"

            # Try common address field selectors
            addr_field_selectors = [
                "input[name*='address' i]",
                "input[id*='address' i]",
                "input[placeholder*='address' i]",
                "input[placeholder*='street' i]",
                "input[name*='street' i]",
                "input[id*='street' i]",
                "#StreetNumber", "#streetNumber",
                "#txtAddress",
            ]

            # Try combined address input first (single field)
            filled = False
            for sel in addr_field_selectors:
                try:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(address)
                        filled = True
                        break
                except Exception:
                    pass

            # If not filled, try separate house number + street name fields
            if not filled:
                num_selectors  = ["input[id*='Number' i]", "input[name*='Number' i]",
                                   "input[id*='Num' i]", "input[placeholder*='Number' i]"]
                name_selectors = ["input[id*='Name' i]", "input[name*='Name' i]",
                                   "input[placeholder*='Street Name' i]"]

                for sel in num_selectors:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(house_num)
                        break

                for sel in name_selectors:
                    el = await page.query_selector(sel)
                    if el:
                        await el.fill(street_word)
                        break

            # Submit search
            for btn_sel in [
                "button:has-text('Search')",
                "input[type='submit']",
                "a:has-text('Search')",
                "button[type='submit']",
                "#searchButton",
                ".btn-search",
            ]:
                try:
                    el = await page.query_selector(btn_sel)
                    if el:
                        await el.click()
                        break
                except Exception:
                    pass

            await page.wait_for_timeout(3_000)

            # ----------------------------------------------------------------
            # 3. Parse the results
            # ----------------------------------------------------------------
            # Most recorder portals show results in a table
            rows = await page.query_selector_all("table tr, .result-row, .document-row")
            headers: list[str] = []

            for row in rows:
                cells = await row.query_selector_all("td, th")
                cell_texts = [(await c.inner_text()).strip() for c in cells]
                if not cell_texts:
                    continue

                joined = " ".join(cell_texts).lower()

                # Detect header row
                if any(h in joined for h in ["grantor", "grantee", "instrument", "deed type", "record date"]):
                    headers = cell_texts
                    continue

                if not cell_texts or len(cell_texts) < 2:
                    continue

                # Parse the deed row
                deed = _parse_deed_row(cell_texts, headers, cutoff_year)
                if deed:
                    # Filter to rows mentioning our address or street
                    row_str = " ".join(cell_texts).upper()
                    if (house_num in row_str or
                            street_word.upper() in row_str or
                            street_raw.split()[-1].upper() in row_str):
                        deeds.append(deed)

            # If table parsing found nothing, try scanning all text for deed patterns
            if not deeds:
                page_text = await page.inner_text("body")
                deeds.extend(_extract_deeds_from_text(page_text, house_num, street_raw, cutoff_year))

        except Exception as e:
            print(f"[deeds] Recorder scrape error: {e}")
        finally:
            try:
                await browser.close()
            except Exception:
                pass

    return deeds


def _parse_deed_row(cells: list[str], headers: list[str], cutoff_year: int) -> Optional[dict]:
    """Parse a recorder result row into a deed dict."""
    # Build dict from headers if available
    if headers and len(headers) == len(cells):
        row_dict = dict(zip(headers, cells))
    else:
        row_dict = {}

    # Try to extract record_date
    record_date = None
    date_str    = None
    all_vals    = list(row_dict.values()) + cells
    for val in all_vals:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", str(val))
        if m:
            date_str = m.group(1)
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    record_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    pass
            if record_date:
                break

    # Only include deeds within our look-back window
    if record_date and record_date.year < cutoff_year:
        return None

    # Extract instrument/deed type
    instrument_type = ""
    deed_type       = ""
    for key in ["Instrument Type", "Document Type", "Deed Type", "Type", "Doc Type"]:
        val = row_dict.get(key, "")
        if val:
            instrument_type = val
            deed_type = val
            break

    if not deed_type and cells:
        # Heuristic: look for known deed keywords in cell text
        for c in cells:
            c_low = c.lower()
            if any(kw.lower() in c_low for kw in ["deed", "affidavit", "transfer"]):
                deed_type = c
                instrument_type = c
                break

    # Extract grantor / grantee
    grantor = row_dict.get("Grantor", row_dict.get("From", ""))
    grantee = row_dict.get("Grantee", row_dict.get("To", ""))
    if not grantor and len(cells) >= 3:
        grantor = cells[1] if len(cells) > 1 else ""
        grantee = cells[2] if len(cells) > 2 else ""

    return {
        "grantor":         grantor.strip(),
        "grantee":         grantee.strip(),
        "deed_type":       deed_type.strip(),
        "record_date":     record_date.isoformat() if record_date else (date_str or ""),
        "instrument_type": instrument_type.strip(),
    }


def _extract_deeds_from_text(text: str, house_num: str, street_raw: str, cutoff_year: int) -> list[dict]:
    """
    Fallback: scan raw page text for deed patterns near our address.
    """
    deeds = []
    street_word = street_raw.split()[0].upper()

    # Find lines that mention our address or street + contain deed-type keywords
    lines = text.split("\n")
    for i, line in enumerate(lines):
        line_up = line.upper()
        if house_num not in line_up and street_word not in line_up:
            continue

        # Grab context window (surrounding lines)
        context = " ".join(lines[max(0, i-3):i+4]).lower()

        # Look for any estate keyword
        found_type = ""
        for kw in ESTATE_KEYWORDS:
            if kw.lower() in context:
                found_type = kw.title()
                break

        # Try to extract a date from the context
        date_m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2})", context)
        date_str = date_m.group(1) if date_m else ""
        record_date = None
        if date_str:
            for fmt in ("%m/%d/%Y", "%Y-%m-%d"):
                try:
                    record_date = datetime.strptime(date_str, fmt).date()
                    break
                except ValueError:
                    pass

        if record_date and record_date.year < cutoff_year:
            continue

        if found_type:
            deeds.append({
                "grantor":         "",
                "grantee":         "",
                "deed_type":       found_type,
                "record_date":     record_date.isoformat() if record_date else date_str,
                "instrument_type": found_type,
            })

    return deeds


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def search_deed_transfers(
    address: str,
    city: str = "Blaine",
    years_back: int = 3,
) -> list[dict]:
    """
    Search Anoka County Recorder for recent deed transfers at the given address.

    Args:
        address:    Street address, e.g. "3448 117th Ln NE"
        city:       City name, default "Blaine"
        years_back: How many years of history to retrieve (default 3)

    Returns:
        list of dicts: {grantor, grantee, deed_type, record_date, instrument_type}
        Empty list if unavailable.

    Flagged deed types:
        - Trustee's Deed
        - Personal Representative's Deed
        - Affidavit of Survivorship
        - Transfer on Death
    """
    try:
        deeds = asyncio.run(_scrape_recorder(address, city, years_back))
        return deeds
    except Exception as e:
        print(f"[deeds] search_deed_transfers failed for '{address}': {e}")
        return []


def is_estate_transfer(deeds: list[dict]) -> tuple[bool, str]:
    """
    Determine if any recorded deed looks like an estate/death transfer.

    Args:
        deeds: Output from search_deed_transfers()

    Returns:
        (True, "reason string")  if an estate deed is found
        (False, "")              otherwise

    Examples of reason strings:
        "Personal Representative's Deed (2024-03-15)"
        "Trustee's Deed (2023-11-02)"
        "Transfer on Death (2024-01-20)"
    """
    if not deeds:
        return False, ""

    for deed in deeds:
        deed_type_raw = (deed.get("deed_type") or deed.get("instrument_type") or "").lower()
        grantor_raw   = (deed.get("grantor") or "").lower()
        grantee_raw   = (deed.get("grantee") or "").lower()
        combined      = f"{deed_type_raw} {grantor_raw} {grantee_raw}"

        matched_type = ""

        # Check against our flagged deed type list
        for etype in ESTATE_DEED_TYPES:
            if etype.lower() in combined:
                matched_type = etype
                break

        # Check keyword patterns
        if not matched_type:
            for kw in ESTATE_KEYWORDS:
                if kw.lower() in combined:
                    matched_type = kw.title()
                    break

        if matched_type:
            rec_date = deed.get("record_date", "")
            reason   = f"{matched_type} ({rec_date})" if rec_date else matched_type
            return True, reason

    return False, ""


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    test_address = "3448 117th Ln NE"  # Elrod, age 79
    test_city    = "Blaine"

    print(f"[deeds] Searching deed transfers for: {test_address}, {test_city}")
    print("-" * 60)

    results = search_deed_transfers(test_address, test_city, years_back=3)

    if results:
        print(f"Found {len(results)} deed transfer(s):")
        for d in results:
            print(f"  [{d.get('record_date','?')}] {d.get('deed_type','?')}")
            print(f"    Grantor: {d.get('grantor','?')}")
            print(f"    Grantee: {d.get('grantee','?')}")
            print(f"    Instrument: {d.get('instrument_type','?')}")
    else:
        print("No deed transfers found (recorder may be unavailable or no recent transfers).")

    is_estate, reason = is_estate_transfer(results)
    print(f"\nEstate transfer detected: {is_estate}")
    if reason:
        print(f"Reason: {reason}")
