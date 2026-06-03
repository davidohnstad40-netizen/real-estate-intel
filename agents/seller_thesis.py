"""
AI Seller Thesis Generator
Uses claude-3-5-haiku for fast per-property narratives.
Uses claude-3-5-sonnet for weekly market summaries.
"""
import os, json
import anthropic

_client = None
def _get_client():
    global _client
    if _client is None:
        key = os.getenv("ANTHROPIC_API_KEY")
        if not key:
            raise ValueError("ANTHROPIC_API_KEY not set")
        _client = anthropic.Anthropic(api_key=key)
    return _client


THESIS_PROMPT = """You are a real estate investment analyst specializing in off-market motivated sellers.

Generate a seller thesis for this property based ONLY on the data provided. Be specific and factual.

PROPERTY DATA:
Address: {address}
Owner: {owner_name}
Motivation Score: {motivation_score}/100  (T1=knock first, T2=knock next, T3=cold knock)
Knock Tier: {knock_tier}
Years Owned: {years_owned}
Est. Market Value: {est_value}
Est. Equity: {equity_usd} ({equity_pct})
Est. Monthly PITI: {monthly_piti}
Homestead Status: {homestead}
Score Factors: {score_factors}
Signals: {primary_signal}

Write a 2-3 sentence seller thesis that covers:
1. WHY this owner may be motivated (grounded in the signal data)
2. The financial context (equity position, carrying cost if relevant)
3. The best outreach angle for a door knock or letter

Keep it professional, concise, and based only on the data. Do not speculate beyond what the data supports."""


OFFER_PROMPT = """You are a real estate investment analyst. Based on this property's data, suggest an offer range.

Address: {address}
Est. Market Value: {est_value}
Est. Equity: {equity_usd} ({equity_pct})
Monthly PITI: {monthly_piti}
Motivation Score: {motivation_score}/100
Knock Tier: {knock_tier}
Score Factors: {score_factors}

Provide:
1. Retail value (what it would list for on MLS)
2. Quick-sale value (what a motivated seller might accept off-market, typically 80-90% of retail)
3. Opening offer (aggressive but not insulting, typically 75-85% of retail)
4. Walk-away price (absolute maximum you'd pay)
5. One sentence on negotiation angle

Format as a brief structured list. Base all numbers on the estimated market value provided."""


SUMMARY_PROMPT = """You are a real estate market analyst. Generate a brief intelligence summary for this set of properties.

REGION: {region_name}
PROPERTIES ANALYZED: {total_count}
T1 (highest priority): {t1_count}
T2 (medium priority): {t2_count}
T3 (cold knock): {t3_count}
Average Motivation Score: {avg_score}
Top Signals Found: {top_signals}

Key T1/T2 Properties:
{top_properties}

Write a 3-4 sentence market intelligence summary covering:
1. The overall opportunity in this region
2. The strongest individual targets and why
3. Any patterns or themes across the motivated sellers
4. Recommended outreach order

Be specific and actionable. Avoid generic real estate boilerplate."""


def generate_thesis(prop: dict) -> str:
    """Generate a seller thesis for one property. Returns the thesis text."""
    def fmt(val, prefix="$", suffix=""):
        if val is None: return "Unknown"
        if isinstance(val, float): return f"{prefix}{val:,.0f}{suffix}"
        return str(val)

    factors = prop.get("score_factors") or {}
    if isinstance(factors, str):
        try: factors = json.loads(factors)
        except: factors = {}
    factors_str = ", ".join(f"{k.replace('_',' ')}: +{v}pts" for k,v in factors.items()) or "No strong signals"

    prompt = THESIS_PROMPT.format(
        address       = prop.get("address","Unknown"),
        owner_name    = prop.get("owner_name","Unknown"),
        motivation_score = prop.get("motivation_score", 0),
        knock_tier    = prop.get("knock_tier","TBD"),
        years_owned   = f"{prop['years_owned']:.0f} years" if prop.get("years_owned") else "Unknown",
        est_value     = fmt(prop.get("est_value")),
        equity_usd    = fmt(prop.get("est_equity_usd")),
        equity_pct    = f"{prop['equity_pct']:.0%}" if prop.get("equity_pct") else "Unknown",
        monthly_piti  = fmt(prop.get("monthly_piti"), suffix="/mo"),
        homestead     = prop.get("homestead","Unknown"),
        score_factors = factors_str,
        primary_signal= prop.get("primary_signal","None"),
    )

    resp = _get_client().messages.create(
        model="claude-haiku-4-5",
        max_tokens=300,
        messages=[{"role":"user","content":prompt}],
    )
    return resp.content[0].text.strip()


def generate_offer_model(prop: dict) -> str:
    """Generate an offer range recommendation."""
    def fmt(val, prefix="$"):
        if val is None: return "Unknown"
        return f"{prefix}{val:,.0f}"

    factors = prop.get("score_factors") or {}
    if isinstance(factors, str):
        try: factors = json.loads(factors)
        except: factors = {}
    factors_str = ", ".join(f"{k.replace('_',' ')}: +{v}pts" for k,v in factors.items()) or "No strong signals"

    prompt = OFFER_PROMPT.format(
        address        = prop.get("address","Unknown"),
        est_value      = fmt(prop.get("est_value")),
        equity_usd     = fmt(prop.get("est_equity_usd")),
        equity_pct     = f"{prop['equity_pct']:.0%}" if prop.get("equity_pct") else "Unknown",
        monthly_piti   = fmt(prop.get("monthly_piti"), prefix="$") + "/mo" if prop.get("monthly_piti") else "Unknown",
        motivation_score = prop.get("motivation_score",0),
        knock_tier     = prop.get("knock_tier","TBD"),
        score_factors  = factors_str,
    )

    resp = _get_client().messages.create(
        model="claude-haiku-4-5",
        max_tokens=400,
        messages=[{"role":"user","content":prompt}],
    )
    return resp.content[0].text.strip()


def generate_region_summary(region_name: str, properties: list[dict]) -> str:
    """Generate a market summary for a full region."""
    from collections import Counter
    tiers = Counter(p.get("knock_tier","TBD") for p in properties)
    scores = [p["motivation_score"] for p in properties if p.get("motivation_score")]
    avg_score = sum(scores)/len(scores) if scores else 0

    # Collect top signals
    all_factors: dict[str,int] = {}
    for p in properties:
        f = p.get("score_factors") or {}
        if isinstance(f,str):
            try: f = json.loads(f)
            except: f = {}
        for k,v in f.items():
            all_factors[k] = all_factors.get(k,0) + v
    top_signals = ", ".join(k.replace("_"," ") for k,_ in sorted(all_factors.items(), key=lambda x:-x[1])[:5])

    top_props = "\n".join(
        f"- {p['address']} | Score {p.get('motivation_score',0)} | {p.get('primary_signal','')[:60]}"
        for p in sorted(properties, key=lambda x: -(x.get("motivation_score") or 0))[:5]
    )

    prompt = SUMMARY_PROMPT.format(
        region_name    = region_name,
        total_count    = len(properties),
        t1_count       = tiers.get("T1",0),
        t2_count       = tiers.get("T2",0),
        t3_count       = tiers.get("T3",0),
        avg_score      = f"{avg_score:.0f}",
        top_signals    = top_signals or "None identified",
        top_properties = top_props,
    )

    resp = _get_client().messages.create(
        model="claude-sonnet-4-5",
        max_tokens=500,
        messages=[{"role":"user","content":prompt}],
    )
    return resp.content[0].text.strip()
