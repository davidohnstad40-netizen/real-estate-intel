"""
app/pages/pipeline.py

Streamlit CRM pipeline page for the real-estate-intel platform.

Sections:
  1. Summary metrics (total in pipeline, active offers, closed, est. profit)
  2. Kanban-style columns for stages Identified → Offer Made
  3. Add to Pipeline (from scored properties not yet in pipeline)
  4. Move Stage form (update stage, offer price, close date, notes)
  5. Closed Deals table
  6. Dead / Pass table (collapsed by default)

Stage order:
  Identified → Researching → Contacted → Warm Lead → Offer Made
  → Under Contract → Closed → Dead/Pass

Days-in-stage warning: > 14 days with no update triggers a caution indicator.
"""

import sys
import os
import datetime
import json
import uuid

import streamlit as st
import pandas as pd

# ---------------------------------------------------------------------------
# Path setup
# ---------------------------------------------------------------------------
_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from db.schema import get_db

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

STAGES = [
    "Identified",
    "Researching",
    "Contacted",
    "Warm Lead",
    "Offer Made",
    "Under Contract",
    "Closed",
    "Dead/Pass",
]

# Stages shown as kanban columns
KANBAN_STAGES = ["Identified", "Researching", "Contacted", "Warm Lead", "Offer Made"]

STAGE_COLORS = {
    "Identified":     "#e0f2fe",  # light blue
    "Researching":    "#fef9c3",  # light yellow
    "Contacted":      "#fef3c7",  # amber-50
    "Warm Lead":      "#ffedd5",  # orange-50
    "Offer Made":     "#fce7f3",  # pink-50
    "Under Contract": "#f0fdf4",  # green-50
    "Closed":         "#dcfce7",  # green-100
    "Dead/Pass":      "#f1f5f9",  # slate-100
}

TIER_BADGE = {
    "T1":  "🔴 T1",
    "T2":  "🟠 T2",
    "T3":  "🔵 T3",
    "TBD": "⚪ TBD",
}

PIPELINE_DDL = """
CREATE TABLE IF NOT EXISTS property_pipeline (
    id               VARCHAR PRIMARY KEY,
    stage            VARCHAR DEFAULT 'Identified',
    last_offer_price DOUBLE,
    last_offer_date  DATE,
    accepted_price   DOUBLE,
    close_date       DATE,
    profit_est       DOUBLE,
    notes            TEXT,
    updated_at       TIMESTAMP DEFAULT current_timestamp
)
"""

STALE_DAYS = 14  # warn if no update in this many days


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------

def _ensure_pipeline_table(con):
    con.execute(PIPELINE_DDL)


def _load_pipeline(con) -> pd.DataFrame:
    """Return all pipeline rows joined to properties and scores."""
    rows = con.execute("""
        SELECT
            pp.id,
            p.address,
            p.city,
            p.state,
            COALESCE(ps.knock_tier, 'TBD')     AS knock_tier,
            COALESCE(ps.motivation_score, 0)   AS motivation_score,
            ps.primary_signal,
            pp.stage,
            pp.last_offer_price,
            pp.last_offer_date,
            pp.accepted_price,
            pp.close_date,
            pp.profit_est,
            pp.notes,
            pp.updated_at
        FROM property_pipeline pp
        JOIN properties p         ON p.id  = pp.id
        LEFT JOIN property_scores ps ON ps.id = pp.id
        ORDER BY ps.motivation_score DESC NULLS LAST
    """).fetchall()

    cols = [
        "id", "address", "city", "state", "knock_tier", "motivation_score",
        "primary_signal", "stage", "last_offer_price", "last_offer_date",
        "accepted_price", "close_date", "profit_est", "notes", "updated_at",
    ]
    return pd.DataFrame(rows, columns=cols)


def _load_properties_not_in_pipeline(con) -> list[dict]:
    """Return properties not yet in property_pipeline, sorted by motivation_score DESC."""
    rows = con.execute("""
        SELECT
            p.id,
            p.address,
            COALESCE(ps.knock_tier, 'TBD')    AS knock_tier,
            COALESCE(ps.motivation_score, 0)  AS motivation_score,
            ps.primary_signal
        FROM properties p
        LEFT JOIN property_scores ps ON ps.id = p.id
        WHERE p.id NOT IN (SELECT id FROM property_pipeline)
        ORDER BY ps.motivation_score DESC NULLS LAST
    """).fetchall()

    return [
        {
            "id":               r[0],
            "address":          r[1],
            "knock_tier":       r[2],
            "motivation_score": r[3],
            "primary_signal":   r[4],
        }
        for r in rows
    ]


