"""
Apply employer disruption signals to property scores.

Conservative approach:
  - Large employer (50+ jobs): +8 pts within 5 miles (neighborhood-level risk)
  - Small employer (< 50 jobs): +4 pts within 3 miles
  - Only actually upgrade tier if property has OTHER signals already (score >= 12)
  - Don't blanket-upgrade all T3s -- that would dilute the T2 list

Signal stored in property_signals_v2 for transparency and explainability.
"""
import sys, os, json, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from db.schema import get_db
from agents.signal_integrator import save_signal

EMPLOYER_EVENTS = [
    {
        "name":           "Aveda/Estee Lauder",
        "lat":            45.1495,
        "lng":           -93.2048,
        "employees":      68,
        "event":          "layoff",
        "radius_mi":      5,
        "signal_name":    "employer_layoff_large",
        "signal_pts":     8,
        "confidence":     0.85,
        "reason":         "Estee Lauder (Aveda) cutting 68 jobs at Blaine facility (2.4mi)",
        "source":         "google_news",
    },
    {
        "name":           "Invictus Brewing Co.",
        "lat":            45.1720,
        "lng":           -93.1970,
        "employees":      15,
        "event":          "closure",
        "radius_mi":      3,
        "signal_name":    "employer_closure_small",
        "signal_pts":     4,
        "confidence":     0.70,
        "reason":         "Invictus Brewing closing in Blaine (0.8mi)",
        "source":         "google_news",
    },
]

def haversine_miles(lat1, lng1, lat2, lng2):
    R = 3959
    dlat = math.radians(lat2 - lat1)
    dlng = math.radians(lng2 - lng1)
    a = (math.sin(dlat/2)**2 +
         math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) *
         math.sin(dlng/2)**2)
    return R * 2 * math.asin(math.sqrt(a))

def main():
    con = get_db()

    # Ensure signal table exists
    con.execute("""
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
    """)

    props = con.execute("""
        SELECT p.id, p.address, p.lat, p.lng,
               s.motivation_score, s.knock_tier, s.score_factors
        FROM properties p
        LEFT JOIN property_scores s ON p.id=s.id
        WHERE p.lat IS NOT NULL AND p.lng IS NOT NULL
          AND (p.scan_source IS NULL OR p.scan_source='manual')
    """).df()

    applied = 0
    upgraded = 0

    for event in EMPLOYER_EVENTS:
        print(f"\nApplying: {event['name']} ({event['employees']} jobs)")
        for _, row in props.iterrows():
            if not row.lat or not row.lng:
                continue
            dist = haversine_miles(event["lat"], event["lng"],
                                   row.lat, row.lng)
            if dist > event["radius_mi"]:
                continue

            # Store in signals table
            save_signal(con,
                property_id = row["id"],
                signal_name = event["signal_name"],
                signal_pts  = event["signal_pts"],
                confidence  = event["confidence"],
                reason      = event["reason"],
                evidence    = f"Distance: {dist:.1f} miles",
                source      = event["source"],
            )

            # Conservative upgrade rule:
            # Only boost score if property already has some signal (score >= 10)
            # This prevents blanket-upgrading clean T3s that just happen to be nearby
            old_score = int(row.get("motivation_score") or 0)
            old_tier  = str(row.get("knock_tier") or "T3")

            if old_score >= 10:  # has some existing signal
                new_score = min(old_score + event["signal_pts"], 100)
                new_tier  = "T1" if new_score >= 40 else "T2" if new_score >= 20 else "T3"

                if new_tier != old_tier:
                    # Update score with bonus
                    factors = json.loads(str(row.get("score_factors") or "{}"))
                    factors[event["signal_name"]] = event["signal_pts"]

                    con.execute("""
                        UPDATE property_scores SET
                            motivation_score = ?,
                            knock_tier       = ?,
                            score_factors    = ?,
                            updated_at       = current_timestamp
                        WHERE id = ?
                    """, [new_score, new_tier, json.dumps(factors), row["id"]])

                    print(f"  UPGRADED: {row['address'].split(',')[0]} "
                          f"{old_tier}({old_score}) -> {new_tier}({new_score})")
                    upgraded += 1

            applied += 1

    con.close()
    print(f"\nDone. Applied {event['signal_name']} to {applied} nearby properties.")
    print(f"Tier upgrades: {upgraded}")
    print("\nConservative rule applied: only boosted properties with score >= 10")
    print("(Prevents blanket T3->T2 upgrades on clean/unsignaled properties)")

if __name__ == "__main__":
    main()
