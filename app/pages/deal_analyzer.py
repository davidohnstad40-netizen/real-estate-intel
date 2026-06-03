"""
Deal Analyzer -- Investment Return Calculator
Select a property → input purchase price + rehab → see full deal metrics
"""
import os
import sys
import math

import pandas as pd
import streamlit as st

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))  # app/pages -> app -> root
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.schema import get_db

try:
    from ingestion.comp_sales import estimate_value, get_comps
    _COMPS_AVAILABLE = True
except ImportError:
    _COMPS_AVAILABLE = False

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _fmt(value: float, prefix: str = "$") -> str:
    """Format a number as currency string."""
    if value is None:
        return "N/A"
    return f"{prefix}{value:,.0f}"


def _monthly_payment(principal: float, annual_rate_pct: float, years: int) -> float:
    """Standard mortgage P&I monthly payment."""
    r = (annual_rate_pct / 100.0) / 12.0
    n = years * 12
    if r == 0:
        return principal / n
    return principal * (r * (1 + r) ** n) / ((1 + r) ** n - 1)


def _deal_rating(profit_margin_pct: float) -> str:
    """Convert profit margin % to star rating."""
    if profit_margin_pct >= 20:
        return "⭐⭐⭐⭐  Excellent"
    elif profit_margin_pct >= 10:
        return "⭐⭐⭐  Good"
    elif profit_margin_pct >= 5:
        return "⭐⭐  Fair"
    else:
        return "⭐  Weak"


def _load_properties() -> pd.DataFrame:
    """Load properties joined with scores from rei.duckdb."""
    db_path = os.path.join(_ROOT, "data", "rei.duckdb")
    try:
        con = get_db(db_path, read_only=True)
        df = con.execute("""
            SELECT
                p.id,
                p.address,
                p.city,
                p.zip,
                p.lat,
                p.lng,
                p.sqft,
                p.emv,
                p.est_value,
                COALESCE(ps.knock_tier, 'TBD')        AS tier,
                COALESCE(ps.motivation_score, 0)       AS score,
                COALESCE(ps.primary_signal, '')        AS primary_signal,
                COALESCE(ps.est_equity_usd, 0)         AS est_equity_usd
            FROM properties p
            LEFT JOIN property_scores ps ON p.id = ps.id
            ORDER BY ps.motivation_score DESC NULLS LAST
        """).fetchdf()
        con.close()
        return df
    except Exception as e:
        st.warning(f"Could not load properties: {e}")
        return pd.DataFrame()


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

st.title("💹 Deal Analyzer")

# ── Property selector ────────────────────────────────────────────────────────
properties = _load_properties()

if properties.empty:
    st.info("No properties found in the database. Run the ingestion pipeline first.")
    st.stop()

# Build display label: address + tier + score
properties["_label"] = properties.apply(
    lambda r: f"{r['address']}  |  {r['tier']}  |  Score: {r['score']}", axis=1
)
label_to_row = {row["_label"]: row for _, row in properties.iterrows()}

selected_label = st.selectbox(
    "Select a property",
    options=list(label_to_row.keys()),
    index=0,
)

prop = label_to_row[selected_label]

# ── Property info ────────────────────────────────────────────────────────────
st.markdown("---")
col_emv, col_equity, col_signal = st.columns(3)

with col_emv:
    st.metric("Estimated Market Value", _fmt(prop.get("emv") or 0))

with col_equity:
    st.metric("Est. Equity", _fmt(prop.get("est_equity_usd") or 0))

with col_signal:
    signal = prop.get("primary_signal") or "--"
    st.metric("Primary Signal", signal)

st.markdown("---")

# ── Comp-based value estimate ─────────────────────────────────────────────────
prop_lat = prop.get("lat")
prop_lng = prop.get("lng")
prop_sqft = prop.get("sqft") or 0
prop_emv = prop.get("emv") or 0

comp_result = None
if _COMPS_AVAILABLE and prop_lat and prop_lng and prop_sqft:
    db_path_parcels = os.path.join(_ROOT, "data", "parcels.duckdb")
    comp_result = estimate_value(
        lat=prop_lat,
        lng=prop_lng,
        sqft=prop_sqft,
        emv=prop_emv,
        db_path=db_path_parcels,
    )
    _comp_est_value = comp_result["est_value"]
else:
    _comp_est_value = (prop_emv * 1.15) if prop_emv else 0.0

# ── Inputs + Metrics columns ──────────────────────────────────────────────────
left_col, right_col = st.columns([1, 1], gap="large")

