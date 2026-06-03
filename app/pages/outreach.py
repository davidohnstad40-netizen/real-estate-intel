"""
app/pages/outreach.py

Streamlit outreach tracking page for the real-estate-intel platform.

Tabs:
  1. Today's Knock List  — optimized route with Folium map + Google Maps link
  2. Log Contact         — form to record a door knock / call / letter
  3. Follow-up Queue     — upcoming and overdue follow-ups + full history
"""
import sys
import os
import math
import json
import datetime
import urllib.parse

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
# Optional map dependencies
# ---------------------------------------------------------------------------
try:
    import folium
    from streamlit_folium import st_folium
    _MAP_AVAILABLE = True
except ImportError:
    _MAP_AVAILABLE = False

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
ROUTE_START_LAT = 45.184
ROUTE_START_LNG = -93.186
BLAINE_MN       = "Blaine MN"

TIER_COLOR = {"T1": "red", "T2": "orange", "T3": "blue", "TBD": "gray"}

CONTACT_LOG_DDL = """
CREATE TABLE IF NOT EXISTS contact_log (
    log_id          VARCHAR PRIMARY KEY,
    property_id     VARCHAR,
    contact_date    DATE,
    method          VARCHAR,
    outcome         VARCHAR,
    notes           TEXT,
    follow_up_date  DATE,
    created_at      TIMESTAMP DEFAULT current_timestamp
)
"""

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def nearest_neighbor_route(
    props_list,
    start_lat: float = ROUTE_START_LAT,
    start_lng: float = ROUTE_START_LNG,
):
    """Return props_list re-ordered by nearest-neighbor starting at start coords."""
    remaining = [p for p in props_list if p.get("lat") and p.get("lng")]
    route = []
    cur = (start_lat, start_lng)
    while remaining:
        nearest = min(
            remaining,
            key=lambda p: math.sqrt((p["lat"] - cur[0]) ** 2 + (p["lng"] - cur[1]) ** 2),
        )
        route.append(nearest)
        remaining.remove(nearest)
        cur = (nearest["lat"], nearest["lng"])
    return route


def google_maps_route_url(route):
    """Build a Google Maps directions URL for the ordered route."""
    if not route:
        return None
    parts = [f"{r['address']} {BLAINE_MN}" for r in route]
    encoded = [urllib.parse.quote(p) for p in parts]
    return "https://www.google.com/maps/dir/" + "/".join(encoded)


def tier_badge(tier: str) -> str:
    colors = {"T1": "🔴", "T2": "🟠", "T3": "🔵", "TBD": "⚪"}
    return f"{colors.get(tier, '⚪')} {tier}"


# ---------------------------------------------------------------------------
# Page
# ---------------------------------------------------------------------------

