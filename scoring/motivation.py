"""
Motivation score engine -- 0-100 composite score.
Each factor returns (points, label) so the UI can explain every point.
"""
from __future__ import annotations
import re
from dataclasses import dataclass, field
from typing import Optional

RATE_MAP = {
    2006:0.0635,2007:0.0634,2008:0.0597,2009:0.0503,2010:0.0470,
    2011:0.0445,2012:0.0370,2013:0.0398,2014:0.0425,2015:0.0385,
    2016:0.0365,2017:0.0399,2018:0.0460,2019:0.0394,2020:0.0310,
    2021:0.0295,2022:0.0506,2023:0.0694,2024:0.0676,2025:0.0665,
}
VALUE_MULT = 1.08
LTV        = 0.80


@dataclass
class PropertyInput:
    address:         str
    owner_name:      str       = ""
    emv:             Optional[float] = None
    prior_sale_price:Optional[float] = None
    prior_sale_year: Optional[int]   = None
    years_owned:     Optional[float] = None
    homestead:       str = "Homestead"
    owner_type:      str = "Owner-Occupied"
    mcro_text:       str = ""
    notes_text:      str = ""
    flags_text:      str = ""
    likelihood:      str = ""
    year_built:      Optional[int]   = None


@dataclass
class ScoreResult:
    total:          int
    tier:           str
    factors:        dict = field(default_factory=dict)
    primary_signal: str  = ""
    est_value:      Optional[float] = None
    equity_usd:     Optional[float] = None
    equity_pct:     Optional[float] = None
    monthly_piti:   Optional[float] = None


def _rem_balance(principal, rate, years_paid):
    r = rate / 12; n = 360; k = min(int(years_paid * 12), n - 1)
    if r == 0: return principal * (1 - k / n)
    return principal * ((1+r)**n - (1+r)**k) / ((1+r)**n - 1)

def _mo_pi(principal, rate):
    r = rate / 12; n = 360
    if r == 0: return principal / n
    return principal * r * (1+r)**n / ((1+r)**n - 1)


