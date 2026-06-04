"""
Bankruptcy Filing Signal Agent
================================
Checks for Chapter 7 or Chapter 13 bankruptcy filings by property owners.

Chapter 7 = liquidation (forced asset sale, strong sell signal)
Chapter 13 = reorganization (debt repayment plan, moderate sell signal)

Primary source: Inforuptcy.com (free public search)
  https://www.inforuptcy.com/filings-search?query={first}+{last}&state=MN&chapter=7
Fallback: CourtListener.com RECAP bankruptcy search

Uses Playwright (async, headless=True -- no bot-gating on public court records).

Signal weights:
  +30 pts: Chapter 7, confidence >= 0.8 (confirmed, recent, specific)
  +20 pts: Chapter 7, confidence 0.5-0.8
  +15 pts: Chapter 13, confidence >= 0.8
  +10 pts: Chapter 13, confidence 0.5-0.8
  +0  pts: confidence < 0.5 (too many false positives)

Usage:
    python -m agents.bankruptcy                      # checks T1/T2 owners
    python -m agents.bankruptcy --tier T1            # T1 only
"""

import sys, os, re, json, asyncio
from datetime import date, datetime, timedelta
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db

# ── constants ──────────────────────────────────────────────────────────────────

BANKRUPTCY_SIGNALS_DDL = """
CREATE TABLE IF NOT EXISTS bankruptcy_signals (
    property_id   VARCHAR,
    owner_name    VARCHAR,
    case_number   VARCHAR,
    chapter       INTEGER,
    filed_date    DATE,
    status        VARCHAR,
    confidence    DOUBLE,
    signal_pts    INTEGER,
    reason        TEXT,
    detected_at   TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (property_id, case_number)
)
"""

# 25 most common US surnames -- common last names need extra confirmation
# before we call it a match (address or DOB needed).
TOP_COMMON_NAMES = {
    "Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller",
    "Davis", "Rodriguez", "Martinez", "Hernandez", "Lopez", "Gonzalez",
    "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
    "Lee", "Perez", "Thompson", "White", "Harris",
}

# Inforuptcy result selectors (CSS / text patterns)
INFORUPTCY_BASE = "https://www.inforuptcy.com/filings-search"
COURTLISTENER_BASE = "https://www.courtlistener.com/api/rest/v3/dockets/"

# DuckDB path default
_DEFAULT_DB = os.getenv("DB_PATH", "./data/rei.duckdb")


# ── name utilities ─────────────────────────────────────────────────────────────

