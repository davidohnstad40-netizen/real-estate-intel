"""
Real Estate Seller Intelligence Platform
Draw a region on the map → discover + score every property inside it.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
import pandas as pd
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
from shapely.geometry import Point, shape

from db.schema import get_db
from scoring.motivation import TIER_COLOR, TIER_LABEL

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="REI — Seller Intelligence",
    page_icon="🏘️",
    layout="wide",
    initial_sidebar_state="collapsed",
)

st.markdown("""
<style>
  .block-container { padding-top: 1rem; }
  .tier-T1  { background:#C00000; color:white;  border-radius:4px; padding:2px 8px; font-weight:bold; }
  .tier-T2  { background:#D6A800; color:white;  border-radius:4px; padding:2px 8px; font-weight:bold; }
  .tier-T3  { background:#375623; color:white;  border-radius:4px; padding:2px 8px; }
  .tier-SKIP{ background:#888888; color:white;  border-radius:4px; padding:2px 8px; }
  .tier-TBD { background:#AAAAAA; color:white;  border-radius:4px; padding:2px 8px; }
  .metric-box { background:#f8f8f8; border-radius:8px; padding:10px; margin:4px; }
</style>
""", unsafe_allow_html=True)

# ── load data ─────────────────────────────────────────────────────────────────
@st.cache_resource
def get_connection():
    return get_db()

@st.cache_data(ttl=60)
def load_properties():
    con = get_connection()
    df = con.execute("""
        SELECT
            p.id, p.address, p.lat, p.lng, p.owner_name,
            p.beds, p.baths, p.sqft, p.year_built,
            p.emv, p.est_value, p.prior_sale_price, p.prior_sale_year,
            p.years_owned, p.homestead, p.owner_type, p.anoka_pin,
            s.motivation_score, s.knock_tier, s.primary_signal,
            s.est_equity_usd, s.equity_pct, s.monthly_piti, s.score_factors
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        ORDER BY s.motivation_score DESC NULLS LAST
    """).df()
    return df

def filter_by_polygon(df, polygon_geojson):
    if not polygon_geojson or df.empty:
        return df
    try:
        poly = shape(polygon_geojson)
        mask = df.apply(
            lambda r: poly.contains(Point(r.lng, r.lat))
            if pd.notna(r.lat) and pd.notna(r.lng) else False,
            axis=1
        )
        return df[mask]
    except Exception:
        return df

# ── sidebar filters ───────────────────────────────────────────────────────────
with st.sidebar:
    st.header("Filters")
    tier_filter = st.multiselect(
        "Knock Tier", ["T1","T2","T3","SKIP","TBD"],
        default=["T1","T2","T3","TBD"]
    )
    min_score = st.slider("Min Motivation Score", 0, 100, 0)
    show_all  = st.checkbox("Show all (ignore polygon)", value=True)
    st.divider()
    st.caption("Draw a polygon on the map to filter properties by region.")

# ── main ──────────────────────────────────────────────────────────────────────
st.title("🏘️ Real Estate Seller Intelligence")
st.caption("Draw a region on the map → score every property inside it → knock on T1s first.")

df_all = load_properties()

# ── MAP ───────────────────────────────────────────────────────────────────────
map_col, detail_col = st.columns([3, 1])

with map_col:
    # Center on neighborhood
    has_coords = df_all.dropna(subset=["lat","lng"])
    center_lat = has_coords.lat.mean() if not has_coords.empty else 45.163
    center_lng = has_coords.lng.mean() if not has_coords.empty else -93.205

    m = folium.Map(
        location=[center_lat, center_lng],
        zoom_start=15,
        tiles="CartoDB positron",
    )

    # Drawing tools
    Draw(
        draw_options={
            "polygon":   {"shapeOptions": {"color": "#0078D4", "fillOpacity": 0.15}},
            "rectangle": {"shapeOptions": {"color": "#0078D4", "fillOpacity": 0.15}},
            "polyline":  False,
            "circle":    False,
            "marker":    False,
            "circlemarker": False,
        },
        edit_options={"edit": False, "remove": True},
    ).add_to(m)

    # Plot all geocoded properties
    tier_icons = {"T1": "🔴", "T2": "🟡", "T3": "🟢", "SKIP": "⬛", "TBD": "⚪"}
    for _, row in df_all.dropna(subset=["lat","lng"]).iterrows():
        tier   = row.knock_tier or "TBD"
        color  = TIER_COLOR.get(tier, "#999999")
        score  = int(row.motivation_score) if pd.notna(row.motivation_score) else 0
        signal = str(row.primary_signal or "")[:80]
        eq_str = f"${row.est_equity_usd:,.0f}" if pd.notna(row.est_equity_usd) and row.est_equity_usd else "—"

        popup_html = f"""
        <div style='font-family:Arial;font-size:12px;min-width:220px'>
          <b>{row.address}</b><br>
          <span style='color:{color};font-weight:bold'>{TIER_LABEL.get(tier,tier)}</span>
          &nbsp;&nbsp;<b>Score: {score}</b><br>
          <i>{signal}</i><br>
          Est. Equity: {eq_str}
          {'<br>Owner: ' + row.owner_name[:40] if row.owner_name else ''}
        </div>"""

        folium.CircleMarker(
            location=[row.lat, row.lng],
            radius=9 if tier == "T1" else 7,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=0.85,
            weight=2,
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=f"{row.address.split(',')[0]}  |  Score {score}  |  {tier}",
        ).add_to(m)

    # Legend
    legend_html = """
    <div style='position:fixed;bottom:30px;left:30px;z-index:1000;background:white;
         padding:10px;border-radius:8px;border:1px solid #ccc;font-family:Arial;font-size:12px'>
      <b>Knock Priority</b><br>
      <span style='color:#C00000'>●</span> T1 — Knock now<br>
      <span style='color:#D6A800'>●</span> T2 — Knock next<br>
      <span style='color:#375623'>●</span> T3 — Cold knock<br>
      <span style='color:#888888'>●</span> Skip<br>
    </div>"""
    m.get_root().html.add_child(folium.Element(legend_html))

    map_result = st_folium(m, width=None, height=520, returned_objects=["all_drawings","last_clicked"])

with detail_col:
    st.subheader("Region Stats")

    # Determine active set (polygon or all)
    active_polygon = None
    if map_result and map_result.get("all_drawings"):
        drawings = map_result["all_drawings"].get("features", [])
        if drawings:
            active_polygon = drawings[-1]["geometry"]

    if active_polygon and not show_all:
        df_region = filter_by_polygon(df_all, active_polygon)
        st.caption(f"**{len(df_region)}** properties in drawn region")
    else:
        df_region = df_all
        st.caption(f"**{len(df_region)}** total properties")

    # Tier breakdown
    tier_counts = df_region.groupby("knock_tier").size().reindex(
        ["T1","T2","T3","SKIP","TBD"], fill_value=0
    )
    for tier, count in tier_counts.items():
        if count:
            st.metric(TIER_LABEL.get(tier, tier), count)

    st.divider()
    if df_region.motivation_score.notna().any():
        st.metric("Avg Score", f"{df_region.motivation_score.mean():.0f}")
        st.metric("Top Score", f"{df_region.motivation_score.max():.0f}")

# ── RANKED TABLE ──────────────────────────────────────────────────────────────
st.divider()
st.subheader("📋 Ranked Door-Knock List")

# Apply sidebar filters
df_show = df_region.copy()
if tier_filter:
    df_show = df_show[df_show.knock_tier.isin(tier_filter)]
df_show = df_show[df_show.motivation_score.fillna(0) >= min_score]

# Format columns for display
def fmt_equity(row):
    if pd.notna(row.est_equity_usd) and row.est_equity_usd:
        pct = f" ({row.equity_pct:.0%})" if pd.notna(row.equity_pct) else ""
        return f"${row.est_equity_usd:,.0f}{pct}"
    return "—"

def fmt_piti(val):
    return f"${val:,.0f}/mo" if pd.notna(val) and val else "—"

display = df_show[[
    "knock_tier","motivation_score","address","owner_name",
    "years_owned","emv","est_equity_usd","equity_pct","monthly_piti",
    "primary_signal"
]].copy()

display.columns = [
    "Tier","Score","Address","Owner",
    "Yrs Owned","EMV","Est Equity $","Equity %","Mo. PITI",
    "Primary Signal"
]
display["Yrs Owned"] = display["Yrs Owned"].apply(lambda x: f"{x:.0f}" if pd.notna(x) else "—")
display["EMV"]       = display["EMV"].apply(lambda x: f"${x:,.0f}" if pd.notna(x) else "—")
display["Est Equity $"] = display.apply(fmt_equity, axis=1)
display["Equity %"]  = display["Equity %"].apply(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
display["Mo. PITI"]  = display["Mo. PITI"].apply(fmt_piti)
display["Score"]     = display["Score"].fillna(0).astype(int)

# Color rows by tier
def row_color(row):
    colors = {"T1": "background-color:#fff0f0", "T2": "background-color:#fffde7",
              "T3": "background-color:#f0fff0", "SKIP": "background-color:#f5f5f5"}
    c = colors.get(row["Tier"], "")
    return [c] * len(row)

st.dataframe(
    display.style.apply(row_color, axis=1),
    use_container_width=True,
    hide_index=True,
    height=min(40 * len(display) + 50, 600),
)

# ── property detail on click ──────────────────────────────────────────────────
if map_result and map_result.get("last_clicked"):
    click_lat = map_result["last_clicked"]["lat"]
    click_lng = map_result["last_clicked"]["lng"]
    # Find nearest property
    df_geo = df_all.dropna(subset=["lat","lng"])
    if not df_geo.empty:
        df_geo = df_geo.copy()
        df_geo["dist"] = ((df_geo.lat - click_lat)**2 + (df_geo.lng - click_lng)**2)**0.5
        nearest = df_geo.loc[df_geo.dist.idxmin()]
        if nearest.dist < 0.001:  # ~100m threshold
            with st.expander(f"📍 {nearest.address}", expanded=True):
                c1, c2, c3 = st.columns(3)
                score = int(nearest.motivation_score) if pd.notna(nearest.motivation_score) else 0
                tier  = nearest.knock_tier or "TBD"
                c1.metric("Motivation Score", score)
                c2.metric("Knock Tier", tier)
                c3.metric("Years Owned", f"{nearest.years_owned:.0f}" if pd.notna(nearest.years_owned) else "—")

                st.markdown(f"**Owner:** {nearest.owner_name}")
                st.markdown(f"**Signal:** {nearest.primary_signal or '—'}")

                col_a, col_b, col_c = st.columns(3)
                col_a.metric("EMV", f"${nearest.emv:,.0f}" if pd.notna(nearest.emv) else "—")
                if pd.notna(nearest.est_equity_usd):
                    col_b.metric("Est. Equity", f"${nearest.est_equity_usd:,.0f}")
                if pd.notna(nearest.monthly_piti):
                    col_c.metric("Mo. PITI", f"${nearest.monthly_piti:,.0f}")

                if nearest.score_factors:
                    st.markdown("**Score Breakdown:**")
                    factors = json.loads(nearest.score_factors) if isinstance(nearest.score_factors, str) else nearest.score_factors
                    for k, v in sorted(factors.items(), key=lambda x: -x[1]):
                        st.markdown(f"- `{k.replace('_',' ')}` → **+{v} pts**")

                # Feedback
                st.divider()
                fb = st.selectbox("Log outcome", ["—","Good lead","Bad lead","Spoke — interested",
                                                   "Spoke — not interested","Follow up later","Sold"],
                                  key=f"fb_{nearest.id}")
                if fb != "—":
                    con = get_connection()
                    con.execute("""
                        INSERT OR IGNORE INTO human_feedback (id, outcome)
                        VALUES (?,?)
                    """, [nearest.id, fb])
                    st.success(f"Logged: {fb}")

st.caption("v0.1 · Lakes of Radisson, Blaine MN 55449 · 52 seed properties")
