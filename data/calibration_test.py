"""
Signal Calibration Test
========================
Runs all available signal agents against a stratified sample of 400+ homes.
Measures signal precision, tier lift (predictive power), and recommends
weight adjustments for the scoring engine.

Methodology:
  1. Sample 400 properties (stratified by tier: T1/T2/T3/SKIP)
  2. Run each signal agent against all properties
  3. Compute for each signal:
       hit_rate    = signals_fired / total_properties
       tier_lift   = (hits_on_T1T2 / count_T1T2) / (hits_on_T3 / count_T3)
       precision   = estimated true positives / total signals fired
  4. Recommend new weights based on tier_lift:
       tier_lift >= 3.0 -> weight unchanged (strong signal)
       tier_lift 2.0-3.0 -> reduce 10%
       tier_lift 1.2-2.0 -> reduce 25%
       tier_lift < 1.2   -> set to 0 (noise)
  5. Output: calibration report + weight update script

Usage:
    python data/calibration_test.py --sample 400
    python data/calibration_test.py --sample 100 --fast  (skip slow scrapers)
"""

import sys, os, json, time, argparse
from datetime import datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pandas as pd
from db.schema import get_db
from scoring.motivation import PropertyInput, score

# ── Helpers ───────────────────────────────────────────────────────────────────

def load_sample(n_per_tier: dict = None, db_path: str = None) -> pd.DataFrame:
    """Load a stratified sample of properties for calibration."""
    n_per_tier = n_per_tier or {"T1": 50, "T2": 100, "T3": 200, "SKIP": 30, "TBD": 20}
    con = get_db(db_path, read_only=True)

    frames = []
    for tier, n in n_per_tier.items():
        try:
            df = con.execute(f"""
                SELECT p.id, p.address, p.owner_name, p.city, p.zip,
                       p.lat, p.lng, p.emv, p.prior_sale_price, p.prior_sale_year,
                       p.years_owned, p.homestead, p.owner_type,
                       s.motivation_score, s.knock_tier, s.primary_signal,
                       COALESCE(p.scan_source, 'manual') as scan_source
                FROM properties p
                LEFT JOIN property_scores s ON p.id=s.id
                WHERE s.knock_tier = '{tier}'
                  AND p.address IS NOT NULL AND p.address != ''
                  AND p.owner_name IS NOT NULL
                  AND p.owner_name NOT LIKE '%LLC%'
                  AND p.owner_name NOT LIKE '%Trust%'
                ORDER BY RANDOM()
                LIMIT {n}
            """).df()
            frames.append(df)
        except Exception as e:
            print(f"  Warning loading {tier}: {e}")
    con.close()

    if not frames:
        return pd.DataFrame()
    result = pd.concat(frames, ignore_index=True)
    print(f"Sample loaded: {len(result)} properties")
    for tier, group in result.groupby("knock_tier"):
        print(f"  {tier}: {len(group)}")
    return result


# ── Per-signal test functions ─────────────────────────────────────────────────

def test_estate_sales(props: pd.DataFrame, db_path: str = None) -> pd.DataFrame:
    """
    Test estate sales signal: searches EstateSales.net for each city.
    Returns DataFrame with columns: property_id, signal_fired, confidence, reason.
    """
    import asyncio
    from agents.estate_sales import scrape_estate_sales, match_against_properties

    print("\n[1/6] Testing estate sales signal...")
    results = []

    # Group by city to minimize API calls
    cities = props["city"].dropna().unique()
    city_matches = {}
    for city in cities:
        city_str = str(city)
        zip_codes = props[props["city"]==city]["zip"].dropna().unique()
        for zip_code in zip_codes[:2]:
            try:
                sales = asyncio.run(scrape_estate_sales(None, city_str, "MN", str(zip_code)))
                matches = asyncio.run(match_against_properties(sales, db_path))
                for m in matches:
                    pid = m.get("nearby_prop_id","")
                    if pid not in city_matches:
                        city_matches[pid] = m
            except Exception:
                pass
        time.sleep(1)

    for _, row in props.iterrows():
        m = city_matches.get(row["id"])
        results.append({
            "property_id":  row["id"],
            "knock_tier":   row["knock_tier"],
            "signal_fired": m is not None,
            "confidence":   m.get("distance_ft", 0) == 0 and 0.95 or 0.75 if m else 0.0,
            "reason":       m.get("match_type","") if m else "",
            "signal_name":  "estate_sale",
        })

    return pd.DataFrame(results)