def _days_since(updated_at) -> int:
    """Return number of days since updated_at (accepts datetime or string)."""
    if updated_at is None:
        return 0
    if isinstance(updated_at, str):
        try:
            updated_at = datetime.datetime.fromisoformat(updated_at)
        except ValueError:
            return 0
    if isinstance(updated_at, datetime.datetime):
        delta = datetime.datetime.now() - updated_at
    elif isinstance(updated_at, datetime.date):
        delta = datetime.date.today() - updated_at
    else:
        return 0
    return max(0, delta.days)


def _fmt_currency(val) -> str:
    if val is None or (isinstance(val, float) and pd.isna(val)):
        return "--"
    try:
        return f"${float(val):,.0f}"
    except (TypeError, ValueError):
        return str(val)


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def main():
    st.title("Deal Pipeline")

    # Use write connection so we can create the table and save updates.
    # If DuckDB is locked by another writer, get_db will raise a clear error.
    con = None
    try:
        con = get_db()
    except RuntimeError as e:
        st.error(str(e))
        st.stop()
        return

    if con is None:
        return

    _ensure_pipeline_table(con)

    df_pipe = _load_pipeline(con)

    # ========================================================================
    # 1. SUMMARY METRICS
    # ========================================================================
    active_stages = [s for s in STAGES if s not in ("Closed", "Dead/Pass")]
    df_active   = df_pipe[df_pipe["stage"].isin(active_stages)]
    df_offers   = df_pipe[df_pipe["stage"] == "Offer Made"]
    df_closed   = df_pipe[df_pipe["stage"] == "Closed"]

    total_in_pipeline = len(df_active)
    active_offers     = len(df_offers)
    closed_deals      = len(df_closed)
    total_profit      = df_closed["profit_est"].dropna().sum()

    col_a, col_b, col_c, col_d = st.columns(4)
    col_a.metric("In Pipeline",    total_in_pipeline)
    col_b.metric("Active Offers",  active_offers)
    col_c.metric("Closed Deals",   closed_deals)
    col_d.metric("Est. Profit",    _fmt_currency(total_profit) if total_profit else "--")

    st.markdown("---")

    # ========================================================================
    # 2. KANBAN COLUMNS
    # ========================================================================
    st.subheader("Active Pipeline")

    kanban_cols = st.columns(len(KANBAN_STAGES))

    for col_idx, stage in enumerate(KANBAN_STAGES):
        with kanban_cols[col_idx]:
            stage_df = df_pipe[df_pipe["stage"] == stage]
            stage_color = STAGE_COLORS.get(stage, "#f8fafc")

            st.markdown(
                f"<div style='background:{stage_color}; padding:6px 10px; "
                f"border-radius:6px; font-weight:600; font-size:0.85rem; "
                f"margin-bottom:8px;'>{stage} "
                f"<span style='color:#64748b'>({len(stage_df)})</span></div>",
                unsafe_allow_html=True,
            )

            if stage_df.empty:
                st.markdown(
                    "<div style='color:#94a3b8; font-size:0.8rem; "
                    "padding:8px 0;'>No deals</div>",
                    unsafe_allow_html=True,
                )
            else:
                for _, row in stage_df.iterrows():
                    days = _days_since(row["updated_at"])
                    stale = days > STALE_DAYS
                    tier_label = TIER_BADGE.get(row["knock_tier"], row["knock_tier"])
                    border_color = "#ef4444" if stale else "#e2e8f0"

                    card_lines = [
                        f"**{row['address']}**",
                        f"{tier_label} | Score: {row['motivation_score']}",
                    ]
                    if row["last_offer_price"] and not pd.isna(row["last_offer_price"]):
                        card_lines.append(f"Offer: {_fmt_currency(row['last_offer_price'])}")
                    if stale:
                        card_lines.append(f"**⚠ {days}d -- no update**")
                    else:
                        card_lines.append(f"{days}d in stage")

                    card_html = (
                        f"<div style='border:1px solid {border_color}; "
                        f"border-radius:6px; padding:8px 10px; margin-bottom:8px; "
                        f"background:#fff; font-size:0.82rem; line-height:1.5;'>"
                        + "<br/>".join(
                            line.replace("**", "<b>", 1).replace("**", "</b>", 1)
                            if "**" in line else line
                            for line in card_lines
                        )
                        + "</div>"
                    )
                    st.markdown(card_html, unsafe_allow_html=True)

                    with st.expander(f"Details -- {row['address'][:28]}"):
                        st.write(f"**Stage:** {row['stage']}")
                        st.write(f"**Tier:** {row['knock_tier']}  |  **Score:** {row['motivation_score']}")
                        if row.get("primary_signal"):
                            st.write(f"**Signal:** {row['primary_signal']}")
                        st.write(f"**Last Offer:** {_fmt_currency(row['last_offer_price'])}")
                        if row["last_offer_date"] and not pd.isna(row["last_offer_date"]):
                            st.write(f"**Offer Date:** {row['last_offer_date']}")
                        st.write(f"**Days in Stage:** {days}" + (" ⚠ Stale" if stale else ""))
                        st.write(f"**Notes:** {row['notes'] or '--'}")

                        # Quick advance button
                        current_idx = STAGES.index(stage) if stage in STAGES else 0
                        next_stages = STAGES[current_idx + 1:] if current_idx + 1 < len(STAGES) else []
                        if next_stages:
                            next_stage = st.selectbox(
                                "Advance to stage",
                                next_stages,
                                key=f"advance_{row['id']}",
                            )
                            if st.button("Move", key=f"move_btn_{row['id']}"):
                                con.execute(
                                    """
                                    UPDATE property_pipeline
                                    SET stage = ?, updated_at = current_timestamp
                                    WHERE id = ?
                                    """,
                                    [next_stage, row["id"]],
                                )
                                st.success(f"Moved to {next_stage}")
                                st.rerun()

    # Under Contract section
    df_uc = df_pipe[df_pipe["stage"] == "Under Contract"]
    if not df_uc.empty:
        st.markdown("---")
        st.subheader("Under Contract")
        for _, row in df_uc.iterrows():
            days = _days_since(row["updated_at"])
            stale = days > STALE_DAYS
            st.info(
                f"**{row['address']}** -- "
                f"Offer: {_fmt_currency(row['last_offer_price'])} | "
                f"Close: {row['close_date'] or 'TBD'} | "
                f"{days}d since update"
                + (" ⚠ Stale" if stale else "")
            )

    st.markdown("---")

    # ========================================================================
    # 3. ADD TO PIPELINE
    # ========================================================================
    st.subheader("Add to Pipeline")

    available_props = _load_properties_not_in_pipeline(con)

    if not available_props:
        st.info("All scored properties are already in the pipeline.")
    else:
        prop_options = {
            f"{p['address']} [{p['knock_tier']} | Score {p['motivation_score']}]": p["id"]
            for p in available_props
        }
        add_selected_label = st.selectbox(
            "Select property to add",
            list(prop_options.keys()),
            key="add_prop_select",
        )
        add_selected_id = prop_options[add_selected_label]

        if st.button("Add to Pipeline at Identified", type="primary", key="add_pipeline_btn"):
            # Use INSERT OR IGNORE in case of race condition
            con.execute(
                """
                INSERT OR IGNORE INTO property_pipeline (id, stage, updated_at)
                VALUES (?, 'Identified', current_timestamp)
                """,
                [add_selected_id],
            )
            st.success(f"Added **{add_selected_label}** to pipeline at Identified stage.")
            st.rerun()

    st.markdown("---")

    # ========================================================================
    # 4. MOVE STAGE FORM
    # ========================================================================
    st.subheader("Move / Update Deal")

    df_updatable = df_pipe[~df_pipe["stage"].isin(["Closed", "Dead/Pass"])]

    if df_updatable.empty:
        st.info("No active deals to update.")
    else:
        move_options = {
            f"{row['address']} [{row['stage']}]": row["id"]
            for _, row in df_updatable.iterrows()
        }
        move_selected_label = st.selectbox(
            "Property to update",
            list(move_options.keys()),
            key="move_prop_select",
        )
        move_selected_id = move_options[move_selected_label]

        # Get current stage to pre-select next
        current_row = df_updatable[df_updatable["id"] == move_selected_id].iloc[0]
        current_stage = current_row["stage"]
        current_stage_idx = STAGES.index(current_stage) if current_stage in STAGES else 0

        new_stage = st.selectbox(
            "New Stage",
            STAGES,
            index=current_stage_idx,
            key="move_new_stage",
        )

        move_col1, move_col2 = st.columns(2)
        with move_col1:
            offer_price = st.number_input(
                "Offer Price ($) -- optional",
                min_value=0.0,
                value=float(current_row["last_offer_price"]) if (
                    current_row["last_offer_price"] is not None
                    and not pd.isna(current_row["last_offer_price"])
                ) else 0.0,
                step=1000.0,
                format="%.0f",
                key="move_offer_price",
            )
        with move_col2:
            close_date_val = None
            if current_row["close_date"] is not None and not (
                isinstance(current_row["close_date"], float) and pd.isna(current_row["close_date"])
            ):
                try:
                    close_date_val = (
                        current_row["close_date"]
                        if isinstance(current_row["close_date"], datetime.date)
                        else datetime.date.fromisoformat(str(current_row["close_date"]))
                    )
                except (ValueError, TypeError):
                    close_date_val = None

            close_date_input = st.date_input(
                "Expected Close Date -- optional",
                value=close_date_val,
                key="move_close_date",
            )

        # Profit estimate (only meaningful when Closed)
        profit_est_input = None
        accepted_price_input = None
        if new_stage in ("Closed", "Under Contract"):
            profit_col1, profit_col2 = st.columns(2)
            with profit_col1:
                accepted_price_input = st.number_input(
                    "Accepted Price ($)",
                    min_value=0.0,
                    value=float(current_row["accepted_price"]) if (
                        current_row["accepted_price"] is not None
                        and not pd.isna(current_row["accepted_price"])
                    ) else 0.0,
                    step=1000.0,
                    format="%.0f",
                    key="move_accepted_price",
                )
            with profit_col2:
                profit_est_input = st.number_input(
                    "Est. Profit ($)",
                    min_value=0.0,
                    value=float(current_row["profit_est"]) if (
                        current_row["profit_est"] is not None
                        and not pd.isna(current_row["profit_est"])
                    ) else 0.0,
                    step=1000.0,
                    format="%.0f",
                    key="move_profit_est",
                )

        move_notes = st.text_area(
            "Notes",
            value=current_row["notes"] or "",
            placeholder="Add context about this stage change…",
            key="move_notes",
        )

        if st.button("Save Update", type="primary", key="move_save_btn"):
            update_offer = offer_price if offer_price > 0 else None
            update_close = close_date_input if close_date_input else None
            update_accepted = accepted_price_input if (accepted_price_input and accepted_price_input > 0) else None
            update_profit   = profit_est_input if (profit_est_input and profit_est_input > 0) else None

            con.execute(
                """
                UPDATE property_pipeline SET
                    stage            = ?,
                    last_offer_price = COALESCE(?, last_offer_price),
                    last_offer_date  = CASE WHEN ? IS NOT NULL THEN current_date ELSE last_offer_date END,
                    accepted_price   = COALESCE(?, accepted_price),
                    close_date       = COALESCE(?, close_date),
                    profit_est       = COALESCE(?, profit_est),
                    notes            = ?,
                    updated_at       = current_timestamp
                WHERE id = ?
                """,
                [
                    new_stage,
                    update_offer,
                    update_offer,   # for the CASE condition
                    update_accepted,
                    update_close,
                    update_profit,
                    move_notes if move_notes else None,
                    move_selected_id,
                ],
            )
            st.success(f"Updated **{move_selected_label}** → **{new_stage}**")
            st.rerun()

    st.markdown("---")

    # ========================================================================
    # 5. CLOSED DEALS TABLE
    # ========================================================================
    st.subheader("Closed Deals")

    df_closed_full = df_pipe[df_pipe["stage"] == "Closed"].copy()

    if df_closed_full.empty:
        st.info("No closed deals yet.")
    else:
        display_closed = df_closed_full[[
            "address", "accepted_price", "close_date", "profit_est", "notes"
        ]].copy()
        display_closed.columns = ["Address", "Accepted Price", "Close Date", "Est. Profit", "Notes"]
        display_closed["Accepted Price"] = display_closed["Accepted Price"].apply(_fmt_currency)
        display_closed["Est. Profit"]    = display_closed["Est. Profit"].apply(_fmt_currency)
        display_closed["Notes"]          = display_closed["Notes"].fillna("--")
        st.dataframe(display_closed, use_container_width=True, hide_index=True)

    # ========================================================================
    # 6. DEAD / PASS TABLE (collapsed)
    # ========================================================================
    df_dead = df_pipe[df_pipe["stage"] == "Dead/Pass"]

    with st.expander(f"Dead / Pass ({len(df_dead)})"):
        if df_dead.empty:
            st.info("No dead/passed deals.")
        else:
            display_dead = df_dead[[
                "address", "knock_tier", "motivation_score", "notes", "updated_at"
            ]].copy()
            display_dead.columns = ["Address", "Tier", "Score", "Notes", "Last Updated"]
            display_dead["Notes"] = display_dead["Notes"].fillna("--")
            st.dataframe(display_dead, use_container_width=True, hide_index=True)

    # ========================================================================
    # 7. STALE DEAL WARNINGS (summary at bottom)
    # ========================================================================
    stale_active = df_active.copy()
    stale_active["days"] = stale_active["updated_at"].apply(_days_since)
    stale_deals = stale_active[stale_active["days"] > STALE_DAYS]

    if not stale_deals.empty:
        st.markdown("---")
        st.warning(
            f"**{len(stale_deals)} deal(s) have not been updated in {STALE_DAYS}+ days:**\n"
            + "  \n".join(
                f"- {row['address']} ({row['stage']}) -- {row['days']}d"
                for _, row in stale_deals.iterrows()
            )
        )

    con.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
else:
    main()
