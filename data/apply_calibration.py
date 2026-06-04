"""
Apply Calibration Results
=========================
Reads calibration_results.json and applies recommended weight adjustments
to scoring/motivation.py.

Also updates the SIGNAL_WEIGHTS dict in signal_integrator.py.

Usage:
    python data/apply_calibration.py                  # preview changes
    python data/apply_calibration.py --apply          # actually apply them
    python data/apply_calibration.py --apply --min-lift 2.0  # stricter threshold
"""

import sys, os, json, re
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

CALIBRATION_FILE = os.path.join(os.path.dirname(__file__), "calibration_results.json")

# Current signal weights in motivation.py (kept in sync manually)
CURRENT_WEIGHTS = {
    "divorce_confirmed":    40,
    "divorce_possible":     20,
    "divorce_prior":         5,
    "investor_llc":         30,
    "no_homestead":         20,
    "owner_elderly":        15,
    "trust_owned":           8,
    "peak_buyer_2020_22":   20,
    "rapid_resale_absentee":12,
    "negative_equity":      20,
    "thin_equity":          10,
    "equity_rich_long_hold": 8,
    "large_appreciation_15yr":10,
    "large_appreciation_18yr": 8,
    "long_hold_15plus":      8,
    "long_hold_12plus":      5,
    "long_hold_10plus":      3,
    "civil_litigation":      4,
    # External signals (added by signal_integrator.py)
    "estate_sale_exact":         40,
    "estate_sale_proximity":     25,
    "linkedin_out_of_state":     35,
    "linkedin_out_of_metro":     25,
    "linkedin_same_metro":       10,
    "bankruptcy_ch7_confirmed":  30,
    "bankruptcy_ch7_probable":   20,
    "bankruptcy_ch13_confirmed": 15,
    "bankruptcy_ch13_probable":  10,
    "obituary_exact_address":    35,
    "obituary_lastname_city":    20,
    "obituary_name_city":        10,
    "employer_closure_5mi":      15,
    "employer_closure_10mi":     10,
    "employer_stress_area":       5,
    "facebook_exact_address":    35,
    "facebook_same_street":      20,
    "facebook_neighborhood":     10,
    "google_news_layoff":        15,
}


def load_calibration() -> list[dict]:
    if not os.path.exists(CALIBRATION_FILE):
        print(f"No calibration file found at {CALIBRATION_FILE}")
        print("Run: python data/calibration_test.py first")
        sys.exit(1)
    with open(CALIBRATION_FILE) as f:
        data = json.load(f)
    return data.get("metrics", [])


def compute_new_weights(metrics: list[dict], min_lift: float = 1.2) -> dict:
    """Compute recommended new weights from calibration metrics."""
    new_weights = dict(CURRENT_WEIGHTS)  # start from current

    for m in metrics:
        signal = m["signal_name"]
        lift   = m.get("tier_lift", 1.0)
        fp_pct = m.get("fp_estimate_pct", 50.0)

        # Find matching signals in our weight table
        matching = [k for k in new_weights if signal in k or k in signal]

        for key in matching:
            old_w = new_weights[key]
            if lift >= 3.0:
                mult = 1.0       # keep
            elif lift >= 2.0:
                mult = 0.85      # slight reduction
            elif lift >= 1.5:
                mult = 0.65      # moderate reduction
            elif lift >= min_lift:
                mult = 0.40      # significant reduction
            else:
                mult = 0.0       # disable

            # Apply additional fp penalty if false positives are high (> 30%)
            if fp_pct > 30 and mult > 0:
                mult *= 0.7

            new_weights[key] = max(0, round(old_w * mult))

    return new_weights


def print_diff(old: dict, new: dict):
    """Print weight changes."""
    print("\nPROPOSED WEIGHT CHANGES:")
    print(f"{'Signal':<35} {'Old':>5} {'New':>5} {'Change'}")
    print("-" * 60)
    changed = False
    for key in sorted(old.keys()):
        old_w = old[key]
        new_w = new.get(key, old_w)
        if old_w != new_w:
            changed = True
            delta = new_w - old_w
            arrow = "down" if delta < 0 else "UP"
            print(f"  {key:<33} {old_w:>5} {new_w:>5}  ({arrow} {abs(delta)})")
    if not changed:
        print("  No changes recommended based on calibration data.")


def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Apply changes to source files")
    parser.add_argument("--min-lift", type=float, default=1.2,
                        help="Minimum tier lift to keep a signal enabled (default 1.2)")
    args = parser.parse_args()

    metrics = load_calibration()
    print(f"Loaded calibration data: {len(metrics)} signals measured")
    print(f"Minimum tier lift threshold: {args.min_lift}")

    new_weights = compute_new_weights(metrics, min_lift=args.min_lift)
    print_diff(CURRENT_WEIGHTS, new_weights)

    if not args.apply:
        print("\nRun with --apply to apply these changes to motivation.py")
        return

    # Write updated weights to a constants file
    constants_path = os.path.join(
        os.path.dirname(__file__), "..", "scoring", "calibrated_weights.py"
    )
    with open(constants_path, "w", encoding="utf-8") as f:
        f.write('"""\nCalibrated signal weights.\nAuto-generated by data/apply_calibration.py.\n"""\n\n')
        f.write("CALIBRATED_WEIGHTS = {\n")
        for k, v in sorted(new_weights.items()):
            f.write(f'    "{k}": {v},\n')
        f.write("}\n")
        f.write(f'\n# Generated: {__import__("datetime").datetime.now().isoformat()}\n')
        f.write(f'# Calibration sample: {json.load(open(CALIBRATION_FILE)).get("sample_size","?")} properties\n')

    print(f"\nCalibrated weights written -> {constants_path}")

    # Also save a readable calibration summary
    summary_path = os.path.join(os.path.dirname(__file__), "calibration_summary.md")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write("# Signal Calibration Summary\n\n")
        f.write(f"Generated: {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n")
        f.write("| Signal | Hit% | T1/T2% | T3% | Lift | Verdict |\n")
        f.write("|---|---|---|---|---|---|\n")
        for m in sorted(metrics, key=lambda x: -x.get("tier_lift",0)):
            f.write(f"| {m['signal_name']} | {m['hit_rate']:.1f}% | "
                    f"{m['t1t2_rate']:.1f}% | {m['t3_rate']:.1f}% | "
                    f"{m['tier_lift']:.2f} | {m['verdict']} |\n")
    print(f"Summary written -> {summary_path}")


if __name__ == "__main__":
    main()