# =========================================================
# LEFT -- Deal Inputs
# =========================================================
with left_col:
    st.subheader("Deal Inputs")

    purchase_default = int(_comp_est_value * 0.80) if _comp_est_value else 0
    purchase_price = st.number_input(
        "Purchase Price ($)",
        min_value=0,
        max_value=5_000_000,
        value=purchase_default,
        step=1_000,
        format="%d",
    )

    rehab_budget = st.number_input(
        "Rehab Budget ($)",
        min_value=0,
        max_value=500_000,
        value=25_000,
        step=5_000,
        format="%d",
    )

    closing_pct = st.slider(
        "Closing Costs -- Buy (%)",
        min_value=2.0,
        max_value=4.0,
        value=3.0,
        step=0.25,
        format="%.2f%%",
    )

    financing = st.radio(
        "Financing",
        options=["All Cash", "Leveraged"],
        horizontal=True,
    )

    down_pct = None
    interest_rate = None
    loan_term = None
    monthly_pi = 0.0

    if financing == "Leveraged":
        down_pct = st.slider(
            "Down Payment (%)",
            min_value=20,
            max_value=40,
            value=25,
            step=5,
            format="%d%%",
        )
        interest_rate = st.number_input(
            "Interest Rate (%)",
            min_value=1.0,
            max_value=20.0,
            value=7.0,
            step=0.125,
            format="%.3f",
        )
        loan_term = st.selectbox("Loan Term (years)", options=[15, 30], index=1)

    holding_months = st.slider(
        "Holding Period (months)",
        min_value=1,
        max_value=24,
        value=6,
        step=1,
    )

    holding_cost_mo = st.number_input(
        "Holding Costs / Month ($)",
        min_value=0,
        max_value=20_000,
        value=1_000,
        step=100,
        format="%d",
    )

    arv_override = st.number_input(
        "ARV Override ($)  -- leave 0 to use estimate",
        min_value=0,
        max_value=5_000_000,
        value=0,
        step=1_000,
        format="%d",
    )


# =========================================================
# Calculations
# =========================================================

# ARV
if arv_override and arv_override > 0:
    arv = float(arv_override)
    arv_source = "manual override"
elif comp_result and comp_result["est_value"] > 0:
    arv = comp_result["est_value"]
    arv_source = f"comp-based ({comp_result['comp_count']} comps, {comp_result['method']})"
elif prop_emv and prop_emv > 0:
    arv = prop_emv * 1.15
    arv_source = "EMV × 1.15 (no comp data)"
else:
    arv = 0.0
    arv_source = "unknown"

# Costs
closing_buy = purchase_price * (closing_pct / 100.0)
holding_total = holding_months * holding_cost_mo
total_all_in = purchase_price + rehab_budget + closing_buy + holding_total

# Sale proceeds
closing_sell = arv * 0.06  # standard 6% seller closing / agent costs
net_proceeds = arv - closing_sell

gross_profit = net_proceeds - total_all_in

# Net profit (after selling costs already in net_proceeds)
net_profit = gross_profit  # gross already nets out selling costs

# Leverage
if financing == "Leveraged" and down_pct is not None:
    down_payment = purchase_price * (down_pct / 100.0)
    loan_amount = purchase_price - down_payment
    monthly_pi = _monthly_payment(loan_amount, interest_rate, loan_term)
    cash_deployed = down_payment + rehab_budget + closing_buy + holding_total
    coc_roi = (gross_profit / cash_deployed * 100.0) if cash_deployed > 0 else 0.0
else:
    down_payment = 0.0
    loan_amount = 0.0
    monthly_pi = 0.0
    cash_deployed = total_all_in
    coc_roi = 0.0

# Quick metrics
mao = arv * 0.70 - rehab_budget  # 70% rule: Max Allowable Offer
break_even_price = net_proceeds - rehab_budget - closing_buy - holding_total
profit_margin_pct = (gross_profit / arv * 100.0) if arv > 0 else 0.0
rating = _deal_rating(profit_margin_pct)