def main():
    st.title("🚪 Outreach Tracker")

    con = get_db()

    # Ensure contact_log exists
    con.execute(CONTACT_LOG_DDL)

    tab1, tab2, tab3 = st.tabs(["Today's Knock List", "Log Contact", "Follow-up Queue"])

    # ====================================================================
    # TAB 1 — Today's Knock List
    # ====================================================================
    with tab1:
        st.subheader("Today's Knock List")

        # Load T1 + T2 properties with lat/lng
        knock_rows = con.execute("""
            SELECT
                p.id,
                p.address,
                p.lat,
                p.lng,
                p.owner_name,
                ps.knock_tier,
                ps.motivation_score,
                ps.primary_signal
            FROM properties p
            JOIN property_scores ps ON ps.id = p.id
            WHERE ps.knock_tier IN ('T1', 'T2')
              AND p.lat IS NOT NULL
              AND p.lng IS NOT NULL
            ORDER BY ps.motivation_score DESC
        """).fetchall()

        if not knock_rows:
            st.info("No T1 or T2 properties with coordinates yet. Score some properties first.")
        else:
            props_list = [
                {
                    "id":           r[0],
                    "address":      r[1],
                    "lat":          r[2],
                    "lng":          r[3],
                    "owner_name":   r[4] or "",
                    "tier":         r[5],
                    "score":        r[6],
                    "signal":       r[7] or "",
                }
                for r in knock_rows
            ]

            route = nearest_neighbor_route(props_list)

            # ---- Table -------------------------------------------------- #
            table_data = []
            for i, p in enumerate(route, start=1):
                table_data.append({
                    "#":       i,
                    "Address": p["address"],
                    "Tier":    tier_badge(p["tier"]),
                    "Score":   p["score"],
                    "Owner":   p["owner_name"],
                    "Signal":  p["signal"],
                })

            df = pd.DataFrame(table_data)
            st.dataframe(df, use_container_width=True, hide_index=True)

            # ---- Google Maps link --------------------------------------- #
            maps_url = google_maps_route_url(route)
            if maps_url:
                st.markdown(f"[🗺️ Open Google Maps Route]({maps_url})", unsafe_allow_html=True)

            # ---- Folium map -------------------------------------------- #
            if _MAP_AVAILABLE:
                center_lat = sum(p["lat"] for p in route) / len(route)
                center_lng = sum(p["lng"] for p in route) / len(route)
                m = folium.Map(location=[center_lat, center_lng], zoom_start=14)

                # Draw route line
                line_coords = [(p["lat"], p["lng"]) for p in route]
                folium.PolyLine(
                    locations=line_coords,
                    color="#555",
                    weight=2,
                    opacity=0.6,
                    dash_array="6",
                ).add_to(m)

                # Numbered markers
                for i, p in enumerate(route, start=1):
                    color = TIER_COLOR.get(p["tier"], "gray")
                    folium.Marker(
                        location=[p["lat"], p["lng"]],
                        popup=folium.Popup(
                            f"<b>#{i} {p['address']}</b><br>"
                            f"Tier: {p['tier']} | Score: {p['score']}<br>"
                            f"Owner: {p['owner_name']}<br>"
                            f"Signal: {p['signal']}",
                            max_width=250,
                        ),
                        tooltip=f"#{i} — {p['address']}",
                        icon=folium.Icon(color=color, icon="home", prefix="fa"),
                    ).add_to(m)

                st_folium(m, width=None, height=500)
            else:
                st.warning(
                    "Map unavailable — install folium and streamlit-folium:\n"
                    "`pip install folium streamlit-folium`"
                )

    # ====================================================================
    # TAB 2 — Log Contact
    # ====================================================================
    with tab2:
        st.subheader("Log a Contact")

        # Load all properties for the dropdown
        all_props = con.execute("""
            SELECT p.id, p.address, COALESCE(ps.knock_tier, 'TBD') AS tier
            FROM properties p
            LEFT JOIN property_scores ps ON ps.id = p.id
            ORDER BY p.address
        """).fetchall()

        if not all_props:
            st.info("No properties in the database yet.")
        else:
            prop_options = {f"{r[1]} [{r[2]}]": r[0] for r in all_props}
            selected_label = st.selectbox("Property", list(prop_options.keys()))
            selected_id    = prop_options[selected_label]

            col1, col2 = st.columns(2)
            with col1:
                contact_date = st.date_input("Contact Date", value=datetime.date.today())
            with col2:
                method = st.radio(
                    "Method",
                    ["Door knock", "Letter left", "Phone", "Email"],
                    horizontal=True,
                )

            outcome = st.selectbox(
                "Outcome",
                [
                    "No answer",
                    "Spoke - interested",
                    "Spoke - not interested",
                    "Left note",
                    "Left voicemail",
                    "Callback scheduled",
                    "Not home",
                ],
            )

            notes = st.text_area("Notes", placeholder="Any details about the interaction …")

            follow_up_date = st.date_input(
                "Follow-up Date (optional — leave blank to skip)",
                value=None,
            )

            if st.button("✅ Submit Contact Log", type="primary"):
                log_id = str(datetime.datetime.now().timestamp())
                con.execute(
                    """
                    INSERT INTO contact_log
                        (log_id, property_id, contact_date, method, outcome, notes, follow_up_date)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    [
                        log_id,
                        selected_id,
                        contact_date,
                        method,
                        outcome,
                        notes if notes else None,
                        follow_up_date if follow_up_date else None,
                    ],
                )
                st.success(f"✅ Contact logged for **{selected_label}** on {contact_date}.")

    # ====================================================================
    # TAB 3 — Follow-up Queue
    # ====================================================================
    with tab3:
        st.subheader("Follow-up Queue")

        today     = datetime.date.today()
        week_out  = today + datetime.timedelta(days=7)

        # Upcoming follow-ups within 7 days
        followup_rows = con.execute("""
            SELECT
                cl.log_id,
                p.address,
                cl.contact_date,
                cl.method,
                cl.outcome,
                cl.notes,
                cl.follow_up_date
            FROM contact_log cl
            JOIN properties p ON p.id = cl.property_id
            WHERE cl.follow_up_date IS NOT NULL
              AND cl.follow_up_date <= ?
            ORDER BY cl.follow_up_date ASC
        """, [week_out]).fetchall()

        cols = ["log_id", "Address", "Contact Date", "Method", "Outcome", "Notes", "Follow-up Date"]

        if not followup_rows:
            st.info("No follow-ups due in the next 7 days.")
        else:
            st.markdown("**Due within 7 days:**")
            styled_rows = []
            for r in followup_rows:
                fu_date = r[6]
                if isinstance(fu_date, str):
                    fu_date = datetime.date.fromisoformat(fu_date)

                if fu_date < today:
                    tag = "🔴 OVERDUE"
                elif fu_date == today:
                    tag = "🟠 TODAY"
                else:
                    tag = ""

                styled_rows.append({
                    "Status":        tag,
                    "Address":       r[1],
                    "Follow-up":     str(r[6]),
                    "Method":        r[3],
                    "Outcome":       r[4],
                    "Notes":         r[5] or "",
                    "Contact Date":  str(r[2]),
                })

            df_fu = pd.DataFrame(styled_rows)
            st.dataframe(df_fu, use_container_width=True, hide_index=True)

        # ---- Full history (last 30 entries) ----------------------------- #
        st.markdown("---")
        st.subheader("Contact History (last 30)")

        history_rows = con.execute("""
            SELECT
                p.address,
                cl.contact_date,
                cl.method,
                cl.outcome,
                cl.notes,
                cl.follow_up_date,
                cl.created_at
            FROM contact_log cl
            JOIN properties p ON p.id = cl.property_id
            ORDER BY cl.created_at DESC
            LIMIT 30
        """).fetchall()

        if not history_rows:
            st.info("No contact history yet.")
        else:
            df_hist = pd.DataFrame(
                history_rows,
                columns=["Address", "Contact Date", "Method", "Outcome", "Notes", "Follow-up Date", "Logged At"],
            )
            st.dataframe(df_hist, use_container_width=True, hide_index=True)

    con.close()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    main()
else:
    # When imported by Streamlit as a page, run immediately
    main()
