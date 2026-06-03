"""
ingestion/mcro_fixed.py

Fixed MCRO Playwright scraper using the JavaScript React native setter trick.

The fix: React-controlled inputs ignore direct .value assignments.
We must call the native HTMLInputElement value setter so React's synthetic
event system sees the change, then dispatch input + change events.

Results are saved to data/mcro_results_v2.json.

Usage:
    python ingestion/mcro_fixed.py
"""
import asyncio
import json
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------------------
# Search list: (last_name, first_name, address)
# ---------------------------------------------------------------------------
SEARCHES = [
    ("Larson",      "Leslie",       "3200 117th Ln NE"),
    ("Kleinjan",    "Ryan",         "3201 117th Ln NE"),
    ("Badzinski",   "David",        "3167 117th Ln NE"),
    ("Canfield",    "Amanda",       "3223 117th Ln NE"),
    ("Gentilini",   "Paul",         "3224 117th Ln NE"),
    ("Broder",      "Gregory",      "3247 117th Ln NE"),
    ("Marra",       "Keith",        "3260 117th Ln NE"),
    ("Maier",       "William",      "3272 117th Ln NE"),
    ("Sabir",       "Mehboob",      "3291 117th Ln NE"),
    ("Cullen",      "Claire",       "3292 117th Ln NE"),
    ("Vandermyde",  "Daniel",       "3297 117th Ln NE"),
    ("Cummings",    "Jennifer",     "3304 117th Ln NE"),
    ("Rice",        "Michelle",     "3313 117th Ln NE"),
    ("Park",        "Minjung",      "3332 117th Ln NE"),
    ("Filipi",      "Michael",      "3364 117th Ln NE"),
    ("Radulovich",  "Danielle",     "3368 117th Ln NE"),
    ("Hertz",       "Derek",        "3400 117th Ln NE"),
    ("Fincher",     "Sonya",        "3436 117th Ln NE"),
    ("Cheng",       "Liangsheng",   "3527 117th Ln NE"),
    ("Odeh",        "Mohammad",     "3557 117th Ln NE"),
    ("Battaglia",   "Daniel",       "11719 Naples Cir NE"),
    ("Johnson",     "Karissa",      "11736 Naples Cir NE"),
    ("Burgwald",    "Ryan",         "11739 Naples Cir NE"),
    ("Rachu",       "Kelly",        "11742 Naples Cir NE"),
    ("Banik",       "Ratan",        "11753 Naples Cir NE"),
    ("Sunderland",  "Toby",         "11756 Naples Cir NE"),
    ("Quist",       "Nathan",       "11757 Naples Cir NE"),
    ("Stusynski",   "Daniel",       "11760 Naples Cir NE"),
    ("Ruether",     "Steve",        "11761 Naples Cir NE"),
    ("Orrey",       "Joseph",       "11764 Naples Cir NE"),
    ("Olson",       "Kyle",         "11765 Naples Cir NE"),
    ("Herr",        "Greggory",     "11769 Naples Cir NE"),
    ("Ross",        "Monica",       "3166 117th Ln NE"),
    ("Chang",       "Su",           "3186 117th Ln NE"),
]

MCRO_URL = "https://publicaccess.courts.state.mn.us/CaseSearch"

# ---------------------------------------------------------------------------
# JS helper -- sets React-controlled input value and fires events
# ---------------------------------------------------------------------------
_SET_INPUTS_JS = """
(values) => {
    const inputs = document.querySelectorAll('input[type="text"]');
    const lastField  = inputs[0];
    const firstField = inputs[1];
    const niv = Object.getOwnPropertyDescriptor(
        window.HTMLInputElement.prototype, 'value'
    ).set;
    niv.call(lastField, values.last);
    lastField.dispatchEvent(new Event('input',  {bubbles: true}));
    lastField.dispatchEvent(new Event('change', {bubbles: true}));
    niv.call(firstField, values.first);
    firstField.dispatchEvent(new Event('input',  {bubbles: true}));
    firstField.dispatchEvent(new Event('change', {bubbles: true}));
}
"""


async def search_person(page, last_name: str, first_name: str) -> str:
    """
    Navigate to MCRO, accept terms if needed, fill the name search form,
    click Find, and return the page text of the results.
    """
    try:
        await page.goto(MCRO_URL, timeout=30_000)
        await page.wait_for_load_state("networkidle", timeout=20_000)
    except Exception as exc:
        return f"[ERROR loading page: {exc}]"

    # Accept terms -- MCRO shows "Yes, I Accept" dialog on each fresh load
    try:
        btn = page.locator("button", has_text="Yes, I Accept").first
        if await btn.is_visible(timeout=3_000):
            await btn.click()
            await page.wait_for_timeout(800)
    except Exception:
        pass

    # Fill in the name fields via React native setter
    try:
        await page.wait_for_selector('input[type="text"]', timeout=10_000)
        await page.evaluate(_SET_INPUTS_JS, {"last": last_name, "first": first_name})
        await page.wait_for_timeout(800)
    except Exception as exc:
        return f"[ERROR filling form: {exc}]"

    # Scroll to Find button and click it (it's near the bottom of the form)
    try:
        await page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        await page.wait_for_timeout(300)
        # MCRO's Find button is the last gold/brown button on the page
        find_btn = page.get_by_role("button", name="Find")
        await find_btn.click()
    except Exception as exc:
        return f"[ERROR clicking Find: {exc}]"

    # Wait for results to render
    await page.wait_for_timeout(20_000)

    try:
        text = await page.inner_text("body")
    except Exception as exc:
        text = f"[ERROR reading page: {exc}]"

    return text


async def run_all_searches(output_path: str) -> None:
    """
    Iterate over SEARCHES, scrape each person, and save results to JSON.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        print("ERROR: playwright is not installed.  Run:  pip install playwright && playwright install chromium")
        sys.exit(1)

    results = []

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=False)  # visible so CAPTCHA can be solved
        context = await browser.new_context()

        for idx, (last_name, first_name, address) in enumerate(SEARCHES, start=1):
            print(f"[{idx}/{len(SEARCHES)}] Searching: {last_name}, {first_name}  ({address})")
            page = await context.new_page()
            try:
                text = await search_person(page, last_name, first_name)
                status = "ok"
            except Exception as exc:
                text = f"[UNHANDLED ERROR: {exc}]"
                status = "error"
            finally:
                await page.close()

            results.append({
                "last_name":  last_name,
                "first_name": first_name,
                "address":    address,
                "status":     status,
                "scraped_at": datetime.utcnow().isoformat(),
                "page_text":  text,
            })

            # Brief pause between requests to avoid rate-limiting
            await asyncio.sleep(2)

        await browser.close()

    # Save results
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
    with open(output_path, "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, ensure_ascii=False)

    print(f"\nSaved {len(results)} results -> {output_path}")


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    _HERE = os.path.dirname(os.path.abspath(__file__))
    _ROOT = os.path.dirname(_HERE)

    output_path = os.path.join(_ROOT, "data", "mcro_results_v2.json")

    print("=" * 60)
    print("MCRO Scraper v2 -- React native setter fix")
    print("=" * 60)
    print(f"Searching {len(SEARCHES)} people …")
    print(f"Output -> {output_path}")
    print()

    asyncio.run(run_all_searches(output_path))