# =========================================================
# RIGHT -- Deal Metrics
# =========================================================
with right_col:
    st.subheader("Deal Metrics")

    # ── All-In Cost ──────────────────────────────────────────────────────────
    st.markdown("**ALL-IN COST**")
    cost_lines = [
        ("Purchase", purchase_price),
        ("Rehab", rehab_budget),
        (f"Closing -- Buy ({closing_pct:.2f}%)", closing_buy),
        (f"Holding ({holding_months} mo @ ${holding_cost_mo:,}/mo)", holding_total),
    ]
    for label, val in cost_lines:
        col_a, col_b = st.columns([3, 1])
        col_a.write(f"&nbsp;&nbsp;{label}")
        col_b.write(_fmt(val))

    st.markdown(
        "<hr style='margin:4px 0; border-color:#555;'/>",
        unsafe_allow_html=True,
    )
    col_a, col_b = st.columns([3, 1])
    col_a.markdown("**Total All-In**")
    col_b.markdown(f"**{_fmt(total_all_in)}**")

    st.markdown("")

    # ── Returns ──────────────────────────────────────────────────────────────
    st.markdown("**RETURNS**")
    return_lines = [
        (f"ARV -- {arv_source}", arv),
        ("Closing -- Sell (6%)", -closing_sell),
        ("Net Proceeds", net_proceeds),
    ]
    for label, val in return_lines:
        col_a, col_b = st.columns([3, 1])
        col_a.write(f"&nbsp;&nbsp;{label}")
        col_b.write(_fmt(abs(val)) if val != net_proceeds else _fmt(val))

    st.markdown(
        "<hr style='margin:4px 0; border-color:#555;'/>",
        unsafe_allow_html=True,
    )

    # Gross profit -- color coded
    gp_color = "green" if gross_profit >= 0 else "red"
    col_a, col_b = st.columns([3, 1])
    col_a.markdown("**Gross Profit**")
    col_b.markdown(
        f"<span style='color:{gp_color}; font-weight:bold;'>{_fmt(gross_profit)}</span>",
        unsafe_allow_html=True,
    )

    col_a, col_b = st.columns([3, 1])
    np_color = "green" if net_profit >= 0 else "red"
    col_a.markdown("**Net Profit**")
    col_b.markdown(
        f"<span style='color:{np_color}; font-weight:bold;'>{_fmt(net_profit)}</span>",
        unsafe_allow_html=True,
    )

    st.markdown("")

    # ── Leverage ─────────────────────────────────────────────────────────────
    if financing == "Leveraged":
        st.markdown("**LEVERAGE**")
        lev_lines = [
            (f"Down Payment ({down_pct}%)", down_payment),
            ("Loan Amount", loan_amount),
            ("Monthly P&I", monthly_pi),
        ]
        for label, val in lev_lines:
            col_a, col_b = st.columns([3, 1])
            col_a.write(f"&nbsp;&nbsp;{label}")
            col_b.write(_fmt(val))

        col_a, col_b = st.columns([3, 1])
        col_a.write("&nbsp;&nbsp;Cash-on-Cash ROI")
        roi_color = "green" if coc_roi >= 10 else ("orange" if coc_roi >= 0 else "red")
        col_b.markdown(
            f"<span style='color:{roi_color}; font-weight:bold;'>{coc_roi:.1f}%</span>",
            unsafe_allow_html=True,
        )

        st.markdown("")

    # ── Quick Metrics ─────────────────────────────────────────────────────────
    st.markdown("**QUICK METRICS**")

    quick_lines = [
        ("Max Allowable Offer (70% rule)", _fmt(mao)),
        ("Break-even Price", _fmt(break_even_price)),
        ("Profit Margin", f"{profit_margin_pct:.1f}%"),
        ("Deal Rating", rating),
    ]
    for label, val in quick_lines:
        col_a, col_b = st.columns([3, 1])
        col_a.write(f"&nbsp;&nbsp;{label}")
        col_b.write(val)


# ── Comparable Sales Table ────────────────────────────────────────────────────
st.markdown("---")
st.subheader("Comparable Sales")

if not _COMPS_AVAILABLE:
    st.info("Comparable sales module not available. Install `ingestion/comp_sales.py` and ensure `parcels.duckdb` exists.")
elif not prop_lat or not prop_lng:
    st.info("No coordinates available for this property -- cannot fetch comps.")
else:
    comps_df = (
        comp_result["comps_df"]
        if comp_result is not None
        else get_comps(
            lat=prop_lat,
            lng=prop_lng,
            sqft=prop_sqft,
            db_path=os.path.join(_ROOT, "data", "parcels.duckdb"),
        )
    )

    if comps_df.empty:
        st.info("No comparable sales found within 0.5 miles in the last 3 years. "
                "Make sure parcels.duckdb is populated.")
    else:
        display_df = comps_df.copy()

        # Format columns for display
        if "sale_price" in display_df.columns:
            display_df["sale_price"] = display_df["sale_price"].apply(
                lambda v: f"${v:,.0f}" if pd.notna(v) else "--"
            )
        if "price_per_sqft" in display_df.columns:
            display_df["price_per_sqft"] = display_df["price_per_sqft"].apply(
                lambda v: f"${v:.0f}/sqft" if pd.notna(v) else "--"
            )
        if "dist_miles" in display_df.columns:
            display_df["dist_miles"] = display_df["dist_miles"].apply(
                lambda v: f"{v:.3f} mi" if pd.notna(v) else "--"
            )
        if "sqft" in display_df.columns:
            display_df["sqft"] = display_df["sqft"].apply(
                lambda v: f"{int(v):,}" if pd.notna(v) else "--"
            )

        display_df = display_df.rename(columns={
            "address": "Address",
            "sale_date": "Sale Date",
            "sale_price": "Sale Price",
            "sqft": "Sqft",
            "price_per_sqft": "$/Sqft",
            "dist_miles": "Distance",
            "year_built": "Year Built",
        })

        st.dataframe(display_df, use_container_width=True, hide_index=True)

# ── Footnotes ─────────────────────────────────────────────────────────────────
st.markdown("---")
st.caption(
    "**70% Rule:** Maximum Allowable Offer = ARV × 0.70 − Rehab Budget. "
    "This is a common fix-and-flip heuristic ensuring enough margin to cover all costs and still profit. "
    "It is a guideline, not a guarantee."
)
st.caption(
    "**Deal Rating:** ⭐⭐⭐⭐ Excellent (>20% margin) · ⭐⭐⭐ Good (10-20%) · "
    "⭐⭐ Fair (5-10%) · ⭐ Weak (<5%). Margin = Gross Profit ÷ ARV."
)
st.caption(
    "**ARV** (After-Repair Value) is estimated from comparable sales when parcel data is available, "
    "or from EMV × 1.15 as a fallback. Use the ARV Override field to enter your own figure."
)
