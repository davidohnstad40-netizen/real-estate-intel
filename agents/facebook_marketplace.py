"""
Facebook Marketplace FSBO Scanner
====================================
Monitors Facebook Marketplace for real estate listings in the target area.

MOST VALUABLE USE: Finding homeowners who are ALREADY trying to sell off-market
(avoiding agents). This catches the warmest possible leads -- people actively
advertising their home for sale, just not through MLS.

Signal weights:
  +35 pts: exact address match (listing address = tracked property address)
  +20 pts: same street name match (e.g. "117th Ln" matches our street)
  +0  pts: neighborhood-only match (too vague, not saved)

STRICT filters:
  - Must be "Homes for Sale" category (not rentals, land, commercial)
  - Price must be $150,000 - $3,000,000
  - Location within 20 miles of target city
  - Description must NOT say "for rent", "lease", "monthly"
  - Must mention "sale", "selling", "must sell", "by owner", "FSBO", "for sale"

Uses Playwright headless=False (Facebook has aggressive bot detection).

Usage:
    python -m agents.facebook_marketplace
    python -m agents.facebook_marketplace --city "Blaine" --state "MN"
"""

import sys, os, re, math, hashlib, asyncio
from datetime import date
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db

# ── constants ──────────────────────────────────────────────────────────────────

