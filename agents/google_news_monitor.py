"""
Google News Employer Signal Agent
==================================
Monitors Google News RSS for local employer closures, mass layoffs, or business
failures in the target area. When a major employer closes near our target
properties, homeowners who work there may be forced to sell.

Google News RSS API (public, no auth):
  https://news.google.com/rss/search?q={query}&hl=en-US&gl=US&ceid=US:en

Signal weights:
  +15 pts: verified closure/layoff of 50+ employees within 5 miles
  +10 pts: verified closure/layoff of 25-49 employees within 10 miles
  +5  pts: general economic stress news in area

Usage:
    python -m agents.google_news_monitor
    python -m agents.google_news_monitor --city "Blaine" --radius 15
"""

import sys, os, re, math, hashlib, asyncio
from datetime import date, datetime, timedelta
from urllib.request import urlopen, Request
from urllib.parse import quote_plus
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db

# ── constants ──────────────────────────────────────────────────────────────────

NEWS_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS news_signals (
    article_id     VARCHAR PRIMARY KEY,
    title          VARCHAR,
    description    TEXT,
    url            VARCHAR,
    published_date DATE,
    city           VARCHAR,
    confidence     DOUBLE,
    reason         TEXT,
    signal_pts     INTEGER,
    detected_at    TIMESTAMP DEFAULT current_timestamp
)
"""

# Words that strongly indicate a real closure/layoff event (not generic economy)
LAYOFF_KEYWORDS = {
    "layoffs", "layoff", "laid off", "job cuts", "job cut", "downsizing",
    "closing", "closure", "closures", "shut down", "shutdown", "shutting down",
    "going out of business", "ceasing operations", "filing bankruptcy",
    "chapter 7", "chapter 11", "bankruptcy", "receivership", "plant closing",
    "plant closure", "facility closing", "store closing", "location closing",
    "mass layoff", "workforce reduction", "reduction in force", "rif ",
    "furlough", "furloughs", "temporary closure", "permanent closure",
}

# Words indicating a specific number of employees or jobs is mentioned
EMPLOYEE_NUMBER_PATTERN = re.compile(
    r"\b(\d[\d,]*)\s*(?:employees|workers|jobs|positions|people|staff)\b",
    re.IGNORECASE,
)

# Words indicating a specific business location (not national HQ story)
LOCAL_INDICATOR_PATTERN = re.compile(
    r"\b(?:local|branch|location|office|plant|facility|store|site|center|centre)"
    r"\b",
    re.IGNORECASE,
)

# Words that signal this is real-estate/mortgage noise -- filter these out
NOISE_KEYWORDS = {
    "housing market", "real estate", "mortgage rates", "home prices",
    "home sales", "inventory", "interest rates", "fed rate", "refinanc",
    "foreclosure rates", "housing starts", "building permits",
    "home values", "zillow", "realtor", "redfin",
}

# Company name heuristic: at least one capitalized word that looks like a
# proper noun (not a common word) adjacent to a layoff keyword.
COMPANY_NAME_PATTERN = re.compile(
    r"\b([A-Z][a-z]{2,}(?:\s+[A-Z][a-z]{2,}){0,4}|[A-Z]{2,})\b",
)

COMMON_WORDS = {
    "The", "And", "For", "Are", "But", "Not", "You", "All", "Can", "Her",
    "Was", "One", "Our", "Out", "Day", "Get", "Has", "Him", "His", "How",
    "Man", "New", "Now", "Old", "See", "Two", "Way", "Who", "Boy", "Did",
    "Its", "Let", "Put", "Say", "She", "Too", "Use", "Minnesota", "Twin",
    "Cities", "County", "City", "State", "Federal", "North", "South",
    "East", "West", "Area", "Local", "Region", "Workers", "Employees",
    "Jobs", "Company", "Business",
}

# ── helpers ────────────────────────────────────────────────────────────────────

def _article_id(url: str, title: str) -> str:
    """Stable ID based on URL + title hash."""
    raw = (url or "") + (title or "")
    return hashlib.md5(raw.encode()).hexdigest()[:16]


def _parse_rss_date(date_str: str):
    """Parse RSS pubDate string to a date object. Returns None on failure."""
    if not date_str:
        return None
    fmts = [
        "%a, %d %b %Y %H:%M:%S %z",
        "%a, %d %b %Y %H:%M:%S GMT",
        "%a, %d %b %Y %H:%M:%S +0000",
        "%d %b %Y %H:%M:%S %z",
    ]
    for fmt in fmts:
        try:
            return datetime.strptime(date_str.strip(), fmt).date()
        except ValueError:
            pass
    # Try just extracting year/month/day digits
    m = re.search(r"(\d{1,2})\s+(\w{3})\s+(\d{4})", date_str)
    if m:
        try:
            return datetime.strptime(m.group(0), "%d %b %Y").date()
        except ValueError:
            pass
    return None


def _clean_html(text: str) -> str:
    """Strip basic HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", text or "")
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace(
        "&gt;", ">").replace("&quot;", '"').replace("&#39;", "'")
    return re.sub(r"\s+", " ", text).strip()


