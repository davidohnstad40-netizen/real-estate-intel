"""
agents/letter_campaign.py

Generates printer-ready HTML letters for T1/T2/T3 properties.

Functions:
  generate_letter_text(prop_dict, investor_name, investor_phone)
      -> str: personalized 150-200 word letter body via Claude claude-haiku-4-5

  build_letter_html(properties, investor_name, investor_phone, output_path)
      -> None: builds one HTML file, one letter per print page

  export_letter_batch(db_path, tiers, investor_name, investor_phone, output_path)
      -> str: loads T1/T2 from DuckDB, calls build_letter_html, returns output_path
"""

import os
import sys
import json
import datetime
from pathlib import Path

import anthropic

# ---------------------------------------------------------------------------
# Path setup so this script works as __main__ or as an import
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_HERE)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.schema import get_db

# ---------------------------------------------------------------------------
# Claude client (lazy init)
# ---------------------------------------------------------------------------
_client = None

def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("ANTHROPIC_API_KEY env var not set")
        _client = anthropic.Anthropic(api_key=key)
    return _client


# ---------------------------------------------------------------------------
# Signal-specific letter prompts
# ---------------------------------------------------------------------------

LETTER_PROMPT = """You are a real estate investor writing a short, genuine, handwritten-style letter to a homeowner.

PROPERTY INFORMATION:
Address: {address}
Owner Name: {owner_name}
Owner First Name: {first_name}
Primary Motivation Signal: {primary_signal}
Estimated Equity: {est_equity}
Years Owned: {years_owned}
Score Factors: {score_factors}

SIGNAL GUIDANCE -- tailor the letter based on the primary signal:
- divorce / marital: Acknowledge that life changes sometimes call for a fresh start. Emphasize speed, simplicity, no showings, certainty of close. Be warm, never preachy.
- no_homestead / non_homestead / investor / non-primary: Note that you noticed this property may not be their primary residence. Focus on freeing up capital, simplifying their portfolio, no landlord headaches.
- elderly / estate / probate: Be respectful and warm. Emphasize ease for the family, no repairs needed, flexible timeline, cash offer. Express gratitude for a well-kept home.
- long_hold / long_hold_equity / high_equity: Acknowledge they've built significant equity over the years. Focus on crystallizing that value now, tax-advantaged timing, moving without the hassle of showings.
- peak_buyer / market_peak / seller_market: Highlight that the market is strong right now and that this is a good window to capitalize before conditions shift.
- generic (if none of the above match): Express genuine interest in the property, acknowledge they may not be thinking of selling, emphasize a no-pressure conversation.

LETTER REQUIREMENTS:
- 150 to 200 words in the body (do NOT include salutation or signature in the word count)
- Handwritten, genuine tone -- not a form letter, not corporate
- First sentence must NOT start with "I"
- Reference something specific: their years of ownership, the equity they've built, or the life signal -- make it feel personal
- End with a soft, no-pressure call to action: a phone call or text is enough
- Do NOT include: the date, the address header, the salutation ("Dear ..."), or the signature block -- only the letter body
- Do NOT use the phrase "as-is" or "hassle-free" -- they feel like marketing copy
- Write in first person from the investor

Output ONLY the letter body text. Nothing else."""


def _fmt_equity(val) -> str:
    if val is None:
        return "significant"
    try:
        return f"${float(val):,.0f}"
    except (TypeError, ValueError):
        return str(val)


def _fmt_years(val) -> str:
    if val is None:
        return "many years"
    try:
        return f"{float(val):.0f} years"
    except (TypeError, ValueError):
        return str(val)


def _extract_first_name(owner_name: str) -> str:
    """Best-effort first name extraction from full name strings like 'SMITH JOHN A'."""
    if not owner_name:
        return "Homeowner"
    parts = owner_name.strip().split()
    if len(parts) == 1:
        return parts[0].capitalize()
    # County records often store as LAST FIRST MIDDLE -- try second token
    # but fall back to first token if it looks like a first name (shorter)
    if len(parts) >= 2:
        # If first part is all-caps and short it's likely a last name
        candidate = parts[1].capitalize()
        # filter out obvious non-names
        skip = {"JR", "SR", "II", "III", "IV", "MR", "MRS", "DR", "EST", "TRUST", "LLC"}
        if candidate.upper() in skip and len(parts) > 2:
            candidate = parts[2].capitalize()
        return candidate
    return parts[0].capitalize()


def _parse_factors(score_factors) -> str:
    if not score_factors:
        return "No strong signals identified"
    if isinstance(score_factors, str):
        try:
            score_factors = json.loads(score_factors)
        except Exception:
            return score_factors
    if isinstance(score_factors, dict):
        return ", ".join(f"{k.replace('_', ' ')}: +{v}pts" for k, v in score_factors.items())
    return str(score_factors)