def _parse_owner_name(owner_name: str) -> tuple[str, str]:
    """
    Parse 'LAST FIRST' or 'FIRST LAST' format into (first, last).
    Anoka County typically stores names as 'LAST FIRST' or 'LAST, FIRST'.

    Returns ("", "") if parsing fails.
    """
    if not owner_name:
        return "", ""
    name = owner_name.strip()

    # Remove corporate noise
    for noise in ("LLC", "INC", "CORP", "TRUST", "ESTATE", "ETAL",
                  "TRUSTEES", "TRUSTEE", "ET AL"):
        name = re.sub(r"\b" + noise + r"\b", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\s+", " ", name).strip()

    # Format: "LAST, FIRST" or "LAST,FIRST"
    if "," in name:
        parts = [p.strip() for p in name.split(",", 1)]
        if len(parts) == 2 and parts[0] and parts[1]:
            return parts[1].split()[0].title(), parts[0].title()

    # Format: "LAST FIRST" (two or more tokens, first token is all-caps last name)
    tokens = name.split()
    if len(tokens) >= 2:
        # Heuristic: if first token is all uppercase and >= 4 chars -> LAST FIRST
        if tokens[0].isupper() and len(tokens[0]) >= 4:
            return tokens[1].title(), tokens[0].title()
        # Otherwise assume FIRST LAST
        return tokens[0].title(), tokens[-1].title()

    return name.title(), ""


def _normalize_name(name: str) -> str:
    """Lowercase, strip punctuation for comparison."""
    return re.sub(r"[^a-z\s]", "", name.lower()).strip()


# ── scraping helpers ───────────────────────────────────────────────────────────

async def _scrape_inforuptcy(
    page,
    first: str,
    last: str,
    state: str = "MN",
    chapter: int = 7,
) -> list[dict]:
    """
    Scrape Inforuptcy.com for a person's bankruptcy filings.
    Returns list of raw result dicts.
    """
    url = (
        f"{INFORUPTCY_BASE}?query={first}+{last}&state={state}&chapter={chapter}"
    )
    results = []
    try:
        await page.goto(url, timeout=25_000)
        await page.wait_for_timeout(3_000)
        text = await page.inner_text("body")

        # Inforuptcy lists: Debtor Name | Case Number | District | Filed | Chapter
        # Pattern: looks for lines where case number format is XX-XXXXX or X:XX-bkXXXXX
        case_pattern = re.compile(
            r"(\d{1,2}[-:]\d{2}-(?:bk|cv)?\d+|\d{4}-\d+)",
            re.IGNORECASE,
        )
        # Date pattern: MM/DD/YYYY or Month DD, YYYY
        date_pattern = re.compile(
            r"(\d{1,2}/\d{1,2}/\d{4}|\w+ \d{1,2},?\s*\d{4})",
        )
        # Chapter pattern in context
        chapter_pattern = re.compile(r"\bChapter\s+(7|11|13)\b", re.IGNORECASE)

        lines = [l.strip() for l in text.splitlines() if l.strip()]

        name_lower = f"{first} {last}".lower()
        last_lower  = last.lower()

        for i, line in enumerate(lines):
            # Look for a line that contains the debtor name
            if last_lower not in line.lower():
                continue

            # Grab context window around this line
            context = " ".join(lines[max(0, i-2):i+6])

            case_m  = case_pattern.search(context)
            date_m  = date_pattern.search(context)
            chap_m  = chapter_pattern.search(context)

            if not case_m:
                continue  # no recognizable case number -> skip

            filed_date = None
            if date_m:
                raw_d = date_m.group(1)
                for fmt in ("%m/%d/%Y", "%B %d %Y", "%B %d, %Y",
                            "%b %d %Y", "%b %d, %Y"):
                    try:
                        filed_date = datetime.strptime(raw_d.strip(), fmt).date()
                        break
                    except ValueError:
                        pass

            detected_chapter = int(chap_m.group(1)) if chap_m else chapter

            # Status: look for Active, Discharged, Dismissed, Closed in context
            status = "Unknown"
            for kw in ("Discharged", "Dismissed", "Closed", "Active",
                        "Open", "Converted"):
                if kw.lower() in context.lower():
                    status = kw
                    break

            results.append({
                "debtor_name": line.strip()[:80],
                "case_number": case_m.group(0),
                "filed_date":  filed_date,
                "chapter":     detected_chapter,
                "status":      status,
                "district":    state,
                "source":      "inforuptcy.com",
                "raw_context": context[:400],
            })

    except Exception as e:
        print(f"  Inforuptcy scrape error ({first} {last}): {e}")

    return results


async def _scrape_courtlistener(
    page,
    first: str,
    last: str,
    state: str = "MN",
) -> list[dict]:
    """
    Fallback: CourtListener RECAP bankruptcy search.
    Uses the public search page (no API key required for basic search).
    """
    query = f"{first} {last}"
    url = (
        f"https://www.courtlistener.com/?q={query.replace(' ','+')}"
        f"&type=r&order_by=score+desc&filed_after=&nature_of_suit=bk"
    )
    results = []
    try:
        await page.goto(url, timeout=25_000)
        await page.wait_for_timeout(3_000)
        text = await page.inner_text("body")

        last_lower = last.lower()
        case_pattern = re.compile(r"\d{1,2}-\d{4,}", re.IGNORECASE)
        date_pattern  = re.compile(r"Filed:\s*(\w+ \d+, \d{4})", re.IGNORECASE)
        chap_pattern  = re.compile(r"\bChapter\s+(7|11|13)\b", re.IGNORECASE)

        lines = [l.strip() for l in text.splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if last_lower not in line.lower():
                continue
            context = " ".join(lines[max(0, i-2):i+6])

            case_m = case_pattern.search(context)
            date_m = date_pattern.search(context)
            chap_m = chap_pattern.search(context)

            if not case_m:
                continue

            filed_date = None
            if date_m:
                try:
                    filed_date = datetime.strptime(
                        date_m.group(1), "%B %d, %Y"
                    ).date()
                except ValueError:
                    pass

            results.append({
                "debtor_name": line.strip()[:80],
                "case_number": case_m.group(0),
                "filed_date":  filed_date,
                "chapter":     int(chap_m.group(1)) if chap_m else 7,
                "status":      "Unknown",
                "district":    state,
                "source":      "courtlistener.com",
                "raw_context": context[:400],
            })

    except Exception as e:
        print(f"  CourtListener scrape error ({first} {last}): {e}")

    return results


# ── public API ─────────────────────────────────────────────────────────────────

async def search_bankruptcy_async(
    page,
    first: str,
    last: str,
    state: str = "MN",
    years_back: int = 3,
    property_address: str = "",
) -> list[dict]:
    """
    Search for bankruptcy filings for a person.

    Handles common-name false positives by adjusting confidence:
      - Uncommon name OR address match in filing -> confidence 1.0
      - Name matches, no address confirmation -> confidence 0.6
      - Common last name, no address match -> confidence 0.3

    Returns list of {case_number, debtor_name, filed_date, chapter,
                      status, confidence, reason}
    """
    cutoff = date.today() - timedelta(days=years_back * 365)
    is_common = last.title() in TOP_COMMON_NAMES

    # Try Inforuptcy for both Ch7 and Ch13
    raw = []
    for ch in (7, 13):
        raw.extend(await _scrape_inforuptcy(page, first, last, state, ch))
    if not raw:
        raw = await _scrape_courtlistener(page, first, last, state)

    results = []
    for r in raw:
        # Date filter
        if r.get("filed_date") and r["filed_date"] < cutoff:
            continue

        debtor = r.get("debtor_name", "")
        first_n  = _normalize_name(first)
        last_n   = _normalize_name(last)
        debtor_n = _normalize_name(debtor)

        # Name match check
        name_matches = (last_n in debtor_n and first_n in debtor_n)
        last_only    = (last_n in debtor_n)

        if not last_only:
            continue  # no last-name match at all -> skip

        # Address match in raw context (strong confirmation)
        addr_hint = ""
        if property_address:
            addr_num = property_address.split()[0] if property_address else ""
            if addr_num and addr_num in r.get("raw_context", ""):
                addr_hint = addr_num

        # Confidence assignment
        if addr_hint:
            confidence = 1.0
            reason = (f"Chapter {r['chapter']} filing: name matches AND "
                      f"property address number '{addr_hint}' found in filing context")
        elif name_matches and not is_common:
            confidence = 0.9
            reason = (f"Chapter {r['chapter']} filing: full name match "
                      f"'{first} {last}' (uncommon name -- low false-positive risk)")
        elif name_matches and is_common:
            confidence = 0.6
            reason = (f"Chapter {r['chapter']} filing: full name match but "
                      f"'{last}' is a top-25 common name -- needs address confirmation")
        elif last_only and not is_common:
            confidence = 0.7
            reason = (f"Chapter {r['chapter']} filing: last name '{last}' match "
                      f"(uncommon) -- first name differs, possible match")
        else:
            confidence = 0.3
            reason = (f"Chapter {r['chapter']} filing: common last name '{last}' "
                      f"only -- high false-positive risk")

        results.append({
            "case_number":  r["case_number"],
            "debtor_name":  debtor,
            "filed_date":   str(r["filed_date"]) if r.get("filed_date") else "",
            "chapter":      r["chapter"],
            "status":       r.get("status", "Unknown"),
            "district":     r.get("district", state),
            "confidence":   round(confidence, 2),
            "reason":       reason,
            "source":       r.get("source", "unknown"),
        })

    return results


def search_bankruptcy(
    first: str,
    last: str,
    state: str = "MN",
    years_back: int = 3,
) -> list[dict]:
    """Sync wrapper for search_bankruptcy_async (launches its own browser)."""
    async def _run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )
            ctx  = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ))
            page = await ctx.new_page()
            result = await search_bankruptcy_async(
                page, first, last, state, years_back
            )
            await browser.close()
            return result

    return asyncio.run(_run())