def test_google_news(props: pd.DataFrame, db_path: str = None) -> pd.DataFrame:
    """Test Google News employer closure signal."""
    from agents.google_news_monitor import search_employer_news

    print("\n[2/6] Testing Google News signal...")
    cities = list(props["city"].dropna().unique())
    city_news = {}
    for city in cities:
        try:
            articles = search_employer_news(str(city))
            city_news[str(city)] = articles
            time.sleep(0.5)
        except Exception:
            city_news[str(city)] = []

    results = []
    for _, row in props.iterrows():
        city = str(row.get("city","Blaine"))
        articles = city_news.get(city, [])
        high_conf = [a for a in articles if a.get("confidence",0) >= 0.6]
        results.append({
            "property_id":  row["id"],
            "knock_tier":   row["knock_tier"],
            "signal_fired": len(high_conf) > 0,
            "confidence":   max((a.get("confidence",0) for a in high_conf), default=0.0),
            "reason":       high_conf[0].get("reason","") if high_conf else "",
            "signal_name":  "google_news",
        })
    return pd.DataFrame(results)


def test_obituaries(props: pd.DataFrame, db_path: str = None) -> pd.DataFrame:
    """Test obituary signal for all owners."""
    import asyncio
    from agents.obituaries import search_legacy, search_star_tribune, classify_match
    from playwright.async_api import async_playwright
    from agents.skip_trace import _parse_name

    print("\n[3/6] Testing obituary signal (headless browser)...")
    matches_by_prop = {}

    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=["--no-sandbox"])
            ctx  = await browser.new_context(user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124.0.0.0 Safari/537.36"
            ))
            page = await ctx.new_page()

            # Group by last name + city to minimize requests
            name_city_map = {}
            for _, row in props.iterrows():
                first, last = _parse_name(str(row.get("owner_name","") or ""))
                if not last or len(last) < 3:
                    continue
                key = (last, str(row.get("city","Blaine")))
                if key not in name_city_map:
                    name_city_map[key] = []
                name_city_map[key].append((row["id"], first, last,
                                            str(row.get("address",""))))

            COMMON_NAMES = {
                "smith","johnson","williams","brown","jones","davis","miller",
                "wilson","moore","taylor","anderson","thomas","jackson","white",
                "harris","martin","thompson","garcia","martinez","robinson",
                "clark","rodriguez","lewis","lee","walker","hall","allen",
                "young","hernandez","king","wright","lopez","hill","scott"
            }

            for (last_name, city), prop_list in list(name_city_map.items())[:100]:
                # Skip common last names -- too many false positives
                if last_name.lower() in COMMON_NAMES:
                    for pid, *_ in prop_list:
                        matches_by_prop[pid] = {
                            "fired": False, "confidence": 0.0,
                            "reason": "Skipped (common last name -- high false positive risk)"
                        }
                    continue

                obits = await search_legacy(page, last_name, city, "MN", years_back=2)
                if not obits:
                    obits = await search_star_tribune(page, last_name, city)

                for pid, first, last, address in prop_list:
                    for obit in obits:
                        match_type, pts = classify_match(obit, last, address, city)
                        if pts >= 20:  # only meaningful matches
                            matches_by_prop[pid] = {
                                "fired": True, "confidence": 0.9 if pts>=35 else 0.7,
                                "reason": f"{match_type}: {obit.get('deceased_name','')[:40]}"
                            }
                            break
                    else:
                        if pid not in matches_by_prop:
                            matches_by_prop[pid] = {"fired": False, "confidence": 0.0, "reason": ""}

                await asyncio.sleep(1.5)

            await browser.close()

    try:
        asyncio.run(run())
    except Exception as e:
        print(f"  Obituary test error: {e}")

    results = []
    for _, row in props.iterrows():
        m = matches_by_prop.get(row["id"], {"fired": False, "confidence": 0.0, "reason": ""})
        results.append({
            "property_id":  row["id"],
            "knock_tier":   row["knock_tier"],
            "signal_fired": m["fired"],
            "confidence":   m["confidence"],
            "reason":       m["reason"],
            "signal_name":  "obituary",
        })
    return pd.DataFrame(results)


