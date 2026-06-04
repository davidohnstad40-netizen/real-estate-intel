"""
Signal Integrator
=================
Unified API that runs all available signal agents for a property and returns
a consolidated signal score with full explainability.

Each signal has:
  - A confidence score (0.0-1.0)
  - A point value (applied only if confidence >= threshold)
  - A reason string (human-readable explanation)

Signal hierarchy:
  Definitive (90%+ accurate, high weight):
    estate_sale_exact      +40 pts  -- estate sale at property address
    linkedin_out_of_state  +35 pts  -- owner moved to new state for work
    bankruptcy_ch7_conf    +30 pts  -- confirmed Chapter 7 liquidation
    obituary_exact         +35 pts  -- deceased owner, exact address

  Strong (70-90% accurate):
    linkedin_out_of_metro  +25 pts  -- job change to different metro
    bankruptcy_ch13_conf   +15 pts  -- Chapter 13 reorganization
    obituary_last_name     +20 pts  -- last name + city obit match
    estate_sale_proximity  +25 pts  -- estate sale within 500ft
    employer_closure_near  +15 pts  -- verified employer closure within 5mi

  Moderate (50-70% accurate):
    facebook_exact_address +35 pts  -- FB marketplace exact address match
    facebook_neighborhood  +15 pts  -- FB marketplace neighborhood match
    employer_closure_area  +10 pts  -- employer closure within 10mi

  Supporting (20-50% accurate, only count if other signals present):
    news_economic_stress   +5 pts   -- general economic news in area
    linkedin_same_metro    +10 pts  -- job change but staying in metro
"""

import sys, os, json
from typing import Optional
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db

SIGNAL_TABLE_DDL = """
CREATE TABLE IF NOT EXISTS property_signals_v2 (
    property_id    VARCHAR,
    signal_name    VARCHAR,
    signal_pts     INTEGER,
    confidence     DOUBLE,
    reason         TEXT,
    evidence       TEXT,
    source         VARCHAR,
    detected_at    TIMESTAMP DEFAULT current_timestamp,
    PRIMARY KEY (property_id, signal_name)
)
"""

# Minimum confidence threshold for each signal category to count toward score
CONFIDENCE_THRESHOLDS = {
    "estate_sale":     0.9,   # must be very specific
    "linkedin":        0.7,   # name + job change pattern
    "bankruptcy":      0.8,   # name + state + recent
    "obituary":        0.7,   # name + city confirmed
    "employer_news":   0.6,   # article quality
    "facebook":        0.8,   # address match required
    "social_signal":   0.7,   # specific moving language
}

# Signal weight table (applied when confidence >= threshold)
SIGNAL_WEIGHTS = {
    # Estate sales
    "estate_sale_exact":        40,
    "estate_sale_proximity":    25,
    # LinkedIn
    "linkedin_out_of_state":    35,
    "linkedin_out_of_metro":    25,
    "linkedin_same_metro":      10,
    # Bankruptcy
    "bankruptcy_ch7_confirmed": 30,
    "bankruptcy_ch7_probable":  20,
    "bankruptcy_ch13_confirmed":15,
    "bankruptcy_ch13_probable": 10,
    # Obituary
    "obituary_exact_address":   35,
    "obituary_lastname_city":   20,
    "obituary_name_city":       10,
    # Employer news
    "employer_closure_5mi":     15,
    "employer_closure_10mi":    10,
    "employer_stress_area":      5,
    # Facebook Marketplace
    "facebook_exact_address":   35,
    "facebook_same_street":     20,
    "facebook_neighborhood":    10,
    # Social signals
    "social_moving_out_of_state": 25,
    "social_moving_out_of_metro": 15,
    "social_life_change":         10,
}


def save_signal(con, property_id: str, signal_name: str, signal_pts: int,
                confidence: float, reason: str, evidence: str, source: str):
    """Upsert one signal into the signals table."""
    con.execute(SIGNAL_TABLE_DDL)
    con.execute("""
        INSERT OR REPLACE INTO property_signals_v2
        (property_id, signal_name, signal_pts, confidence, reason, evidence, source)
        VALUES (?,?,?,?,?,?,?)
    """, [property_id, signal_name, signal_pts, confidence,
          reason, evidence[:500] if evidence else "", source])


