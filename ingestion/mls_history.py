"""
MLS / Listing History Scraper (Zillow)
========================================
Pulls price history and listing event history from Zillow for specific
property addresses.

Prior expired/withdrawn listings are the strongest motivated-seller signal:
the owner tried to sell and couldn't (at the price they wanted). They may be
ready to accept a below-market off-market offer now.

Scoring:
    25 pts  Previous "Listing removed" / "Expired" / "Withdrawn" event
    15 pts  Price reduction > 5% before eventual sale
    10 pts  Multiple distinct listing attempts (relisted)
    0  pts  Clean first-time listing or no history

Usage:
    python -m ingestion.mls_history
    # or import and call get_zillow_history(address, zip_code)
"""

import sys, os, re, json, asyncio, time
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

DEFAULT_ZIP      = "55449"
RATE_LIMIT_SECS  = 3   # delay between batch requests
PAGE_TIMEOUT     = 20_000   # ms

# Zillow event labels that flag prior listing attempt
LISTING_ATTEMPT_EVENTS = [
    "listed for sale",
    "listing removed",
    "listing expired",
    "listing withdrawn",
    "relisted",
    "re-listed",
]

REMOVAL_EVENTS = [
    "listing removed",
    "listing expired",
    "listing withdrawn",
    "expired",
    "withdrawn",
    "removed",
    "delisted",
]

PRICE_CHANGE_EVENTS = [
    "price change",
    "price reduced",
    "price cut",
    "price decrease",
    "reduced",
]


# ---------------------------------------------------------------------------
# Address → Zillow URL slug
# ---------------------------------------------------------------------------
def _build_zillow_url(address: str, zip_code: str) -> str:
    """
    Convert a street address + zip to a Zillow property URL.

    Example:
        "3316 117th Ln NE", "55449"
        → "https://www.zillow.com/homes/3316-117th-Ln-NE-Blaine-MN-55449/"
    """
    # Normalize: replace spaces with dashes, remove commas
    slug = re.sub(r"\s+", "-", address.strip().replace(",", ""))
    # Include city/state for disambiguation; we'll try both with and without
    url = f"https://www.zillow.com/homes/{slug}-{zip_code}/"
    return url


def _build_zillow_search_url(address: str, zip_code: str) -> str:
    """Build a Zillow search URL as a fallback."""
    query = f"{address} {zip_code}"
    encoded = query.replace(" ", "+")
    return f"https://www.zillow.com/homes/{encoded}/"