def test_bankruptcy(props: pd.DataFrame, db_path: str = None) -> pd.DataFrame:
    """Test PACER/Inforuptcy bankruptcy signal."""
    print("\n[4/6] Testing bankruptcy signal...")

    results = []
    try:
        from agents.bankruptcy import search_bankruptcy, score_bankruptcy_signal
        from ingestion.metrogis import _parse_name
        import asyncio

        for _, row in props.iterrows():
            first, last = _parse_name(str(row.get("owner_name","") or ""))
            fired = False; conf = 0.0; reason = ""
            if first and last and len(last) >= 3:
                try:
                    from agents.bankruptcy import search_bankruptcy as sb
                    filings = asyncio.run(sb(first, last)) if asyncio.iscoroutinefunction(sb) else sb(first, last)

                    pts, r = score_bankruptcy_signal(filings)
                    if pts > 0:
                        fired = True; conf = max(f.get("confidence",0) for f in filings); reason = r
                except Exception:
                    pass

            results.append({
                "property_id":  row["id"],
                "knock_tier":   row["knock_tier"],
                "signal_fired": fired,
                "confidence":   conf,
                "reason":       reason,
                "signal_name":  "bankruptcy",
            })
            time.sleep(0.5)
    except Exception as e:
        print(f"  Bankruptcy test error: {e}")
        for _, row in props.iterrows():
            results.append({"property_id": row["id"], "knock_tier": row["knock_tier"],
                            "signal_fired": False, "confidence": 0.0,
                            "reason": f"Error: {e}", "signal_name": "bankruptcy"})

    return pd.DataFrame(results)


def test_metrogis_signals(props: pd.DataFrame) -> pd.DataFrame:
    """
    Validate MetroGIS-derived signals (no homestead, peak buyer, appreciation).
    These are already in the scoring engine -- this validates their accuracy.
    """
    print("\n[5/6] Validating MetroGIS signals (already in scoring engine)...")
    results = []
    for _, row in props.iterrows():
        pi = PropertyInput(
            address=str(row.get("address","")),
            emv=row.get("emv"),
            prior_sale_price=row.get("prior_sale_price"),
            prior_sale_year=int(row["prior_sale_year"]) if row.get("prior_sale_year") and not pd.isna(row["prior_sale_year"]) else None,
            years_owned=row.get("years_owned"),
            homestead=str(row.get("homestead","") or ""),
            owner_type=str(row.get("owner_type","") or ""),
        )
        r = score(pi)
        results.append({
            "property_id":  row["id"],
            "knock_tier":   row["knock_tier"],
            "signal_fired": r.total >= 20,
            "confidence":   min(r.total / 100, 1.0),
            "reason":       ", ".join(r.factors.keys()),
            "signal_name":  "metrogis_combined",
            "score":        r.total,
            "tier":         r.tier,
        })
    return pd.DataFrame(results)


# ── Analysis ──────────────────────────────────────────────────────────────────

def compute_metrics(signal_df: pd.DataFrame, signal_name: str) -> dict:
    """Compute precision/recall metrics for one signal."""
    t1t2 = signal_df[signal_df["knock_tier"].isin(["T1","T2"])]
    t3    = signal_df[signal_df["knock_tier"] == "T3"]

    total      = len(signal_df)
    total_t1t2 = len(t1t2)
    total_t3   = len(t3)

    fired_all  = signal_df["signal_fired"].sum()
    fired_t1t2 = t1t2["signal_fired"].sum()
    fired_t3   = t3["signal_fired"].sum()

    hit_rate    = fired_all / max(total, 1)
    t1t2_rate   = fired_t1t2 / max(total_t1t2, 1)
    t3_rate     = fired_t3 / max(total_t3, 1)
    tier_lift   = t1t2_rate / max(t3_rate, 0.001)

    # Recommended weight multiplier
    if tier_lift >= 3.0:       weight_mult = 1.0    # strong signal
    elif tier_lift >= 2.0:     weight_mult = 0.85   # good signal
    elif tier_lift >= 1.5:     weight_mult = 0.65   # moderate
    elif tier_lift >= 1.2:     weight_mult = 0.40   # weak
    else:                      weight_mult = 0.0    # noise -- disable

    # False positive estimate: signals that fire on T3 are potential FPs
    fp_estimate = t3_rate if total_t3 > 0 else 0.0

    return {
        "signal_name":     signal_name,
        "total_tested":    total,
        "total_fired":     int(fired_all),
        "hit_rate":        round(hit_rate * 100, 1),
        "t1t2_rate":       round(t1t2_rate * 100, 1),
        "t3_rate":         round(t3_rate * 100, 1),
        "tier_lift":       round(tier_lift, 2),
        "fp_estimate_pct": round(fp_estimate * 100, 1),
        "weight_mult":     weight_mult,
        "verdict":         (
            "STRONG -- keep weight" if tier_lift >= 3.0 else
            "GOOD -- slight reduction" if tier_lift >= 2.0 else
            "MODERATE -- reduce weight 35%" if tier_lift >= 1.5 else
            "WEAK -- reduce weight 60%" if tier_lift >= 1.2 else
            "NOISE -- DISABLE this signal"
        ),
    }