def get_all_signals(property_id: str, db_path: str = None) -> list[dict]:
    """Load all stored signals for a property."""
    con = get_db(db_path, read_only=True)
    con.execute(SIGNAL_TABLE_DDL)
    rows = con.execute("""
        SELECT signal_name, signal_pts, confidence, reason, evidence, source, detected_at
        FROM property_signals_v2 WHERE property_id = ?
        ORDER BY signal_pts DESC
    """, [property_id]).df()
    con.close()
    return rows.to_dict("records")


def compute_signal_score(property_id: str, db_path: str = None) -> tuple[int, str]:
    """
    Compute total signal bonus for a property from all stored signals.
    Returns (bonus_pts, explanation).
    Signals are additive but capped at 100 total (including base score from motivation.py).
    """
    signals = get_all_signals(property_id, db_path)
    if not signals:
        return 0, "No external signals detected"

    total_bonus = 0
    parts = []
    for s in signals:
        pts  = s.get("signal_pts", 0) or 0
        conf = s.get("confidence", 0.0) or 0.0
        name = s.get("signal_name","")

        # Apply confidence threshold
        cat = name.split("_")[0]
        threshold = CONFIDENCE_THRESHOLDS.get(cat, 0.6)
        if conf < threshold:
            continue

        total_bonus += pts
        parts.append(f"+{pts}pts [{name}] ({conf:.0%} confidence) -- {s.get('reason','')[:50]}")

    explanation = " | ".join(parts) if parts else "No qualifying signals"
    return min(total_bonus, 60), explanation  # cap bonus at 60 additional pts