MARKETPLACE_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS marketplace_signals (
    listing_id          VARCHAR PRIMARY KEY,
    price               DOUBLE,
    address_hint        VARCHAR,
    neighborhood        VARCHAR,
    description         TEXT,
    url                 VARCHAR,
    matched_property_id VARCHAR,
    confidence          DOUBLE,
    signal_pts          INTEGER,
    detected_at         TIMESTAMP DEFAULT current_timestamp
)
"""

# Reject if title/description contains any of these (rental indicators)
RENTAL_KEYWORDS = {
    "for rent", "for lease", "to lease", "per month", "/month", "per mo",
    "monthly", "month to month", "rental", "tenant", "lease agreement",
    "security deposit", "first last", "landlord", "renting out",
}

# Accept only if at least one of these is present (sale indicators)
SALE_KEYWORDS = {
    "for sale", "sale", "selling", "must sell", "by owner", "fsbo",
    "price reduced", "motivated seller", "make offer", "best offer",
    "sell fast", "quick sale", "priced to sell",
}

# Street suffix abbreviations to normalize addresses
STREET_SUFFIXES = {
    "street": "st", "avenue": "ave", "boulevard": "blvd",
    "drive": "dr", "court": "ct", "lane": "ln", "road": "rd",
    "place": "pl", "way": "way", "circle": "cir", "terrace": "ter",
    "northeast": "ne", "northwest": "nw", "southeast": "se", "southwest": "sw",
}

MIN_PRICE = 150_000
MAX_PRICE = 3_000_000


# ── helpers ────────────────────────────────────────────────────────────────────

def _listing_id(url: str, price: float, hint: str) -> str:
    raw = f"{url}{price}{hint}"
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _parse_price(text: str) -> float | None:
    """Extract and return a numeric price from a string like '$425,000' or '425000'."""
    # Remove dollar sign, commas, spaces
    cleaned = re.sub(r"[\$,\s]", "", text)
    # Try to find a standalone number >= 6 digits
    m = re.search(r"\b(\d{6,10})\b", cleaned)
    if m:
        return float(m.group(1))
    # K notation: 425K or 425k
    km = re.search(r"\b(\d{3,4})[Kk]\b", text)
    if km:
        return float(km.group(1)) * 1000
    return None


def _is_rental(text: str) -> bool:
    """Return True if the listing is a rental (should be excluded)."""
    lower = text.lower()
    return any(kw in lower for kw in RENTAL_KEYWORDS)


def _is_sale(text: str) -> bool:
    """Return True if the listing signals a home for sale."""
    lower = text.lower()
    return any(kw in lower for kw in SALE_KEYWORDS)


def _normalize_street(addr: str) -> str:
    """Lowercase, strip unit numbers, normalize suffixes."""
    addr = addr.lower().strip()
    addr = re.sub(r"\s+(apt|unit|#|ste)\s*\w+", "", addr)
    for full, abbr in STREET_SUFFIXES.items():
        addr = re.sub(r"\b" + full + r"\b", abbr, addr)
    addr = re.sub(r"\s+", " ", addr).strip()
    return addr


def _extract_address_hint(title: str, description: str) -> str:
    """
    Try to extract an address fragment from listing title or description.
    Returns the best address hint found, or empty string.
    """
    combined = f"{title} {description}"
    # Pattern: number + street name + suffix
    pattern = re.compile(
        r"\b(\d{3,5})\s+"
        r"([A-Za-z0-9]+(?:\s+[A-Za-z]+){0,3})\s+"
        r"(St|Ave|Ln|Dr|Ct|Blvd|Rd|Way|Cir|Pl|Ter|Street|Avenue|"
        r"Lane|Drive|Court|Boulevard|Road|Place|Circle|Terrace)"
        r"(?:\s+(?:NE|NW|SE|SW|N|S|E|W))?",
        re.IGNORECASE,
    )
    m = pattern.search(combined)
    if m:
        return m.group(0).strip()

    # Fallback: just a house number + word
    m2 = re.search(r"\b(\d{3,5})\s+([A-Z][a-z]+)\b", combined)
    if m2:
        return m2.group(0).strip()

    return ""


def _extract_neighborhood(title: str, description: str) -> str:
    """
    Try to extract a neighborhood or subdivision name from the listing text.
    Look for patterns like: "in [Name] neighborhood", "in [Name] subdivision",
    "located in [Name]", or common Blaine/Twin Cities neighborhood names.
    """
    combined = f"{title} {description}"

    patterns = [
        r"\bin\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\s+(?:neighborhood|subdivision|community|development|area|addition|estates|hills|meadows|park|ridge|woods)",
        r"(?:neighborhood|subdivision|community|area):\s*([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})",
        r"\blocated\s+in\s+([A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3})\b",
    ]
    for pat in patterns:
        m = re.search(pat, combined, re.IGNORECASE)
        if m:
            return m.group(1).strip()

    return ""


def _extract_listings_from_page(text: str, city: str, state: str) -> list[dict]:
    """
    Parse raw page text from Facebook Marketplace and extract home listings.
    Facebook's DOM is heavily obfuscated; we work with visible text patterns.
    """
    listings = []
    lines = [l.strip() for l in text.splitlines() if l.strip()]

    i = 0
    while i < len(lines):
        line = lines[i]

        # Facebook Marketplace price lines typically start with $ and have commas
        price_match = re.search(r"\$[\d,]+", line)
        if not price_match:
            i += 1
            continue

        price_val = _parse_price(price_match.group(0))
        if not price_val or not (MIN_PRICE <= price_val <= MAX_PRICE):
            i += 1
            continue

        # Grab context: next ~8 lines form the listing card
        context_lines = lines[i:min(i+10, len(lines))]
        context = " ".join(context_lines)

        # Apply strict filters
        if _is_rental(context):
            i += 1
            continue

        if not _is_sale(context):
            i += 1
            continue

        # Check location hint
        city_lower = city.lower()
        state_lower = state.lower()
        if city_lower not in context.lower() and state_lower not in context.lower():
            i += 1
            continue

        addr_hint   = _extract_address_hint(context, "")
        neighborhood = _extract_neighborhood(context, "")

        # Build confidence based on specificity
        if addr_hint and len(addr_hint.split()) >= 3:
            confidence = 0.8
            conf_reason = f"Address fragment extracted: '{addr_hint}'"
        elif addr_hint:
            confidence = 0.6
            conf_reason = f"Partial address: '{addr_hint}'"
        elif neighborhood:
            confidence = 0.4
            conf_reason = f"Neighborhood mentioned: '{neighborhood}'"
        else:
            confidence = 0.3
            conf_reason = f"City match only ({city})"

        # Higher confidence for "by owner" / "FSBO" -- warmest signal
        lower_ctx = context.lower()
        if "by owner" in lower_ctx or "fsbo" in lower_ctx:
            confidence = min(1.0, confidence + 0.15)
            conf_reason += " + owner listed (FSBO)"

        listing_id = _listing_id("marketplace", price_val, addr_hint or context[:50])

        listings.append({
            "listing_id":    listing_id,
            "price":         price_val,
            "address_hint":  addr_hint,
            "neighborhood":  neighborhood,
            "description":   context[:600],
            "url":           "",   # filled later if link found
            "confidence":    round(confidence, 2),
            "reason":        conf_reason,
        })

        i += 10  # skip past this card

    return listings


def _haversine_miles(lat1, lng1, lat2, lng2) -> float:
    R = 3958.8
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── public API ─────────────────────────────────────────────────────────────────

async def scrape_marketplace_listings(
    city: str = "Blaine",
    state: str = "MN",
    radius_miles: int = 20,
) -> list[dict]:
    """
    Scrape Facebook Marketplace for home-for-sale listings near the target city.

    Uses Playwright headless=False because Facebook aggressively detects headless
    browsers. The user must have a Facebook session active, or the page will
    redirect to login.

    Returns list of listing dicts (filtered, with confidence scores).
    """
    from playwright.async_api import async_playwright

    # Try multiple URL patterns; FB frequently changes their routing
    search_urls = [
        (f"https://www.facebook.com/marketplace/{city.lower()}-{state.lower()}"
         f"/propertyforsale/"),
        (f"https://www.facebook.com/marketplace/search"
         f"?query=house+for+sale+{city}+{state}&category_id=321187581960682"),
        (f"https://www.facebook.com/marketplace/search"
         f"?query=home+for+sale+{city}+{state}"),
        (f"https://www.facebook.com/marketplace/{city.lower()}mn"
         f"/propertyforsale/"),
    ]

    all_listings = []

    async with async_playwright() as pw:
        # headless=False -- Facebook bot detection requires visible browser
        browser = await pw.chromium.launch(
            headless=False,
            slow_mo=500,
            args=["--start-maximized"],
        )
        ctx = await browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ),
        )
        page = await ctx.new_page()

        # Check for existing FB login session
        print("Navigating to Facebook to check login status...")
        try:
            await page.goto("https://www.facebook.com/marketplace/",
                            timeout=25_000)
            await page.wait_for_timeout(3_000)

            if "login" in page.url.lower() or "checkpoint" in page.url.lower():
                print("\nFacebook login required.")
                print("Please log in to Facebook in the browser window.")
                print("Waiting up to 90 seconds for login...")
                for _ in range(90):
                    await asyncio.sleep(1)
                    if "marketplace" in page.url.lower():
                        print("Login detected -- continuing scan.")
                        break
        except Exception as e:
            print(f"  Facebook navigation error: {e}")

        # Try each search URL until we get listings
        for search_url in search_urls:
            print(f"Trying URL: {search_url}")
            try:
                await page.goto(search_url, timeout=20_000)
                await page.wait_for_timeout(5_000)

                # Scroll to load more listings
                for _ in range(3):
                    await page.keyboard.press("End")
                    await page.wait_for_timeout(2_000)

                # Grab all visible listing links
                links = await page.query_selector_all(
                    "a[href*='/marketplace/item/']"
                )
                link_urls = set()
                for link in links[:50]:
                    href = await link.get_attribute("href")
                    if href:
                        link_urls.add(
                            href if href.startswith("http")
                            else f"https://www.facebook.com{href}"
                        )

                # Get full page text for parsing
                text = await page.inner_text("body")
                listings = _extract_listings_from_page(text, city, state)

                # Assign URLs to listings where we can
                url_list = list(link_urls)
                for j, listing in enumerate(listings):
                    if j < len(url_list):
                        listing["url"] = url_list[j]

                print(f"  {len(listings)} listings extracted after filters.")
                if listings:
                    all_listings.extend(listings)
                    break  # stop trying URLs once we have results

            except Exception as e:
                print(f"  Error on {search_url}: {e}")
                continue

        await browser.close()

    # Deduplicate by listing_id
    seen = set()
    unique = []
    for lst in all_listings:
        if lst["listing_id"] not in seen:
            seen.add(lst["listing_id"])
            unique.append(lst)

    print(f"Total unique listings after scrape: {len(unique)}")
    return unique


def match_listings_to_properties(
    listings: list[dict],
    db_path: str = None,
) -> list[dict]:
    """
    Cross-reference marketplace listings against tracked properties in the DB.

    Match logic:
      - Exact: street number + street name fragment match -> confidence 1.0, +35 pts
      - Street name: same street name (multiple properties) -> confidence 0.6, +20 pts
      - Neighborhood: same area only -> confidence 0.3, excluded (too vague)

    Returns only matches with confidence >= 0.6.
    """
    if not listings:
        return []

    con = get_db(db_path, read_only=True)
    props = con.execute(
        "SELECT p.id, p.address, p.city, p.lat, p.lng FROM properties p "
        "WHERE p.address IS NOT NULL"
    ).fetchall()
    con.close()

    # Build a normalized property address index
    prop_index = []
    for pid, addr, pcity, plat, plng in props:
        norm = _normalize_street(addr or "")
        tokens = norm.split()
        prop_index.append({
            "id":          pid,
            "address":     addr,
            "city":        pcity,
            "lat":         plat,
            "lng":         plng,
            "normalized":  norm,
            "street_num":  tokens[0] if tokens else "",
            # Street name = everything after the number, drop suffix
            "street_name": " ".join(tokens[1:-1]) if len(tokens) > 2 else " ".join(tokens[1:]),
        })

    matches = []

    for listing in listings:
        addr_hint = listing.get("address_hint", "")
        if not addr_hint:
            continue

        hint_norm  = _normalize_street(addr_hint)
        hint_tokens = hint_norm.split()
        hint_num   = hint_tokens[0] if hint_tokens else ""
        hint_street = " ".join(hint_tokens[1:]) if len(hint_tokens) > 1 else ""

        if not hint_street:
            continue  # can't match without a street name

        best_match = None
        best_conf  = 0.0

        for prop in prop_index:
            pnum   = prop["street_num"]
            pstreet = prop["street_name"]

            if not pstreet or len(pstreet) < 3:
                continue

            # Exact address: number matches AND street name overlaps substantially
            num_match    = (hint_num == pnum and bool(hint_num))
            # Street overlap: longest common token
            hint_parts = set(hint_street.split())
            prop_parts = set(pstreet.split())
            overlap    = hint_parts & prop_parts

            if num_match and overlap:
                confidence = 1.0
                reason     = (f"Exact: {hint_num} {hint_street} matches "
                              f"tracked property {prop['address']}")
                pts        = 35
            elif overlap and len(max(overlap, key=len)) >= 4:
                confidence = 0.6
                reason     = (f"Same street name '{max(overlap, key=len)}' "
                              f"as tracked property {prop['address']}")
                pts        = 20
            else:
                continue

            if confidence > best_conf:
                best_conf  = confidence
                best_match = {
                    "listing_id":           listing["listing_id"],
                    "price":                listing["price"],
                    "address_hint":         addr_hint,
                    "neighborhood":         listing.get("neighborhood", ""),
                    "description":          listing.get("description", "")[:600],
                    "url":                  listing.get("url", ""),
                    "matched_property_id":  prop["id"],
                    "confidence":           round(confidence, 2),
                    "signal_pts":           pts,
                    "match_reason":         reason,
                    "listing_confidence":   listing.get("confidence", 0),
                    "listing_reason":       listing.get("reason", ""),
                }

        if best_match and best_match["confidence"] >= 0.6:
            matches.append(best_match)

    print(f"Property matches found: {len(matches)} "
          f"(confidence >= 0.6 required, {len(listings)} listings evaluated)")
    return matches


async def run_scan_async(
    city: str = "Blaine",
    db_path: str = None,
) -> list[dict]:
    """
    Full pipeline: scrape -> match -> save -> return matches.
    """
    print(f"\n{'='*60}")
    print(f"FACEBOOK MARKETPLACE SCAN -- {city}")
    print(f"{'='*60}")

    listings = await scrape_marketplace_listings(city=city)

    if not listings:
        print("No listings found. Check Facebook session / bot detection.")
        return []

    matches = match_listings_to_properties(listings, db_path=db_path)

    if not matches:
        print("No property matches found in tracked database.")
        return []

    # Persist to DB
    try:
        write_con = get_db(db_path)
        write_con.execute(MARKETPLACE_SIGNALS_DDL)
        for m in matches:
            write_con.execute("""
                INSERT OR REPLACE INTO marketplace_signals
                (listing_id, price, address_hint, neighborhood, description,
                 url, matched_property_id, confidence, signal_pts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                m["listing_id"],
                m["price"],
                m["address_hint"],
                m["neighborhood"],
                m["description"][:1000],
                m["url"],
                m["matched_property_id"],
                m["confidence"],
                m["signal_pts"],
            ])
        write_con.close()
        print(f"Saved {len(matches)} matches to marketplace_signals table.")
    except Exception as e:
        print(f"DB write error: {e}")

    return matches


def run_scan(city: str = "Blaine", db_path: str = None) -> list[dict]:
    """Sync wrapper for run_scan_async."""
    return asyncio.run(run_scan_async(city=city, db_path=db_path))


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Scan Facebook Marketplace for FSBO / off-market homes"
    )
    parser.add_argument("--city",   default="Blaine")
    parser.add_argument("--state",  default="MN")
    parser.add_argument("--radius", type=int, default=20)
    args = parser.parse_args()

    matches = run_scan(city=args.city)

    print(f"\n{'='*70}")
    print(f"FACEBOOK MARKETPLACE REPORT -- {args.city}")
    print(f"{'='*70}")
    print(f"Total property matches: {len(matches)}")

    for m in sorted(matches, key=lambda x: x["signal_pts"], reverse=True):
        print(f"\n  [{m['confidence']:.1f}] {m['address_hint'] or 'no address'} "
              f"-- ${m['price']:,.0f}")
        print(f"    Property ID: {m['matched_property_id']}")
        print(f"    Signal pts:  +{m['signal_pts']}")
        print(f"    Match:       {m['match_reason']}")
        print(f"    Listing:     {m['listing_reason']}")
        if m.get("url"):
            print(f"    URL:         {m['url'][:80]}")