def score_bankruptcy_signal(filings: list[dict]) -> tuple[int, str]:
    """
    Score a list of bankruptcy filings for a property owner.

    Returns (signal_pts, reason_string).
    Only filings with confidence >= 0.5 contribute to score.
    """
    if not filings:
        return 0, "No bankruptcy filings found"

    best_pts    = 0
    best_reason = "Filings found but confidence too low (< 0.5)"

    for f in filings:
        conf    = f.get("confidence", 0)
        chapter = f.get("chapter", 0)

        if conf < 0.5:
            continue

        if chapter == 7 and conf >= 0.8:
            pts    = 30
            reason = (f"+30: Chapter 7 LIQUIDATION confirmed "
                      f"(conf={conf:.1f}) -- case {f['case_number']}, "
                      f"filed {f.get('filed_date','?')}")
        elif chapter == 7 and 0.5 <= conf < 0.8:
            pts    = 20
            reason = (f"+20: Chapter 7 likely (conf={conf:.1f}) -- "
                      f"case {f['case_number']}, needs address verification")
        elif chapter == 13 and conf >= 0.8:
            pts    = 15
            reason = (f"+15: Chapter 13 REORGANIZATION confirmed "
                      f"(conf={conf:.1f}) -- case {f['case_number']}, "
                      f"owner under debt pressure")
        elif chapter == 13 and 0.5 <= conf < 0.8:
            pts    = 10
            reason = (f"+10: Chapter 13 likely (conf={conf:.1f}) -- "
                      f"case {f['case_number']}, possible financial stress")
        else:
            pts    = 0
            reason = f"Chapter {chapter} -- scoring not applicable"

        if pts > best_pts:
            best_pts    = pts
            best_reason = reason

    return best_pts, best_reason