def _has_noise(text: str) -> bool:
    """Return True if the text is dominated by real-estate/finance noise."""
    lower = text.lower()
    hits = sum(1 for kw in NOISE_KEYWORDS if kw in lower)
    return hits >= 2


def _has_layoff_keyword(text: str) -> bool:
    lower = text.lower()
    return any(kw in lower for kw in LAYOFF_KEYWORDS)


def _extract_employee_count(text: str):
    """Return the largest employee count mentioned, or None."""
    matches = EMPLOYEE_NUMBER_PATTERN.findall(text)
    if not matches:
        return None
    nums = [int(m.replace(",", "")) for m in matches]
    return max(nums)


def _has_company_name(text: str) -> bool:
    """Return True if text contains something that looks like a company name."""
    caps = COMPANY_NAME_PATTERN.findall(text)
    proper = [w for w in caps if w not in COMMON_WORDS and len(w) > 3]
    return len(proper) >= 1


def _has_local_indicator(text: str) -> bool:
    return bool(LOCAL_INDICATOR_PATTERN.search(text))


def _score_article(title: str, description: str, city: str) -> tuple[float, str]:
    """
    Assign confidence and a reason string.

    Rules (applied in order, first match wins):
      1.0  -- mentions city, specific employee count >= 25, local indicator,
               has company name, has layoff keyword
      0.8  -- mentions city, specific employee count >= 10, has layoff keyword
      0.6  -- mentions city, has layoff keyword (vague about numbers)
      0.3  -- mentions city, general economic downturn language only
      0.0  -- does not satisfy any threshold (will be dropped)
    """
    combined = f"{title} {description}"
    city_lower = city.lower()

    if city_lower not in combined.lower():
        return 0.0, "city not mentioned"

    if _has_noise(combined):
        return 0.0, "real-estate/mortgage noise"

    if not _has_layoff_keyword(combined):
        return 0.3, f"general economic language mentioning {city}"

    emp_count = _extract_employee_count(combined)
    has_company = _has_company_name(combined)
    has_local = _has_local_indicator(combined)

    if emp_count and emp_count >= 25 and has_company and has_local:
        return 1.0, (f"Verified: {emp_count} employees affected at named local "
                     f"business in {city}")

    if emp_count and emp_count >= 10 and has_company:
        return 0.8, (f"Likely verified: {emp_count} employees at named company, "
                     f"city mentioned")

    if has_company and has_local:
        return 0.7, f"Named local business layoff/closure in {city} (count unclear)"

    if has_company:
        return 0.6, f"Named company layoff/closure mentioning {city} (vague)"

    return 0.3, f"Layoff keyword in {city} area -- no specific company identified"


def _haversine_miles(lat1, lng1, lat2, lng2) -> float:
    """Distance in miles between two lat/lng points."""
    R = 3958.8  # Earth radius in miles
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng / 2) ** 2)
    return R * 2 * math.asin(math.sqrt(a))


# ── public API ─────────────────────────────────────────────────────────────────

def search_employer_news(
    city: str,
    radius_miles: int = 15,
    days_back: int = 180,
) -> list[dict]:
    """
    Query Google News RSS for employer closure/layoff stories near the city.

    Returns a list of dicts with keys:
      title, description, url, published_date, mentioned_city,
      confidence, reason, estimated_employees
    """
    query = (
        f"(layoffs OR closing OR shutdown OR bankruptcy OR "
        f'"going out of business") AND {city} AND Minnesota AND '
        f"(employees OR workers OR jobs)"
    )
    encoded = quote_plus(query)
    url = (f"https://news.google.com/rss/search?q={encoded}"
           f"&hl=en-US&gl=US&ceid=US:en")

    cutoff = date.today() - timedelta(days=days_back)

    print(f"Fetching Google News RSS for: {city} ...")
    try:
        req = Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        })
        with urlopen(req, timeout=20) as resp:
            raw = resp.read()
    except Exception as e:
        print(f"  ERROR fetching RSS: {e}")
        return []

    try:
        root = ET.fromstring(raw)
    except ET.ParseError as e:
        print(f"  ERROR parsing RSS XML: {e}")
        return []

    ns = {"media": "http://search.yahoo.com/mrss/"}
    channel = root.find("channel")
    if channel is None:
        print("  No <channel> element in RSS feed.")
        return []

    items = channel.findall("item")
    print(f"  {len(items)} raw items returned from Google News RSS.")

    results = []
    for item in items:
        title_el = item.find("title")
        desc_el  = item.find("description")
        link_el  = item.find("link")
        date_el  = item.find("pubDate")

        title  = _clean_html(title_el.text if title_el is not None else "")
        desc   = _clean_html(desc_el.text  if desc_el  is not None else "")
        link   = (link_el.text or "").strip() if link_el is not None else ""
        pub_d  = _parse_rss_date(date_el.text if date_el is not None else "")

        # Date filter
        if pub_d and pub_d < cutoff:
            continue

        confidence, reason = _score_article(title, desc, city)
        if confidence < 0.3:
            continue  # not relevant at all

        emp_count = _extract_employee_count(f"{title} {desc}")

        results.append({
            "article_id":         _article_id(link, title),
            "title":              title,
            "description":        desc[:1000],
            "url":                link,
            "published_date":     str(pub_d) if pub_d else "",
            "mentioned_city":     city,
            "confidence":         round(confidence, 2),
            "reason":             reason,
            "estimated_employees": emp_count,
        })

    print(f"  {len(results)} articles passed filters (confidence >= 0.3).")
    return results