def generate_letter_text(
    prop_dict: dict,
    investor_name: str = "[Your Name]",
    investor_phone: str = "[Your Phone]",
) -> str:
    """
    Use Claude claude-haiku-4-5 to write a personalized 150-200 word letter body
    for the given property dict.

    The dict should contain keys: address, owner_name, primary_signal,
    est_equity_usd, years_owned, score_factors.

    Returns the letter body as a plain string (no salutation, no signature).
    """
    first_name = _extract_first_name(prop_dict.get("owner_name", ""))

    prompt = LETTER_PROMPT.format(
        address        = prop_dict.get("address", "the property"),
        owner_name     = prop_dict.get("owner_name", "Homeowner"),
        first_name     = first_name,
        primary_signal = prop_dict.get("primary_signal") or "general interest",
        est_equity     = _fmt_equity(prop_dict.get("est_equity_usd")),
        years_owned    = _fmt_years(prop_dict.get("years_owned")),
        score_factors  = _parse_factors(prop_dict.get("score_factors")),
    )

    resp = _get_client().messages.create(
        model      = "claude-haiku-4-5",
        max_tokens = 400,
        messages   = [{"role": "user", "content": prompt}],
    )
    return resp.content[0].text.strip()


# ---------------------------------------------------------------------------
# HTML builder
# ---------------------------------------------------------------------------

_HTML_TEMPLATE = """\
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <title>Letter Campaign</title>
  <style>
    /* ---- Reset ---- */
    *, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}

    body {{
      font-family: Georgia, "Times New Roman", serif;
      font-size: 13pt;
      color: #1a1a1a;
      background: #fff;
    }}

    /* ---- One page per letter ---- */
    .letter-page {{
      width: 8.5in;
      min-height: 11in;
      padding: 1.5in;
      page-break-after: always;
      position: relative;
    }}

    /* Remove page-break on the very last letter */
    .letter-page:last-child {{
      page-break-after: avoid;
    }}

    /* ---- Envelope address (top, small) ---- */
    .envelope-address {{
      font-size: 9pt;
      color: #555;
      margin-bottom: 0.6in;
      line-height: 1.5;
      font-style: italic;
    }}

    /* ---- Date ---- */
    .letter-date {{
      font-size: 12pt;
      margin-bottom: 0.35in;
      color: #222;
    }}

    /* ---- Salutation ---- */
    .salutation {{
      font-size: 13pt;
      margin-bottom: 0.25in;
    }}

    /* ---- Body ---- */
    .letter-body {{
      font-size: 13pt;
      line-height: 1.85;
      white-space: pre-wrap;
      margin-bottom: 0.5in;
    }}

    /* ---- Closing / signature ---- */
    .closing {{
      font-size: 13pt;
      margin-bottom: 0.1in;
    }}

    .signature-block {{
      margin-top: 0.5in;
      font-size: 13pt;
      line-height: 1.7;
    }}

    .signature-name {{
      font-size: 14pt;
      font-style: italic;
    }}

    /* ---- Tier badge (screen only, hidden at print) ---- */
    .tier-badge {{
      position: absolute;
      top: 0.5in;
      right: 0.5in;
      font-size: 9pt;
      padding: 3px 8px;
      border-radius: 4px;
      font-family: Arial, sans-serif;
      font-weight: bold;
    }}
    .tier-T1  {{ background: #fee2e2; color: #b91c1c; border: 1px solid #fca5a5; }}
    .tier-T2  {{ background: #fef3c7; color: #92400e; border: 1px solid #fcd34d; }}
    .tier-T3  {{ background: #dbeafe; color: #1e40af; border: 1px solid #93c5fd; }}
    .tier-TBD {{ background: #f3f4f6; color: #374151; border: 1px solid #d1d5db; }}

    /* ---- Print overrides ---- */
    @media print {{
      body {{ margin: 0; }}
      .letter-page {{
        margin: 0;
        padding: 1.5in;
        page-break-after: always;
      }}
      .tier-badge {{ display: none; }}
    }}
  </style>
</head>
<body>
{letters}
</body>
</html>
"""

_LETTER_PAGE_TEMPLATE = """\
<div class="letter-page">
  <span class="tier-badge tier-{tier_safe}">{tier}</span>

  <div class="envelope-address">
    {address}<br/>
    {city}, {state} {zip}
  </div>

  <div class="letter-date">{date}</div>

  <div class="salutation">Dear {first_name},</div>

  <div class="letter-body">{body}</div>

  <div class="closing">Best regards,</div>

  <div class="signature-block">
    <div class="signature-name">{investor_name}</div>
    <div>{investor_phone}</div>
  </div>
</div>
"""