def check_owner(property_id: str, db_path: str = None) -> dict | None:
    """
    Look up a property owner, search for bankruptcy filings, save result.
    Returns the best match dict or None.
    """
    con = get_db(db_path, read_only=True)
    row = con.execute(
        "SELECT p.id, p.owner_name, p.address FROM properties p WHERE p.id = ?",
        [property_id]
    ).fetchone()
    con.close()

    if not row:
        print(f"  Property {property_id} not found in DB.")
        return None

    prop_id, owner_name, address = row
    if not owner_name:
        print(f"  No owner name for property {property_id}.")
        return None

    first, last = _parse_owner_name(owner_name)
    if not first or not last:
        print(f"  Could not parse name: '{owner_name}'")
        return None

    print(f"  Checking bankruptcy: {first} {last} ({owner_name}) @ {address}")

    async def _run():
        from playwright.async_api import async_playwright
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )
            ctx  = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ))
            page = await ctx.new_page()
            filings = await search_bankruptcy_async(
                page, first, last, property_address=address or ""
            )
            await browser.close()
            return filings

    filings = asyncio.run(_run())

    if not filings:
        print(f"  No filings found for {first} {last}.")
        return None

    signal_pts, reason = score_bankruptcy_signal(filings)
    best_filing = max(filings, key=lambda x: x.get("confidence", 0))

    result = {
        "property_id":  prop_id,
        "owner_name":   owner_name,
        "signal_pts":   signal_pts,
        "reason":       reason,
        **best_filing,
    }

    # Save to DB
    try:
        write_con = get_db(db_path)
        write_con.execute(BANKRUPTCY_SIGNALS_DDL)
        for f in filings:
            pts, _ = score_bankruptcy_signal([f])
            write_con.execute("""
                INSERT OR REPLACE INTO bankruptcy_signals
                (property_id, owner_name, case_number, chapter, filed_date,
                 status, confidence, signal_pts, reason)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, [
                prop_id,
                owner_name,
                f["case_number"],
                f["chapter"],
                f.get("filed_date") or None,
                f["status"],
                f["confidence"],
                pts,
                f["reason"],
            ])
        write_con.close()
        print(f"  Saved {len(filings)} filing(s) for {owner_name}.")
    except Exception as e:
        print(f"  DB write error for {prop_id}: {e}")

    return result


def check_all_owners(tiers: list = None, db_path: str = None) -> list[dict]:
    """
    Check bankruptcy filings for all T1/T2 (or specified tier) property owners.
    Applies a 2-second delay between requests to be respectful.
    Returns list of matches found.
    """
    tiers = tiers or ["T1", "T2"]
    placeholders = ",".join("?" * len(tiers))

    con = get_db(db_path, read_only=True)
    targets = con.execute(
        f"SELECT p.id, p.owner_name, p.address FROM properties p "
        f"LEFT JOIN property_scores s ON p.id = s.id "
        f"WHERE s.knock_tier IN ({placeholders}) "
        f"AND p.owner_name IS NOT NULL "
        f"AND p.owner_name NOT LIKE '%LLC%' "
        f"AND p.owner_name NOT LIKE '%Trust%' "
        f"AND p.owner_name NOT LIKE '%TRUST%' "
        f"ORDER BY s.motivation_score DESC NULLS LAST",
        tiers,
    ).fetchall()
    con.close()

    print(f"Checking bankruptcy for {len(targets)} {'/'.join(tiers)} owners...")

    async def _run_all():
        from playwright.async_api import async_playwright
        matches = []

        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=True, args=["--no-sandbox"]
            )
            ctx  = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            ))
            page = await ctx.new_page()

            for prop_id, owner_name, address in targets:
                first, last = _parse_owner_name(owner_name or "")
                if not first or not last:
                    continue

                print(f"  {first} {last} ({owner_name}) ...", end=" ", flush=True)

                filings = await search_bankruptcy_async(
                    page, first, last,
                    property_address=address or "",
                )

                if filings:
                    signal_pts, reason = score_bankruptcy_signal(filings)
                    if signal_pts > 0:
                        best = max(filings, key=lambda x: x.get("confidence", 0))
                        print(f"MATCH! Ch{best['chapter']} "
                              f"conf={best['confidence']:.1f} +{signal_pts}pts")
                        matches.append({
                            "property_id": prop_id,
                            "owner_name":  owner_name,
                            "signal_pts":  signal_pts,
                            "reason":      reason,
                            **best,
                        })

                        # Save to DB
                        try:
                            write_con = get_db(db_path)
                            write_con.execute(BANKRUPTCY_SIGNALS_DDL)
                            for f in filings:
                                pts, _ = score_bankruptcy_signal([f])
                                write_con.execute("""
                                    INSERT OR REPLACE INTO bankruptcy_signals
                                    (property_id, owner_name, case_number, chapter,
                                     filed_date, status, confidence, signal_pts, reason)
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, [
                                    prop_id, owner_name, f["case_number"],
                                    f["chapter"], f.get("filed_date") or None,
                                    f["status"], f["confidence"], pts, f["reason"],
                                ])
                            write_con.close()
                        except Exception as e:
                            print(f"  DB error: {e}")
                    else:
                        print(f"filings found but confidence too low")
                else:
                    print("no filings")

                await asyncio.sleep(2)  # 2-second delay between requests

            await browser.close()

        return matches

    return asyncio.run(_run_all())


# ── main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(
        description="Check property owners for bankruptcy filings"
    )
    parser.add_argument("--tier", action="append", dest="tiers",
                        help="Tier to check (T1, T2, T3). Can repeat.")
    args = parser.parse_args()

    tiers   = args.tiers or ["T1", "T2"]
    matches = check_all_owners(tiers=tiers)

    print(f"\n{'='*70}")
    print(f"BANKRUPTCY SIGNAL REPORT")
    print(f"{'='*70}")
    print(f"Total matches: {len(matches)}")
    for m in matches:
        print(f"\n  {m['owner_name']} -- {m.get('address','')}")
        print(f"    Case:       {m.get('case_number','?')} "
              f"(Chapter {m.get('chapter','?')})")
        print(f"    Filed:      {m.get('filed_date','?')}")
        print(f"    Status:     {m.get('status','?')}")
        print(f"    Confidence: {m.get('confidence',0):.1f}")
        print(f"    Signal pts: +{m['signal_pts']}")
        print(f"    Reason:     {m['reason']}")