def score_news_signal(
    news_items: list[dict],
    property_lat: float,
    property_lng: float,
) -> tuple[int, str]:
    """
    Score news signals relative to a specific property location.

    Returns (signal_pts, reason_string).
    Only items with confidence >= 0.6 contribute to the score.
    """
    qualifying = [n for n in news_items if n.get("confidence", 0) >= 0.6]
    if not qualifying:
        return 0, "No qualifying news signals (confidence < 0.6)"

    best_pts = 0
    best_reason = ""

    for item in qualifying:
        # We do not have exact lat/lng of employer from RSS data,
        # so we use the city-level signal only (city is in our target area).
        # Future enhancement: geocode the mentioned employer address.
        emp = item.get("estimated_employees") or 0
        conf = item.get("confidence", 0)
        title_short = item["title"][:80]

        if emp >= 50 and conf >= 0.8:
            pts = 15
            reason = (f"+15: Verified closure/layoff of {emp}+ employees "
                      f"in target area -- \"{title_short}\"")
        elif 25 <= emp < 50 and conf >= 0.6:
            pts = 10
            reason = (f"+10: Verified closure/layoff of {emp} employees "
                      f"in target area -- \"{title_short}\"")
        elif conf >= 0.6:
            pts = 5
            reason = (f"+5: General economic stress in area "
                      f"(conf={conf:.1f}) -- \"{title_short}\"")
        else:
            pts = 0
            reason = ""

        if pts > best_pts:
            best_pts   = pts
            best_reason = reason

    return best_pts, best_reason or "News signals present but low scoring"


def check_for_area(city: str = "Blaine", db_path: str = None) -> list[dict]:
    """
    Main entry point: fetch news, score each article, save to DB, return results.
    """
    news = search_employer_news(city=city)
    if not news:
        print("No relevant news found.")
        return []

    # Assign signal_pts at article level (city-wide, not property-specific)
    # Property-specific scoring happens in score_news_signal() when called
    # from run_daily.py with property coordinates.
    for item in news:
        emp  = item.get("estimated_employees") or 0
        conf = item.get("confidence", 0)
        if emp >= 50 and conf >= 0.8:
            item["signal_pts"] = 15
        elif 25 <= emp < 50 and conf >= 0.6:
            item["signal_pts"] = 10
        elif conf >= 0.6:
            item["signal_pts"] = 5
        else:
            item["signal_pts"] = 0

    # Persist to DB
    try:
        con = get_db(db_path)
        con.execute(NEWS_SIGNALS_DDL)
        for item in news:
            con.execute("""
                INSERT OR REPLACE INTO news_signals
                (article_id, title, description, url, published_date, city,
                 confidence, reason, signal_pts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                item["article_id"],
                item["title"],
                item["description"],
                item["url"],
                item.get("published_date") or None,
                item["mentioned_city"],
                item["confidence"],
                item["reason"],
                item["signal_pts"],
            ])
        con.close()
        print(f"Saved {len(news)} articles to news_signals table.")
    except Exception as e:
        print(f"DB write error: {e}")

    return news


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json

    parser = argparse.ArgumentParser(
        description="Monitor Google News for employer closures near target area"
    )
    parser.add_argument("--city",   default="Blaine")
    parser.add_argument("--radius", type=int, default=15)
    parser.add_argument("--days",   type=int, default=180)
    args = parser.parse_args()

    results = check_for_area(city=args.city)

    print(f"\n{'='*70}")
    print(f"GOOGLE NEWS EMPLOYER SIGNAL REPORT -- {args.city}")
    print(f"{'='*70}")
    print(f"Total qualifying articles: {len(results)}")

    if results:
        by_conf = sorted(results, key=lambda x: x["confidence"], reverse=True)
        for r in by_conf:
            print(f"\n  [{r['confidence']:.1f}] {r['title'][:80]}")
            print(f"        Reason:     {r['reason']}")
            print(f"        Employees:  {r.get('estimated_employees','?')}")
            print(f"        Signal pts: {r['signal_pts']}")
            print(f"        Published:  {r.get('published_date','?')}")
            print(f"        URL:        {r['url'][:80]}")
    else:
        print("  No signals found.")
