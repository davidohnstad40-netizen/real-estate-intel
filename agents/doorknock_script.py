"""
Door-Knock Script Generator
============================
Generates personalized door-knock conversation starters and letters
for each T1/T2 property using Claude AI.

Each script is tailored to the specific motivation signal:
- Divorce:      Acknowledge life transitions, emphasize speed/simplicity
- No homestead: Assume they've moved, approach as a service
- Elderly/estate: Respectful, emphasize ease and fairness for family
- Peak buyer:   Focus on stopping the bleeding / crystallizing equity
- Long hold:    Emphasize tax-advantaged exit, legacy, what's next
"""

import os, sys, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import anthropic
from db.schema import get_db

DOOR_SCRIPT_PROMPT = """You are a real estate investor coach. Write a personalized door-knock script for a specific property owner.

PROPERTY:
Address: {address}
Owner: {owner_name}
Motivation Score: {score}/100  |  Tier: {tier}
Years Owned: {years_owned}
Est. Equity: {equity}
Primary Signal: {signal}
Score Factors: {factors}

Write THREE things:

1. DOOR OPENER (10-15 seconds when they answer)
   - Introduce yourself naturally (use first name only, "Hi, I'm [Name]")
   - One sentence on why you're specifically at THEIR door (reference signal subtly)
   - One soft question to open dialogue
   - Do NOT mention "motivated seller" or "I know you want to sell"

2. FOLLOW-UP PIVOT (if they're willing to talk — 30-60 seconds)
   - Brief explanation of how you buy (no agents, no fees, quick close)
   - One question about their timeline or plans for the property
   - How to handle "I'm not interested" gracefully (leave door open)

3. LEAVE-BEHIND CARD TEXT (what to write on your business card)
   - 2-3 lines max
   - Specific to their situation

Tone: Warm, peer-level, NOT salesy. You're a neighbor who buys houses, not a pushy investor.
Do NOT use: "motivated seller", "distressed", "desperate", "I heard you're selling", "I know your situation"
DO use: natural, conversational language appropriate for someone's front door at 10am Saturday.

Format with clear headers for each section."""


LETTER_PROMPT = """You are a real estate investor. Write a personalized handwritten-style letter for a property owner.

PROPERTY:
Address: {address}
Owner: {owner_name}
Motivation Score: {score}/100  |  Tier: {tier}
Years Owned: {years_owned}
Est. Equity: {equity}
Primary Signal: {signal}

Write a SHORT letter (150-200 words max) that:
1. Opens with a specific compliment about the neighborhood/street (real, not generic)
2. Briefly introduces who you are (local investor, not a company/flipper)
3. Makes a soft, non-pressuring offer to have a conversation
4. Includes a clear but low-pressure call to action (text/call)
5. Closes warmly

The letter should feel handwritten and personal, NOT like a mass mailer.
It should reference something specific about their property or street.
Do NOT use: form letter language, "I'm reaching out because...", "distressed property"
Leave [PHONE] and [NAME] as placeholders.

Format: Just the letter text, no headers."""


def generate_door_script(prop: dict) -> dict:
    """Generate door script + letter for one property."""
    client = anthropic.Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY", ""))

    factors = prop.get("score_factors") or {}
    if isinstance(factors, str):
        try: factors = json.loads(factors)
        except: factors = {}
    factors_str = ", ".join(f"{k.replace('_',' ')}: +{v}pts" for k, v in factors.items()) or "No strong signals"

    equity = "Unknown"
    if prop.get("est_equity_usd") and prop.get("equity_pct"):
        equity = f"${prop['est_equity_usd']:,.0f} ({prop['equity_pct']:.0%})"

    fmt = dict(
        address      = prop.get("address", ""),
        owner_name   = prop.get("owner_name", ""),
        score        = prop.get("motivation_score", 0),
        tier         = prop.get("knock_tier", "TBD"),
        years_owned  = f"{prop['years_owned']:.0f} years" if prop.get("years_owned") else "Unknown",
        equity       = equity,
        signal       = prop.get("primary_signal", "No specific signal"),
        factors      = factors_str,
    )

    door_resp   = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=600,
        messages=[{"role": "user", "content": DOOR_SCRIPT_PROMPT.format(**fmt)}],
    )
    letter_resp = client.messages.create(
        model="claude-haiku-4-5",
        max_tokens=400,
        messages=[{"role": "user", "content": LETTER_PROMPT.format(**fmt)}],
    )

    return {
        "door_script": door_resp.content[0].text.strip(),
        "letter":      letter_resp.content[0].text.strip(),
    }


def generate_all_scripts(db_path: str = None, tiers: list = None) -> dict:
    """Generate scripts for all T1/T2 properties. Returns {address: {door_script, letter}}."""
    tiers = tiers or ["T1", "T2"]
    con = get_db(db_path, read_only=True)
    props = con.execute("""
        SELECT p.id, p.address, p.owner_name, p.years_owned,
               s.motivation_score, s.knock_tier, s.primary_signal,
               s.score_factors, s.est_equity_usd, s.equity_pct
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        WHERE s.knock_tier IN ({})
        ORDER BY s.motivation_score DESC
    """.format(",".join(f"'{t}'" for t in tiers))).df()
    con.close()

    results = {}
    for _, row in props.iterrows():
        print(f"  Generating script for {row.address}...", flush=True)
        try:
            scripts = generate_door_script(row.to_dict())
            results[row.address] = scripts
        except Exception as e:
            results[row.address] = {"error": str(e)}

    # Save to file
    out = os.path.join(os.path.dirname(__file__), "..", "data", "door_scripts.json")
    with open(out, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, ensure_ascii=False)
    print(f"\nSaved {len(results)} scripts -> {out}")
    return results


def get_script(property_id: str, db_path: str = None) -> dict | None:
    """Load cached door script for a property from door_scripts.json."""
    script_file = os.path.join(os.path.dirname(__file__), "..", "data", "door_scripts.json")
    if not os.path.exists(script_file):
        return None
    with open(script_file, encoding="utf-8") as f:
        data = json.load(f)

    con = get_db(db_path, read_only=True)
    row = con.execute("SELECT address FROM properties WHERE id = ?", [property_id]).fetchone()
    con.close()
    if not row:
        return None
    return data.get(row[0])


if __name__ == "__main__":
    if not os.getenv("ANTHROPIC_API_KEY"):
        print("Set ANTHROPIC_API_KEY in .env first.")
        sys.exit(1)
    generate_all_scripts()