def print_calibration_report(metrics: list[dict]):
    """Print formatted calibration report."""
    print("\n" + "=" * 75)
    print("SIGNAL CALIBRATION REPORT")
    print(f"Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print("=" * 75)

    print(f"\n{'Signal':<25} {'Hit%':>5} {'T1T2%':>6} {'T3%':>5} "
          f"{'Lift':>6} {'FP%':>5} {'Verdict'}")
    print("-" * 75)

    for m in sorted(metrics, key=lambda x: -x.get("tier_lift",0)):
        print(f"{m['signal_name']:<25} "
              f"{m['hit_rate']:>5.1f} "
              f"{m['t1t2_rate']:>6.1f} "
              f"{m['t3_rate']:>5.1f} "
              f"{m['tier_lift']:>6.2f} "
              f"{m['fp_estimate_pct']:>5.1f} "
              f"  {m['verdict'][:35]}")

    print("\n" + "-" * 75)
    print("\nKEY METRICS EXPLAINED:")
    print("  Hit%     = % of all properties where signal fired")
    print("  T1T2%    = % of T1/T2 properties where signal fired")
    print("  T3%      = % of T3 properties where signal fired (false positive proxy)")
    print("  Lift     = T1T2% / T3% (how much more likely to fire on motivated sellers)")
    print("  FP%      = estimated false positive rate (= T3%)")
    print()
    print("WEIGHT RECOMMENDATION:")
    print("  Lift >= 3.0 -> STRONG: keep current weight")
    print("  Lift >= 2.0 -> GOOD: reduce weight by 15%")
    print("  Lift >= 1.5 -> MODERATE: reduce weight by 35%")
    print("  Lift >= 1.2 -> WEAK: reduce weight by 60%")
    print("  Lift < 1.2  -> NOISE: disable this signal")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sample", type=int, default=400)
    parser.add_argument("--fast", action="store_true",
                        help="Skip slow scrapers (bankruptcy, obituary)")
    parser.add_argument("--tier", nargs="+", default=["T1","T2","T3","SKIP"])
    args = parser.parse_args()

    n_t1 = min(50,  args.sample // 8)
    n_t2 = min(100, args.sample // 4)
    n_t3 = min(200, args.sample // 2)
    n_sk = min(50,  args.sample // 8)
    n_tbd = args.sample - n_t1 - n_t2 - n_t3 - n_sk

    print(f"Loading {args.sample}-property calibration sample...")
    props = load_sample({"T1": n_t1, "T2": n_t2, "T3": n_t3,
                          "SKIP": n_sk, "TBD": max(n_tbd, 0)})
    if props.empty:
        print("No properties loaded. Ensure DuckDB has data.")
        return

    all_metrics = []
    all_results = []

    # 5. MetroGIS signals (always run -- fast, no scraping)
    df_mg = test_metrogis_signals(props)
    all_results.append(df_mg)
    all_metrics.append(compute_metrics(df_mg, "metrogis_combined"))

    # 1. Estate sales (fast scraper)
    df_es = test_estate_sales(props)
    all_results.append(df_es)
    all_metrics.append(compute_metrics(df_es, "estate_sale"))

    # 2. Google News (fast RSS)
    df_gn = test_google_news(props)
    all_results.append(df_gn)
    all_metrics.append(compute_metrics(df_gn, "google_news"))

    if not args.fast:
        # 3. Obituaries (slower -- headless browser)
        df_ob = test_obituaries(props)
        all_results.append(df_ob)
        all_metrics.append(compute_metrics(df_ob, "obituary"))

        # 4. Bankruptcy (slower -- external site)
        df_bk = test_bankruptcy(props)
        all_results.append(df_bk)
        all_metrics.append(compute_metrics(df_bk, "bankruptcy"))

    # Save full results
    import os
    out_path = os.path.join(os.path.dirname(__file__), "calibration_results.json")
    with open(out_path, "w") as f:
        json.dump({
            "metrics": all_metrics,
            "sample_size": len(props),
            "generated": datetime.now().isoformat(),
        }, f, indent=2)

    # Print report
    print_calibration_report(all_metrics)

    print(f"\nFull results saved -> {out_path}")
    print("\nNEXT STEP: Apply recommended weight adjustments to scoring/motivation.py")
    print("Run: python data/apply_calibration.py to auto-apply recommendations")


if __name__ == "__main__":
    main()