# ---------------------------------------------------------------------------
# Internal: Playwright scraper
# ---------------------------------------------------------------------------
async def _fetch_zillow_property(address: str, zip_code: str) -> dict:
    """
    Navigate to Zillow property page and extract price history.
    Returns a dict with zestimate, price_history list, and raw URL used.
    """
    from playwright.async_api import async_playwright

    result: dict = {
        "zestimate":     None,
        "price_history": [],
        "url_used":      "",
    }

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-blink-features=AutomationControlled",
                "--disable-dev-shm-usage",
            ],
        )
        ctx = await browser.new_context(
            user_agent=USER_AGENT,
            viewport={"width": 1400, "height": 900},
            extra_http_headers={
                "Accept-Language": "en-US,en;q=0.9",
                "Accept":          "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
        )
        page = await ctx.new_page()

        # Build candidate URLs to try
        direct_url = _build_zillow_url(address, zip_code)
        search_url = _build_zillow_search_url(address, zip_code)
        candidate_urls = [direct_url, search_url]

        page_loaded = False
        for url in candidate_urls:
            try:
                resp = await page.goto(url, timeout=PAGE_TIMEOUT)
                await page.wait_for_timeout(3_500)

                # Check for redirect to a property detail page (good sign)
                current_url = page.url
                status      = resp.status if resp else 0

                if status == 200 or ("zpid" in current_url or "/homedetails/" in current_url):
                    result["url_used"] = current_url
                    page_loaded = True
                    break

                # Zillow might redirect to a search results page; try clicking first result
                if "homes" in current_url and "search" not in current_url:
                    # Try to click the first property card
                    card = await page.query_selector('[data-test="property-card-link"], .list-card-link')
                    if card:
                        await card.click()
                        await page.wait_for_timeout(3_000)
                        result["url_used"] = page.url
                        page_loaded = True
                        break

            except Exception as e:
                print(f"[mls_history] URL failed {url}: {e}")
                continue

        if not page_loaded:
            await browser.close()
            return result

        # ----------------------------------------------------------------
        # 1. Try to extract from __NEXT_DATA__ JSON (most reliable)
        # ----------------------------------------------------------------
        try:
            raw_json = await page.evaluate("""
                () => {
                    const el = document.getElementById('__NEXT_DATA__');
                    return el ? el.textContent : null;
                }
            """)

            if raw_json:
                data = json.loads(raw_json)
                home_info = (
                    data.get("props", {})
                        .get("pageProps", {})
                        .get("componentProps", {})
                        .get("gdpClientCache", {})
                )

                # gdpClientCache is a JSON-encoded string in some Zillow versions
                if isinstance(home_info, str):
                    try:
                        home_info = json.loads(home_info)
                    except Exception:
                        home_info = {}

                # Walk the cache to find priceHistory
                for key, val in home_info.items():
                    if isinstance(val, dict):
                        props_node = val.get("property", val)
                        ph = props_node.get("priceHistory", [])
                        if ph:
                            result["price_history"] = _normalize_price_history(ph)

                        ze = props_node.get("zestimate")
                        if ze:
                            result["zestimate"] = float(ze)

                        if result["price_history"]:
                            break

                # Also try the simpler path
                if not result["price_history"]:
                    search_state = (
                        data.get("props", {})
                            .get("pageProps", {})
                            .get("searchPageState", {})
                    )
                    cat1 = search_state.get("cat1", {})
                    for r in cat1.get("searchResults", {}).get("listResults", []):
                        hdp = r.get("hdpData", {}).get("homeInfo", {})
                        if hdp.get("priceHistory"):
                            result["price_history"] = _normalize_price_history(hdp["priceHistory"])
                            result["zestimate"]      = hdp.get("zestimate")
                            break

        except Exception as e:
            print(f"[mls_history] JSON extraction error: {e}")

        # ----------------------------------------------------------------
        # 2. Fallback: DOM scraping of the Price History section
        # ----------------------------------------------------------------
        if not result["price_history"]:
            try:
                # Scroll down to ensure price history section is loaded
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await page.wait_for_timeout(2_000)

                # Try to find and expand the price history section
                for expander in [
                    "button:has-text('Price history')",
                    "button:has-text('Price History')",
                    "[data-testid='price-history-toggle']",
                    ".price-history-toggle",
                    "a:has-text('See all')",
                ]:
                    try:
                        el = await page.query_selector(expander)
                        if el:
                            await el.click()
                            await page.wait_for_timeout(1_500)
                            break
                    except Exception:
                        pass

                # Extract from price history table
                rows = await page.query_selector_all(
                    "[data-testid='price-history-row'], "
                    ".price-history-row, "
                    "table.price-history tr, "
                    "[class*='PriceHistory'] tr, "
                    "[class*='priceHistory'] tr"
                )

                for row in rows:
                    cells = await row.query_selector_all("td, th, span, div")
                    texts = [(await c.inner_text()).strip() for c in cells if (await c.inner_text()).strip()]
                    if len(texts) >= 2:
                        entry = _parse_price_history_dom_row(texts)
                        if entry:
                            result["price_history"].append(entry)

            except Exception as e:
                print(f"[mls_history] DOM price history error: {e}")

        # ----------------------------------------------------------------
        # 3. Extract zestimate from DOM if not found in JSON
        # ----------------------------------------------------------------
        if result["zestimate"] is None:
            try:
                for ze_sel in [
                    "[data-testid='zestimate-value']",
                    ".Zestimate__Value",
                    "[class*='zestimate' i]",
                    "span:has-text('Zestimate')",
                ]:
                    el = await page.query_selector(ze_sel)
                    if el:
                        ze_text = await el.inner_text()
                        ze_clean = re.sub(r"[^\d]", "", ze_text)
                        if ze_clean:
                            result["zestimate"] = float(ze_clean)
                        break
            except Exception:
                pass

        try:
            await browser.close()
        except Exception:
            pass

    return result


def _normalize_price_history(raw: list) -> list[dict]:
    """
    Normalize Zillow priceHistory JSON (from __NEXT_DATA__) to standard format.
    Each entry: {date, event, price, delta}
    """
    normalized = []
    for item in raw:
        if not isinstance(item, dict):
            continue

        # Date
        date_str = item.get("date", item.get("time", ""))
        if isinstance(date_str, (int, float)):
            # Unix timestamp in ms
            try:
                date_str = datetime.fromtimestamp(date_str / 1000).strftime("%Y-%m-%d")
            except Exception:
                date_str = ""

        # Event
        event = (
            item.get("event", "")
            or item.get("priceChangeRate", "")
            or ""
        )
        if not event:
            event = item.get("source", "")

        # Price
        price = item.get("price", item.get("pricePerSquareFoot"))
        if price is not None:
            try:
                price = float(price)
            except (TypeError, ValueError):
                price = None

        # Delta / percent change
        delta = item.get("priceChangeRate", item.get("pricePerSqftChangeRate"))
        if delta is not None:
            try:
                delta = float(delta)
            except (TypeError, ValueError):
                delta = None

        normalized.append({
            "date":  str(date_str),
            "event": str(event).strip(),
            "price": price,
            "delta": delta,
        })

    return normalized


def _parse_price_history_dom_row(texts: list[str]) -> Optional[dict]:
    """
    Parse a DOM row from the price history table.
    Handles both 3-column and 4-column Zillow table layouts.
    """
    # Try to find date
    date_str = ""
    for t in texts:
        m = re.search(r"(\d{1,2}/\d{1,2}/\d{4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]+\s+\d+,\s+\d{4})", t)
        if m:
            date_str = m.group(1)
            break

    # Event is usually the longest non-numeric, non-date string
    event = ""
    for t in texts:
        if t == date_str:
            continue
        if re.match(r"^[\$\d,+\-%\.]+$", t):
            continue
        if len(t) > len(event):
            event = t

    # Price: find $ amount
    price = None
    for t in texts:
        m = re.search(r"\$?([\d,]+)", t)
        if m:
            try:
                price = float(m.group(1).replace(",", ""))
            except ValueError:
                pass
            break

    # Delta: look for percentage
    delta = None
    for t in texts:
        m = re.search(r"([+-]?[\d.]+)\s*%", t)
        if m:
            try:
                delta = float(m.group(1))
            except ValueError:
                pass
            break

    if not date_str and not event:
        return None

    return {"date": date_str, "event": event, "price": price, "delta": delta}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def get_zillow_history(address: str, zip_code: str = DEFAULT_ZIP) -> dict:
    """
    Navigate to Zillow property page for the given address and extract
    the price history table.

    Args:
        address:  Street address, e.g. "3316 117th Ln NE"
        zip_code: 5-digit zip, default "55449" (Blaine, MN)

    Returns:
        {
            "zestimate":     float or None,
            "price_history": [{"date": str, "event": str, "price": float, "delta": float}],
        }
        OR {"error": "reason"} if page unavailable.
    """
    try:
        raw = asyncio.run(_fetch_zillow_property(address, zip_code))

        if not raw.get("price_history") and raw.get("zestimate") is None:
            return {"error": "No property data found -- page may have CAPTCHA or address not matched"}

        return {
            "zestimate":     raw.get("zestimate"),
            "price_history": raw.get("price_history", []),
        }

    except Exception as e:
        print(f"[mls_history] get_zillow_history failed for '{address}': {e}")
        return {"error": str(e)}


def score_mls_signal(history: dict) -> tuple[int, str]:
    """
    Score MLS/listing history as a motivated-seller signal.

    Args:
        history: Output from get_zillow_history()

    Returns:
        (points, reason)  where points is 0-25 and reason is a short description.
        Returns (0, "") on error or clean history.

    Scoring:
        25 pts  Prior "Listing removed" / "Expired" / "Withdrawn" event
        15 pts  Price reduction > 5% before eventual sale
        10 pts  Multiple distinct listing attempts (relisted)
        0  pts  Clean first-time listing or no history
    """
    if "error" in history or not history.get("price_history"):
        return 0, ""

    events   = history["price_history"]
    best_pts = 0
    reason   = ""

    # Count distinct listing attempts
    listing_attempts = [
        e for e in events
        if "listed for sale" in (e.get("event") or "").lower()
        or "listing" in (e.get("event") or "").lower()
    ]
    removal_events = [
        e for e in events
        if any(kw in (e.get("event") or "").lower() for kw in REMOVAL_EVENTS)
    ]
    price_changes = [
        e for e in events
        if any(kw in (e.get("event") or "").lower() for kw in PRICE_CHANGE_EVENTS)
        and e.get("delta") is not None
        and float(e.get("delta") or 0) < 0
    ]

    # 25 pts: Prior removal/expiry
    if removal_events:
        best_pts = 25
        ev       = removal_events[0]
        reason   = f"Prior listing removed/expired on {ev.get('date','?')}"
        return best_pts, reason

    # 15 pts: Price reduction > 5%
    for pc in price_changes:
        try:
            delta_pct = abs(float(pc.get("delta") or 0))
            if delta_pct > 5.0:
                if best_pts < 15:
                    best_pts = 15
                    reason   = (
                        f"Price reduced {delta_pct:.1f}% on {pc.get('date','?')} "
                        f"(${pc.get('price') or 0:,.0f})"
                    )
        except (TypeError, ValueError):
            pass

    # 10 pts: Multiple listing attempts
    if len(listing_attempts) >= 2 and best_pts < 10:
        best_pts = 10
        reason   = f"Relisted {len(listing_attempts)} times -- couldn't sell at ask"

    return best_pts, reason


def check_properties_batch(
    property_ids: list[str],
    db_path: Optional[str] = None,
) -> pd.DataFrame:
    """
    For each property_id, look up the address from DuckDB, call
    get_zillow_history, score the result, and return a DataFrame.

    Args:
        property_ids: List of property IDs from the properties table.
        db_path:      Path to DuckDB file. Defaults to env var DB_PATH or
                      ./data/rei.duckdb.

    Returns:
        DataFrame with columns:
            property_id, address, zestimate, mls_signal_pts, mls_reason, price_history_json
    """
    from db.schema import get_db

    con = get_db(db_path, read_only=True)
    try:
        # Build SQL-safe list of IDs
        id_list = ", ".join(f"'{pid}'" for pid in property_ids)
        rows = con.execute(f"""
            SELECT id, address, zip
            FROM properties
            WHERE id IN ({id_list})
            ORDER BY id
        """).fetchall()
    finally:
        con.close()

    records = []
    for prop_id, address, zip_code in rows:
        print(f"[mls_history] Fetching Zillow for: {address} ({prop_id})")

        hist = get_zillow_history(address, zip_code or DEFAULT_ZIP)
        pts, reason = score_mls_signal(hist)

        records.append({
            "property_id":        prop_id,
            "address":            address,
            "zestimate":          hist.get("zestimate"),
            "mls_signal_pts":     pts,
            "mls_reason":         reason,
            "price_history_json": json.dumps(hist.get("price_history", [])),
        })

        # Rate limit between requests
        time.sleep(RATE_LIMIT_SECS)

    return pd.DataFrame(records)


# ---------------------------------------------------------------------------
# CLI test
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    from db.schema import get_db

    print("[mls_history] Fetching T1/T2 properties from DB...")
    print("-" * 60)

    db_path = os.path.join(
        os.path.dirname(__file__), "..", "data", "rei.duckdb"
    )

    try:
        con = get_db(db_path, read_only=True)
        rows = con.execute("""
            SELECT p.id, p.address, p.zip
            FROM properties p
            JOIN property_scores s ON p.id = s.id
            WHERE s.knock_tier IN ('T1', 'T2')
            ORDER BY s.motivation_score DESC
            LIMIT 5
        """).fetchall()
        con.close()
    except Exception as e:
        print(f"Could not query DB: {e}")
        rows = []

    if not rows:
        # Fallback: test with a known address
        print("No DB rows found -- testing with a single address.")
        test_addr = "3316 117th Ln NE"
        test_zip  = "55449"

        hist = get_zillow_history(test_addr, test_zip)
        pts, reason = score_mls_signal(hist)

        print(f"\nAddress: {test_addr}")
        print(f"Zestimate: ${hist.get('zestimate') or 0:,.0f}")
        print(f"MLS signal: {pts} pts -- {reason}")
        print(f"Price history ({len(hist.get('price_history', []))} events):")
        for ev in hist.get("price_history", []):
            print(f"  [{ev.get('date','?')}] {ev.get('event','?')} -- ${ev.get('price') or 0:,.0f}")

        if "error" in hist:
            print(f"Error: {hist['error']}")
    else:
        # Run batch check
        prop_ids = [r[0] for r in rows]
        print(f"Checking {len(prop_ids)} T1/T2 properties: {prop_ids}")
        df = check_properties_batch(prop_ids, db_path)
        print("\nResults:")
        print(df.to_string(index=False))
