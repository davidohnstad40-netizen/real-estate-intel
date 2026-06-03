"""
Skip Trace Agent
================
Strategy (tiered by cost):

Tier 0 -- Free scrape:  FastPeopleSearch.com via Playwright
Tier 1 -- Paid batch:   BatchSkipTracing.com ($0.18/record, no subscription)
          Upload CSV → they return phone, email, relatives, DOB
Tier 2 -- Paid API:     IDI/TLO/CLEAR if volume justifies it

Workflow for 52 properties:
  1. Run export_for_skiptracing()  → generates data/skip_trace_upload.csv
  2. Upload to batchskiptracing.com and download results CSV
  3. Run import_batch_results()    → parses results into contact_info table
  4. Optionally run free_scrape()  → fills gaps via FastPeopleSearch

BatchSkipTracing.com column mapping (their output format):
  Input:  FirstName, LastName, PropertyAddress, PropertyCity, PropertyState, PropertyZip
  Output: Phone1..Phone5, Email1..Email3, DOB, RelativeName1..3, MailingAddress

FastPeopleSearch URL pattern:
  https://www.fastpeoplesearch.com/name/{First}-{Last}_{City}-{State}
"""

import os, sys, re, csv, json, asyncio, time
from datetime import datetime
from typing import Optional

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from db.schema import get_db

# ── Schema ─────────────────────────────────────────────────────────────────────
CONTACT_DDL = """
CREATE TABLE IF NOT EXISTS contact_info (
    property_id   VARCHAR PRIMARY KEY,
    owner_name    VARCHAR,
    phone1        VARCHAR,
    phone2        VARCHAR,
    phone3        VARCHAR,
    email1        VARCHAR,
    email2        VARCHAR,
    mailing_addr  VARCHAR,
    dob           VARCHAR,
    relatives     VARCHAR,
    source        VARCHAR,   -- 'batch_skip', 'fastpeoplesearch', 'manual'
    confidence    VARCHAR,   -- 'high', 'medium', 'low'
    notes         TEXT,
    updated_at    TIMESTAMP DEFAULT current_timestamp
)
"""

def ensure_table(con):
    con.execute(CONTACT_DDL)

# ── EXPORT for BatchSkipTracing upload ─────────────────────────────────────────
def export_for_skiptracing(
    db_path: str = None,
    tiers: list = None,
    output_path: str = None,
) -> str:
    """
    Export T1/T2 (or specified tiers) to a CSV formatted for
    batchskiptracing.com upload.

    BatchSkipTracing input columns:
      FirstName, LastName, PropertyAddress, PropertyCity, PropertyState, PropertyZip
    """
    tiers = tiers or ["T1", "T2"]
    output_path = output_path or os.path.join(
        os.path.dirname(__file__), "..", "data", "skip_trace_upload.csv"
    )
    os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)

    con = get_db(db_path, read_only=True)
    rows = con.execute("""
        SELECT p.id, p.address, p.owner_name, p.city, p.state, p.zip,
               s.knock_tier, s.motivation_score
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        WHERE s.knock_tier IN ({})
        ORDER BY s.motivation_score DESC
    """.format(",".join(f"'{t}'" for t in tiers))).fetchall()
    con.close()

    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["FirstName", "LastName", "PropertyAddress",
                         "PropertyCity", "PropertyState", "PropertyZip",
                         "RefID", "KnockTier", "Score"])
        for row in rows:
            prop_id, addr, owner, city, state, zip_, tier, score = row
            # Parse first/last from owner string
            first, last = _parse_name(owner or "")
            street = addr.split(",")[0].strip()
            writer.writerow([first, last, street, city or "Blaine",
                             state or "MN", zip_ or "55449",
                             prop_id, tier, score])

    print(f"Exported {len(rows)} records -> {output_path}")
    print("Next: upload to https://www.batchskiptracing.com and download results CSV")
    return output_path


def _parse_name(owner: str) -> tuple[str, str]:
    """Best-effort split of 'FirstName LastName' or 'Last, First' owner strings."""
    owner = owner.strip()
    # Remove trust / LLC suffixes
    owner = re.sub(r"\b(Trust|LLC|LLP|Trustee|Properties)\b.*", "", owner, flags=re.I).strip()
    # Remove parenthetical notes
    owner = re.sub(r"\(.*?\)", "", owner).strip()
    # "Last, First" format
    if "," in owner:
        parts = owner.split(",", 1)
        return parts[1].strip().split()[0], parts[0].strip()
    # "First & Second LastName" -- take first person only
    owner = re.sub(r"\s*&\s*\w+\s+\w+$", "", owner).strip()
    parts = owner.split()
    if len(parts) >= 2:
        return parts[0], parts[-1]
    return owner, ""