def run_all_available_signals(property_id: str, db_path: str = None,
                               skip_login_required: bool = True) -> dict:
    """
    Run all signal agents for one property. Returns summary dict.

    skip_login_required: if True, skips LinkedIn and Facebook (need user session).
    Set to False when running interactively with a browser open.
    """
    import asyncio
    from db.schema import get_db as _get_db

    con = _get_db(db_path, read_only=True)
    prop = con.execute("""
        SELECT p.id, p.address, p.owner_name, p.city, p.zip,
               p.lat, p.lng, s.knock_tier, s.motivation_score
        FROM properties p LEFT JOIN property_scores s ON p.id=s.id
        WHERE p.id = ?
    """, [property_id]).fetchone()
    con.close()

    if not prop:
        return {"error": "Property not found"}

    prop_id, address, owner, city, zip_, lat, lng, tier, score = prop
    write_con = _get_db(db_path)

    results = {
        "property_id": prop_id,
        "address": address,
        "signals_run": [],
        "signals_fired": [],
        "total_bonus": 0,
    }

    # 1. Estate Sales (no login required)
    try:
        from agents.estate_sales import scrape_estate_sales, match_against_properties
        sales = asyncio.run(scrape_estate_sales(None, city or "Blaine", "MN", zip_ or "55449"))
        matches = asyncio.run(match_against_properties(sales, db_path))
        prop_matches = [m for m in matches if m.get("nearby_prop_id") == prop_id]
        for m in prop_matches:
            signal_name = ("estate_sale_exact" if m["match_type"] == "exact"
                           else "estate_sale_proximity")
            pts = SIGNAL_WEIGHTS.get(signal_name, 0)
            save_signal(write_con, prop_id, signal_name, pts, 0.95 if m["match_type"]=="exact" else 0.75,
                        m.get("match_type",""), json.dumps(m)[:200], "estatesales.net")
            results["signals_fired"].append(signal_name)
        results["signals_run"].append("estate_sales")
    except Exception as e:
        results["signals_run"].append(f"estate_sales:ERROR:{e}")

    # 2. Google News (no login required)
    try:
        from agents.google_news_monitor import check_for_area
        news = check_for_area(city or "Blaine", db_path=db_path)
        for n in news:
            if n.get("confidence", 0) >= 0.6:
                pts = 15 if n.get("confidence", 0) >= 0.8 else 10
                signal_name = "employer_closure_5mi" if pts == 15 else "employer_closure_10mi"
                save_signal(write_con, prop_id, signal_name, pts,
                            n["confidence"], n.get("reason",""), n.get("title",""), "google_news")
                results["signals_fired"].append(signal_name)
        results["signals_run"].append("google_news")
    except Exception as e:
        results["signals_run"].append(f"google_news:ERROR:{e}")

    # 3. Bankruptcy (no login required, uses public site)
    try:
        from agents.bankruptcy import check_owner
        bk = check_owner(prop_id, db_path=db_path)
        if bk and bk.get("signal_pts", 0) > 0:
            write_con.execute("SELECT 1")  # ensure connection alive
            results["signals_fired"].append(f"bankruptcy_{bk.get('chapter','?')}")
        results["signals_run"].append("bankruptcy")
    except Exception as e:
        results["signals_run"].append(f"bankruptcy:ERROR:{e}")

    # 4. Obituary (no login required, headless)
    try:
        from agents.obituaries import scan
        import asyncio
        obits = scan(city=city or "Blaine", years_back=2, db_path=db_path)
        prop_obits = [o for o in obits if o.get("property_id") == prop_id]
        for o in prop_obits:
            signal_name = f"obituary_{o.get('match_type','lastname')}"
            pts = SIGNAL_WEIGHTS.get(signal_name, 15)
            save_signal(write_con, prop_id, signal_name, pts,
                        0.9 if o["match_type"]=="exact_address" else 0.7,
                        o.get("match_type",""), o.get("raw_snippet","")[:200], "legacy.com")
            results["signals_fired"].append(signal_name)
        results["signals_run"].append("obituaries")
    except Exception as e:
        results["signals_run"].append(f"obituaries:ERROR:{e}")

    # 5. LinkedIn (requires login -- skip if skip_login_required)
    if not skip_login_required:
        try:
            from agents.linkedin_jobs import check_owners
            li = check_owners(property_ids=[prop_id], db_path=db_path)
            for match in li:
                signal_name = f"linkedin_{match.get('signal_reason','').split()[0].lower()}"
                save_signal(write_con, prop_id, signal_name,
                            match.get("signal_pts",0), 0.8,
                            match.get("signal_reason",""), match.get("raw_snippet","")[:200],
                            "linkedin.com")
                results["signals_fired"].append(signal_name)
            results["signals_run"].append("linkedin")
        except Exception as e:
            results["signals_run"].append(f"linkedin:ERROR:{e}")

    # 6. Facebook Marketplace (requires login -- skip if login_required)
    if not skip_login_required:
        try:
            from agents.facebook_marketplace import run_scan
            fb = run_scan(city=city or "Blaine", db_path=db_path)
            prop_fb = [f for f in fb if f.get("matched_property_id") == prop_id]
            for match in prop_fb:
                signal_name = ("facebook_exact_address" if match["confidence"] >= 0.9
                               else "facebook_same_street")
                save_signal(write_con, prop_id, signal_name,
                            match.get("signal_pts",0), match["confidence"],
                            f"Listed ${match.get('price',0):,.0f} on FB Marketplace",
                            match.get("description","")[:200], "facebook.com")
                results["signals_fired"].append(signal_name)
            results["signals_run"].append("facebook_marketplace")
        except Exception as e:
            results["signals_run"].append(f"facebook:ERROR:{e}")

    write_con.close()

    bonus, explanation = compute_signal_score(prop_id, db_path)
    results["total_bonus"] = bonus
    results["explanation"] = explanation
    return results


if __name__ == "__main__":
    import sys
    prop_id = sys.argv[1] if len(sys.argv) > 1 else None
    if not prop_id:
        print("Usage: python -m agents.signal_integrator <property_id>")
        print("Example: python -m agents.signal_integrator 3316_117TH_LN_NE")
        sys.exit(1)

    result = run_all_available_signals(prop_id)
    print(f"\nProperty: {result.get('address','?')}")
    print(f"Signals run:   {result.get('signals_run')}")
    print(f"Signals fired: {result.get('signals_fired')}")
    print(f"Bonus points:  +{result.get('total_bonus',0)}")
    print(f"Explanation:   {result.get('explanation','')}")