def build_letter_html(
    properties: list[dict],
    investor_name: str = "[Your Name]",
    investor_phone: str = "[Your Phone]",
    output_path: str = None,
) -> None:
    """
    Build a single printer-ready HTML file with one letter per page.

    Parameters
    ----------
    properties    : list of dicts with at minimum: address, owner_name,
                    knock_tier, primary_signal, est_equity_usd,
                    years_owned, score_factors.
                    Optional: city, state, zip.
    investor_name : name to appear in the signature block
    investor_phone: phone to appear in the signature block
    output_path   : file path to write the HTML to
    """
    if output_path is None:
        output_path = os.path.join(
            _ROOT, "output",
            f"letters_{datetime.date.today().isoformat()}.html",
        )

    Path(os.path.dirname(output_path)).mkdir(parents=True, exist_ok=True)

    today_str = datetime.date.today().strftime("%B %d, %Y")
    letter_pages = []

    for i, prop in enumerate(properties, start=1):
        address    = prop.get("address", "Unknown Address")
        city       = prop.get("city", "Blaine")
        state      = prop.get("state", "MN")
        zipcode    = prop.get("zip", "")
        tier       = prop.get("knock_tier", "TBD")
        tier_safe  = tier.replace("/", "-")
        first_name = _extract_first_name(prop.get("owner_name", ""))

        print(f"  [{i}/{len(properties)}] Generating letter for {address} ({tier}) …")

        try:
            body = generate_letter_text(prop, investor_name, investor_phone)
        except Exception as exc:
            body = (
                f"[Letter generation failed: {exc}]\n\n"
                "Please feel free to reach out if you have any interest in a "
                "private, no-pressure conversation about your property."
            )

        page = _LETTER_PAGE_TEMPLATE.format(
            address       = address,
            city          = city,
            state         = state,
            zip           = zipcode,
            tier          = tier,
            tier_safe     = tier_safe,
            date          = today_str,
            first_name    = first_name,
            body          = body,
            investor_name = investor_name,
            investor_phone= investor_phone,
        )
        letter_pages.append(page)

    html = _HTML_TEMPLATE.format(letters="\n".join(letter_pages))

    with open(output_path, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"\nWrote {len(letter_pages)} letters to: {output_path}")


# ---------------------------------------------------------------------------
# Batch export
# ---------------------------------------------------------------------------

def export_letter_batch(
    db_path: str = None,
    tiers: list[str] = None,
    investor_name: str = "[Your Name]",
    investor_phone: str = "[Your Phone]",
    output_path: str = None,
) -> str:
    """
    Load T1/T2 (or specified tiers) from DuckDB, generate letters, return output path.

    Parameters
    ----------
    db_path      : path to the DuckDB file (default: DB_PATH env var or ./data/rei.duckdb)
    tiers        : list of tier strings to include, e.g. ["T1", "T2"]. Default: ["T1", "T2"]
    investor_name: investor name for signatures
    investor_phone: investor phone for signatures
    output_path  : where to write the HTML file. Default: output/letters_YYYY-MM-DD.html

    Returns
    -------
    str: the output_path written
    """
    if tiers is None:
        tiers = ["T1", "T2"]

    if output_path is None:
        tier_label = "_".join(sorted(tiers))
        output_path = os.path.join(
            _ROOT, "output",
            f"letters_{tier_label}_{datetime.date.today().isoformat()}.html",
        )

    db_path = db_path or os.getenv("DB_PATH", os.path.join(_ROOT, "data", "rei.duckdb"))

    print(f"Loading {tiers} properties from {db_path} …")
    con = get_db(db_path, read_only=True)

    placeholders = ", ".join("?" for _ in tiers)
    rows = con.execute(
        f"""
        SELECT
            p.id,
            p.address,
            p.city,
            p.state,
            p.zip,
            p.owner_name,
            p.years_owned,
            ps.knock_tier,
            ps.primary_signal,
            ps.est_equity_usd,
            ps.score_factors,
            ps.motivation_score
        FROM properties p
        JOIN property_scores ps ON ps.id = p.id
        WHERE ps.knock_tier IN ({placeholders})
        ORDER BY ps.motivation_score DESC
        """,
        tiers,
    ).fetchall()
    con.close()

    if not rows:
        print(f"No properties found for tiers: {tiers}")
        return output_path

    properties = [
        {
            "id":              r[0],
            "address":         r[1],
            "city":            r[2] or "Blaine",
            "state":           r[3] or "MN",
            "zip":             r[4] or "",
            "owner_name":      r[5] or "",
            "years_owned":     r[6],
            "knock_tier":      r[7],
            "primary_signal":  r[8],
            "est_equity_usd":  r[9],
            "score_factors":   r[10],
            "motivation_score":r[11],
        }
        for r in rows
    ]

    print(f"Found {len(properties)} properties. Generating letters …\n")
    build_letter_html(properties, investor_name, investor_phone, output_path)

    return output_path


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Generate letter campaign HTML")
    parser.add_argument("--db",     default=None,            help="Path to rei.duckdb")
    parser.add_argument("--tiers",  default="T1,T2",         help="Comma-separated tiers, e.g. T1,T2")
    parser.add_argument("--name",   default="[Your Name]",   help="Investor name for signature")
    parser.add_argument("--phone",  default="[Your Phone]",  help="Investor phone for signature")
    parser.add_argument("--out",    default=None,            help="Output HTML path")
    args = parser.parse_args()

    tier_list = [t.strip() for t in args.tiers.split(",") if t.strip()]

    out = export_letter_batch(
        db_path       = args.db,
        tiers         = tier_list,
        investor_name = args.name,
        investor_phone= args.phone,
        output_path   = args.out,
    )
    print(f"\nDone. Open in your browser or print: {out}")