def score(p: PropertyInput) -> ScoreResult:
    flags  = (p.flags_text + " " + p.notes_text + " " + p.mcro_text).lower()
    otype  = p.owner_type.lower()
    like   = p.likelihood.lower()

    # ── Skip cases ────────────────────────────────────────────────────────────
    is_listed  = "listed" in like or "active listing" in flags
    is_new     = p.years_owned is not None and p.years_owned <= 1.5

    if is_listed or is_new:
        signal = "Active MLS listing -- not an off-market target" if is_listed else \
                 "Recently purchased (<2 yrs) -- too early"
        return ScoreResult(total=5, tier="SKIP", factors={"skip": 5}, primary_signal=signal)

    # ── Equity calculation ────────────────────────────────────────────────────
    est_value   = p.emv * VALUE_MULT if p.emv else None
    equity_usd  = None
    equity_pct  = None
    monthly_piti = None

    purchase_year = p.prior_sale_year
    if not purchase_year and p.years_owned:
        purchase_year = int(2026 - p.years_owned)
    years_paid = (2026 - purchase_year) if purchase_year else (p.years_owned or 0)

    if p.prior_sale_price and purchase_year and est_value:
        rate      = RATE_MAP.get(purchase_year, 0.045)
        principal = p.prior_sale_price * LTV
        bal       = _rem_balance(principal, rate, years_paid)
        equity_usd = est_value - bal
        equity_pct = equity_usd / est_value if est_value else None

        pi  = _mo_pi(principal, rate)
        emv = p.emv or est_value
        tax_r = 0.018 if "no homestead" in otype or "investor" in otype else 0.012
        monthly_piti = pi + emv * tax_r / 12 + p.prior_sale_price * 0.005 / 12
    # ── Data quality guards (run BEFORE factor scoring) ───────────────────────

    # Guard 1: Bad county sale price data ($500 or $3.465M = data error)
    if p.prior_sale_price and (p.prior_sale_price < 10_000 or p.prior_sale_price > 3_000_000):
        purchase_year = None; years_paid = p.years_owned or 0
        equity_usd = None; equity_pct = None; monthly_piti = None

    # Guard 2: New construction EMV lag
    # EMV < 40% of sale price + purchase 2022+ = county assessed lot only, not building
    # Suppresses false negative_equity signal on new builds
    if (p.prior_sale_price and p.emv and p.emv > 0 and p.prior_sale_price > 0 and
            p.emv / p.prior_sale_price < 0.40 and
            purchase_year is not None and purchase_year >= 2022):
        equity_usd = None; equity_pct = None   # no EMV-based signals for new builds

    # Guard 3: Vacant lot / pre-construction filter
    if est_value and est_value < 200_000 and not p.prior_sale_price:
        if not p.years_owned or p.years_owned <= 3:
            return ScoreResult(total=0, tier="T3",
                               primary_signal="New construction / vacant lot -- builder listing",
                               est_value=est_value)

    # ── Scoring ───────────────────────────────────────────────────────────────
    factors: dict[str, int] = {}

    # Divorce signals
    div_confirmed = ("divorce on record" in flags or "⚠ divorce" in flags or
                     ("dissolution" in flags and "pre-dates" not in flags
                      and "no divorce" not in flags and "possible" not in flags))
    div_possible  = "possible divorce" in flags
    div_prior     = "pre-dates" in flags and "divorce" in flags

    if div_confirmed:   factors["divorce_confirmed"]   = 40
    elif div_possible:  factors["divorce_possible"]    = 20
    elif div_prior:     factors["divorce_prior"]       = 5

    # Homestead / investor
    if "investor" in otype or "investor" in flags:
        factors["investor_llc"] = 30
    elif "no homestead" in otype or "no homestead" in flags:
        factors["no_homestead"] = 20

    # Elderly owner
    if "age 79" in flags or "1946" in flags:
        factors["owner_elderly"] = 15

    # Trust
    if "trust" in otype:
        factors["trust_owned"] = 8

    # Peak buyer (2020-22 purchases at rate peak — high monthly carry, flat equity)
    # Raised to 20 pts: data shows peak buyers sell even without other signals
    if purchase_year and 2020 <= purchase_year <= 2022:
        factors["peak_buyer_2020_22"] = 20

    # Rapid resale (bought < 3 yrs ago but already selling = life disruption or distress)
    if p.years_owned and p.years_owned <= 3 and p.prior_sale_price:
        # Only score if there's an absentee/no-homestead flag (avoids scoring new buyers)
        if "no homestead" in otype or "no homestead" in flags:
            factors["rapid_resale_absentee"] = 12

    # Equity position
    if equity_pct is not None:
        if equity_pct < 0:
            factors["negative_equity"] = 20
        elif equity_pct < 0.10:
            factors["thin_equity"]     = 10
        elif equity_pct > 0.45 and p.years_owned and p.years_owned >= 12:
            factors["equity_rich_long_hold"] = 8

    # Appreciation gain signal: big realized gain = strong cash-out motivation
    # Even without distress, someone who gained 60%+ in 15+ years often sells to
    # capture the gain (downsizing, retirement, kids left home)
    if p.prior_sale_price and est_value and p.years_owned:
        appreciation_pct = (est_value - p.prior_sale_price) / p.prior_sale_price
        if appreciation_pct >= 0.60 and p.years_owned >= 15:
            factors["large_appreciation_15yr"] = 10   # 60%+ gain in 15+ yrs
        elif appreciation_pct >= 0.40 and p.years_owned >= 18:
            factors["large_appreciation_18yr"] = 8    # 40%+ gain in 18+ yrs (slower market)

    # ── Data quality guards ───────────────────────────────────────────────────

    # Guard 1: Suspiciously low or high prior sale price = county data error
    if p.prior_sale_price and (p.prior_sale_price < 10_000 or p.prior_sale_price > 3_000_000):
        purchase_year = None
        years_paid    = p.years_owned or 0
        equity_usd    = None
        equity_pct    = None
        monthly_piti  = None

    # Guard 2: New construction EMV assessment lag
    # EMV < 40% of purchase price + recent sale (2022+) = county only assessed the lot,
    # not the finished building. Negative equity signal is a false positive here.
    # The no_homestead and peak_buyer signals are still valid.
    emv_lag = (
        p.prior_sale_price and p.emv and
        p.emv > 0 and p.prior_sale_price > 0 and
        p.emv / p.prior_sale_price < 0.40 and
        purchase_year is not None and purchase_year >= 2022
    )
    if emv_lag:
        equity_usd  = None    # disable -- data unreliable for new builds
        equity_pct  = None

    # Guard 3: Vacant lot / pre-construction (very low EMV, no sale history, short hold)
    if est_value and est_value < 200_000 and not p.prior_sale_price:
        if not p.years_owned or p.years_owned <= 3:
            return ScoreResult(total=0, tier="T3",
                               primary_signal="New construction / vacant lot -- builder listing",
                               est_value=est_value)

    # Hold duration (expanded — 10yr threshold added based on pressure test data)
    if p.years_owned:
        if   p.years_owned >= 15: factors["long_hold_15plus"] = 8
        elif p.years_owned >= 12: factors["long_hold_12plus"] = 5
        elif p.years_owned >= 10: factors["long_hold_10plus"] = 3

    # Civil litigation
    if "civil" in flags and "no divorce" not in flags:
        factors["civil_litigation"] = 4

    total = min(sum(factors.values()), 100)

    # Tier
    if   total >= 40: tier = "T1"
    elif total >= 20: tier = "T2"
    elif total >= 5:  tier = "T3"
    else:             tier = "T3"

    # Primary signal
    parts = []
    if "divorce_confirmed"    in factors: parts.append("Post-purchase DIVORCE on record")
    if "divorce_possible"     in factors: parts.append("Possible divorce -- verify before visiting")
    if "investor_llc"         in factors: parts.append("Investor LLC -- profit-driven")
    if "no_homestead"         in factors: parts.append("No homestead -- absentee/moved")
    if "owner_elderly"        in factors: parts.append("Owner age 79 -- estate/care signal")
    if "peak_buyer_2020_22"   in factors: parts.append(f"Peak buyer {purchase_year} -- high-rate carry")
    if "negative_equity"      in factors: parts.append("Est. negative equity -- underwater")
    if "thin_equity"          in factors: parts.append("Thin equity (<10%)")
    if "equity_rich_long_hold"     in factors: parts.append(f"{int(p.years_owned)}-yr hold -- equity-rich")
    if "large_appreciation_15yr"   in factors: parts.append(f"60%+ appreciation in {int(p.years_owned)} yrs -- cash-out candidate")
    if "large_appreciation_18yr"   in factors: parts.append(f"40%+ appreciation in {int(p.years_owned)} yrs -- cash-out candidate")
    if "long_hold_10plus"          in factors: parts.append(f"{int(p.years_owned)}-yr hold -- approaching equity stage")
    if "long_hold_15plus"          in factors: parts.append(f"{int(p.years_owned)}-yr hold -- cold knock")
    if "rapid_resale_absentee"     in factors: parts.append("Absentee selling within 3 yrs -- rapid resale signal")
    if "trust_owned"               in factors: parts.append("Trust-owned -- estate vehicle")
    if not parts:
        yr = f"{int(p.years_owned)}-yr hold" if p.years_owned else "hold unknown"
        parts.append(f"No strong signals -- cold knock ({yr})")

    return ScoreResult(
        total=total, tier=tier, factors=factors,
        primary_signal=" | ".join(parts),
        est_value=est_value, equity_usd=equity_usd,
        equity_pct=equity_pct, monthly_piti=monthly_piti,
    )


TIER_COLOR = {"T1": "#C00000", "T2": "#D6A800", "T3": "#375623", "SKIP": "#888888", "TBD": "#AAAAAA"}
TIER_LABEL = {"T1": "🔴 T1 -- KNOCK", "T2": "🟡 T2 -- KNOCK", "T3": "🟢 T3", "SKIP": "⛔ SKIP", "TBD": "❓ TBD"}