# ── IMPORT BatchSkipTracing results CSV ────────────────────────────────────────
def import_batch_results(
    results_csv: str,
    db_path: str = None,
) -> int:
    """
    Parse the CSV downloaded from batchskiptracing.com and store in contact_info.
    Returns number of records imported.
    """
    con = get_db(db_path)
    ensure_table(con)

    imported = 0
    with open(results_csv, "r", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prop_id = row.get("RefID", "").strip()
            if not prop_id:
                continue

            # Collect phones (BatchSkipTracing returns Phone1..Phone5)
            phones = [row.get(f"Phone{i}", "").strip() for i in range(1, 6)]
            phones = [p for p in phones if p and p not in ("", "N/A")]

            emails = [row.get(f"Email{i}", "").strip() for i in range(1, 4)]
            emails = [e for e in emails if e and "@" in e]

            relatives = ", ".join(filter(None, [
                row.get(f"RelativeName{i}", "").strip() for i in range(1, 4)
            ]))

            mailing = " ".join(filter(None, [
                row.get("MailingAddress", ""),
                row.get("MailingCity", ""),
                row.get("MailingState", ""),
                row.get("MailingZip", ""),
            ])).strip()

            confidence = "high" if phones else ("medium" if emails else "low")

            con.execute("""
                INSERT OR REPLACE INTO contact_info
                (property_id, owner_name, phone1, phone2, phone3,
                 email1, email2, mailing_addr, dob, relatives,
                 source, confidence, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,current_timestamp)
            """, [
                prop_id,
                row.get("FirstName", "") + " " + row.get("LastName", ""),
                phones[0] if len(phones) > 0 else None,
                phones[1] if len(phones) > 1 else None,
                phones[2] if len(phones) > 2 else None,
                emails[0] if len(emails) > 0 else None,
                emails[1] if len(emails) > 1 else None,
                mailing or None,
                row.get("DOB", "") or None,
                relatives or None,
                "batch_skip",
                confidence,
            ])
            imported += 1

    con.close()
    print(f"Imported {imported} skip trace records from {results_csv}")
    return imported


# ── FREE SCRAPE via FastPeopleSearch ───────────────────────────────────────────
async def _scrape_one(page, first: str, last: str, city: str = "Blaine", state: str = "MN"):
    """Scrape a single person from FastPeopleSearch. Returns dict or None."""
    name_slug = f"{first}-{last}".lower().replace(" ", "-")
    loc_slug  = f"{city}-{state}".lower().replace(" ", "-")
    url = f"https://www.fastpeoplesearch.com/name/{name_slug}_{loc_slug}"

    try:
        await page.goto(url, timeout=20_000)
        await page.wait_for_timeout(3_000)
        text = await page.inner_text("body")
    except Exception as e:
        return {"error": str(e)}

    # Extract phones (pattern: (xxx) xxx-xxxx)
    phones = re.findall(r"\(\d{3}\)\s*\d{3}[-.\s]\d{4}", text)
    # Extract emails
    emails = re.findall(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", text)
    emails = [e for e in emails if "fastpeople" not in e.lower()]

    return {
        "phones": list(dict.fromkeys(phones))[:3],
        "emails": list(dict.fromkeys(emails))[:2],
        "raw_snippet": text[:500],
    }


async def free_scrape_async(
    db_path: str = None,
    tiers: list = None,
    delay_sec: float = 4.0,
):
    """
    Scrape FastPeopleSearch for all T1/T2 properties not yet in contact_info.
    Respectful delay between requests.
    """
    from playwright.async_api import async_playwright

    tiers = tiers or ["T1", "T2"]
    con = get_db(db_path)
    ensure_table(con)

    targets = con.execute("""
        SELECT p.id, p.owner_name, p.city, p.state
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        LEFT JOIN contact_info c ON p.id = c.property_id
        WHERE s.knock_tier IN ({}) AND c.property_id IS NULL
        ORDER BY s.motivation_score DESC
    """.format(",".join(f"'{t}'" for t in tiers))).fetchall()
    con.close()

    # Open a write connection separately for inserts
    write_con = get_db(db_path)
    ensure_table(write_con)
    write_con.close()

    print(f"Free-scraping {len(targets)} properties from FastPeopleSearch...")

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx     = await browser.new_context(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ))
        page = await ctx.new_page()

        for prop_id, owner, city, state in targets:
            first, last = _parse_name(owner or "")
            if not first or not last:
                print(f"  [SKIP] Cannot parse name: {owner}")
                continue

            print(f"  Scraping: {first} {last} ({city})...", end=" ", flush=True)
            result = await _scrape_one(page, first, last, city or "Blaine", state or "MN")

            phones = result.get("phones", [])
            emails = result.get("emails", [])
            print(f"{len(phones)} phone(s), {len(emails)} email(s)")

            con = get_db(db_path)
            con.execute("""
                INSERT OR REPLACE INTO contact_info
                (property_id, owner_name, phone1, phone2, phone3,
                 email1, email2, source, confidence, updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,current_timestamp)
            """, [
                prop_id, owner,
                phones[0] if len(phones) > 0 else None,
                phones[1] if len(phones) > 1 else None,
                phones[2] if len(phones) > 2 else None,
                emails[0] if len(emails) > 0 else None,
                emails[1] if len(emails) > 1 else None,
                "fastpeoplesearch",
                "medium" if phones else "low",
            ])
            con.close()
            await asyncio.sleep(delay_sec)

        await browser.close()

    print("Free scrape complete.")


def free_scrape(db_path: str = None, tiers: list = None):
    """Sync wrapper for the async scraper."""
    asyncio.run(free_scrape_async(db_path=db_path, tiers=tiers))


def get_contact(property_id: str, db_path: str = None) -> Optional[dict]:
    """Fetch contact info for a property. Returns dict or None."""
    con = get_db(db_path, read_only=True)
    rows = con.execute(
        "SELECT * FROM contact_info WHERE property_id = ?", [property_id]
    ).df()
    con.close()
    return rows.iloc[0].to_dict() if not rows.empty else None


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "export"
    if cmd == "export":
        export_for_skiptracing()
    elif cmd == "import" and len(sys.argv) > 2:
        import_batch_results(sys.argv[2])
    elif cmd == "scrape":
        free_scrape()
    else:
        print("Usage: python -m agents.skip_trace [export|import <csv>|scrape]")
