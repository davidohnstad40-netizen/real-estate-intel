"""
Real Estate Seller Intelligence Platform — v0.2
Draw a region → discover + score every property → AI-powered knock list.
"""
import sys, os, json
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
import pandas as pd
import folium
from folium.plugins import Draw
from streamlit_folium import st_folium
from shapely.geometry import Point, shape
from dotenv import load_dotenv

load_dotenv()
from db.schema import get_db
from scoring.motivation import TIER_COLOR, TIER_LABEL, PropertyInput, score as compute_score

# ── page config ───────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="REI — Seller Intelligence",
    page_icon="🏘️",
    layout="wide",
    initial_sidebar_state="expanded",
)
st.markdown("""<style>
  .block-container{padding-top:1rem}
  div[data-testid="metric-container"]{background:#f8f8f8;border-radius:8px;padding:8px}
  .stTabs [data-baseweb="tab-list"]{gap:4px}
  .stTabs [data-baseweb="tab"]{padding:8px 16px;border-radius:6px 6px 0 0}
</style>""", unsafe_allow_html=True)

# ── shared state ──────────────────────────────────────────────────────────────
if "selected_id"   not in st.session_state: st.session_state.selected_id = None
if "polygon"       not in st.session_state: st.session_state.polygon     = None
if "ai_theses"     not in st.session_state: st.session_state.ai_theses   = {}
if "region_summary" not in st.session_state: st.session_state.region_summary = None

# ── db connection ─────────────────────────────────────────────────────────────
@st.cache_resource
def get_con():
    return get_db()

@st.cache_data(ttl=30)
def load_properties():
    con = get_con()
    return con.execute("""
        SELECT p.id, p.address, p.lat, p.lng, p.owner_name,
               p.beds, p.baths, p.sqft, p.year_built,
               p.emv, p.est_value, p.prior_sale_price, p.prior_sale_year,
               p.years_owned, p.homestead, p.owner_type, p.anoka_pin,
               s.motivation_score, s.knock_tier, s.primary_signal,
               s.est_equity_usd, s.equity_pct, s.monthly_piti, s.score_factors
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        ORDER BY s.motivation_score DESC NULLS LAST
    """).df()

def filter_by_polygon(df, geojson):
    if not geojson or df.empty: return df
    try:
        poly = shape(geojson)
        mask = df.apply(
            lambda r: poly.contains(Point(r.lng, r.lat))
            if pd.notna(r.lat) and pd.notna(r.lng) else False, axis=1)
        return df[mask]
    except: return df

def tier_badge(tier):
    colors = {"T1":"#C00000","T2":"#D6A800","T3":"#375623","SKIP":"#888","TBD":"#aaa"}
    labels = {"T1":"T1 — KNOCK","T2":"T2 — KNOCK","T3":"T3","SKIP":"SKIP","TBD":"TBD"}
    c = colors.get(tier,"#aaa"); l = labels.get(tier, tier)
    return f'<span style="background:{c};color:white;border-radius:4px;padding:2px 8px;font-size:12px;font-weight:bold">{l}</span>'

def fmt_money(v, dash="—"):
    return f"${v:,.0f}" if pd.notna(v) and v else dash

# ── sidebar ───────────────────────────────────────────────────────────────────
with st.sidebar:
    st.image("https://upload.wikimedia.org/wikipedia/commons/thumb/b/b9/Map_marker.svg/60px-Map_marker.svg.png", width=40)
    st.title("REI Platform")
    st.caption("Lakes of Radisson · Blaine MN")
    st.divider()

    st.subheader("🎯 Buy Box")
    bb_min_beds   = st.slider("Min Beds",   2, 6, 3)
    bb_max_price  = st.number_input("Max Price ($)", value=1_200_000, step=50_000)
    bb_min_equity = st.slider("Min Equity %", 0, 80, 0)
    bb_tiers      = st.multiselect("Tiers to show", ["T1","T2","T3","SKIP","TBD"],
                                   default=["T1","T2","T3","TBD"])
    bb_min_score  = st.slider("Min Motivation Score", 0, 100, 0)
    st.divider()

    polygon_active = st.checkbox("Filter by drawn polygon", value=False)
    if st.button("🔄 Refresh data"):
        st.cache_data.clear()
        st.rerun()

# ── load + filter ─────────────────────────────────────────────────────────────
df_all = load_properties()

# Apply buy box
df_filtered = df_all.copy()
if bb_tiers:
    df_filtered = df_filtered[df_filtered.knock_tier.fillna("TBD").isin(bb_tiers)]
df_filtered = df_filtered[df_filtered.motivation_score.fillna(0) >= bb_min_score]
if bb_min_equity > 0:
    df_filtered = df_filtered[df_filtered.equity_pct.fillna(0) >= bb_min_equity/100]
if bb_max_price < 9_000_000:
    df_filtered = df_filtered[df_filtered.est_value.fillna(0) <= bb_max_price]
if bb_min_beds > 2:
    df_filtered = df_filtered[df_filtered.beds.fillna(0) >= bb_min_beds]

# Apply polygon
if polygon_active and st.session_state.polygon:
    df_active = filter_by_polygon(df_filtered, st.session_state.polygon)
else:
    df_active = df_filtered

# ── header metrics ────────────────────────────────────────────────────────────
st.title("🏘️ Real Estate Seller Intelligence")
m1, m2, m3, m4, m5 = st.columns(5)
tier_c = df_active.knock_tier.fillna("TBD").value_counts()
m1.metric("Total", len(df_active))
m2.metric("🔴 T1", tier_c.get("T1",0))
m3.metric("🟡 T2", tier_c.get("T2",0))
m4.metric("🟢 T3", tier_c.get("T3",0))
m5.metric("Avg Score", f"{df_active.motivation_score.mean():.0f}" if not df_active.empty else "—")

# ── tabs ──────────────────────────────────────────────────────────────────────
tab_map, tab_list, tab_detail, tab_ai, tab_data = st.tabs([
    "🗺️ Map", "📋 Ranked List", "📍 Property Detail", "🤖 AI Analysis", "🔧 Data"
])

# ══════════════════════════════════════════════════════════════════════════════
# TAB 1 — MAP
# ══════════════════════════════════════════════════════════════════════════════
with tab_map:
    has_coords = df_all.dropna(subset=["lat","lng"])
    clat = has_coords.lat.mean() if not has_coords.empty else 45.184
    clng = has_coords.lng.mean() if not has_coords.empty else -93.186

    m = folium.Map(location=[clat,clng], zoom_start=15, tiles="CartoDB positron")

    Draw(draw_options={
        "polygon":{"shapeOptions":{"color":"#0078D4","fillOpacity":0.12}},
        "rectangle":{"shapeOptions":{"color":"#0078D4","fillOpacity":0.12}},
        "polyline":False,"circle":False,"marker":False,"circlemarker":False,
    }, edit_options={"edit":False,"remove":True}).add_to(m)

    for _, row in df_all.dropna(subset=["lat","lng"]).iterrows():
        tier  = str(row.knock_tier or "TBD")
        color = TIER_COLOR.get(tier,"#999")
        score = int(row.motivation_score) if pd.notna(row.motivation_score) else 0
        eq    = fmt_money(row.est_equity_usd)
        in_bb = row.id in df_active.id.values

        popup_html = f"""
        <div style='font-family:Arial;font-size:12px;min-width:230px'>
          <b>{row.address.split(',')[0]}</b><br>
          <b style='color:{color}'>{TIER_LABEL.get(tier,tier)}</b> &nbsp; Score: <b>{score}</b><br>
          <i style='font-size:11px'>{(str(row.primary_signal or '')[:70])}</i><br>
          Est. Equity: {eq} &nbsp;|&nbsp; Owner: {str(row.owner_name or '')[:30]}
          {'<br><b style="color:#888">[Filtered out by buy box]</b>' if not in_bb else ''}
        </div>"""

        folium.CircleMarker(
            location=[row.lat, row.lng],
            radius=9 if tier=="T1" else 7,
            color=color, fill=True, fill_color=color,
            fill_opacity=0.9 if in_bb else 0.3,
            weight=2 if in_bb else 1,
            popup=folium.Popup(popup_html, max_width=270),
            tooltip=f"{row.address.split(',')[0]}  |  Score {score}  |  {tier}",
        ).add_to(m)

    # Legend
    m.get_root().html.add_child(folium.Element("""
    <div style='position:fixed;bottom:30px;left:30px;z-index:9999;background:white;
         padding:10px 14px;border-radius:8px;border:1px solid #ddd;font-family:Arial;font-size:12px;box-shadow:2px 2px 6px rgba(0,0,0,0.15)'>
      <b>Knock Priority</b><br>
      <span style='color:#C00000'>●</span> T1 — Knock first<br>
      <span style='color:#D6A800'>●</span> T2 — Knock next<br>
      <span style='color:#375623'>●</span> T3 — Cold knock<br>
      <span style='color:#888'>●</span> Skip<br>
      <span style='opacity:0.3'>●</span> Filtered out
    </div>"""))

    # Optional heatmap overlay
    show_heat = st.checkbox("🌡️ Score heatmap", value=False, key="show_heat")
    if show_heat:
        from folium.plugins import HeatMap
        heat_data = [
            [row.lat, row.lng, float(row.motivation_score or 0)]
            for _, row in df_all.dropna(subset=["lat","lng"]).iterrows()
            if pd.notna(row.motivation_score)
        ]
        if heat_data:
            HeatMap(heat_data, radius=20, blur=15, max_zoom=16,
                    gradient={0.2:"blue",0.5:"lime",0.8:"orange",1.0:"red"}).add_to(m)

    map_out = st_folium(m, width=None, height=560, returned_objects=["all_drawings","last_clicked"])

    # Capture polygon
    if map_out and map_out.get("all_drawings"):
        feats = map_out["all_drawings"].get("features",[])
        if feats:
            st.session_state.polygon = feats[-1]["geometry"]
            region_props = filter_by_polygon(df_all, st.session_state.polygon)
            st.success(f"Polygon captures **{len(region_props)}** properties — switch to Ranked List or AI Analysis tab.")

    # Capture click → select property
    if map_out and map_out.get("last_clicked"):
        clk = map_out["last_clicked"]
        geo_df = df_all.dropna(subset=["lat","lng"]).copy()
        if not geo_df.empty:
            geo_df["dist"] = ((geo_df.lat-clk["lat"])**2+(geo_df.lng-clk["lng"])**2)**0.5
            nearest = geo_df.loc[geo_df.dist.idxmin()]
            if nearest.dist < 0.001:
                st.session_state.selected_id = nearest.id
                st.info(f"Selected: **{nearest.address.split(',')[0]}** — switch to Property Detail tab.")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 2 — RANKED LIST
# ══════════════════════════════════════════════════════════════════════════════
with tab_list:
    st.subheader(f"Ranked Door-Knock List — {len(df_active)} properties")
    st.caption("Click any row to view in Property Detail tab.")

    disp = df_active[[
        "knock_tier","motivation_score","address","owner_name",
        "years_owned","emv","est_equity_usd","equity_pct","monthly_piti",
        "primary_signal"
    ]].copy()
    disp.columns = ["Tier","Score","Address","Owner","Yrs","EMV","Equity $","Equity %","PITI/mo","Primary Signal"]
    disp["Yrs"]       = disp["Yrs"].apply(lambda x: f"{x:.0f}" if pd.notna(x) else "—")
    disp["EMV"]       = disp["EMV"].apply(lambda x: fmt_money(x))
    disp["Equity $"]  = disp["Equity $"].apply(lambda x: fmt_money(x))
    disp["Equity %"]  = disp["Equity %"].apply(lambda x: f"{x:.0%}" if pd.notna(x) else "—")
    disp["PITI/mo"]   = disp["PITI/mo"].apply(lambda x: fmt_money(x))
    disp["Score"]     = disp["Score"].fillna(0).astype(int)

    def row_bg(row):
        c = {"T1":"#fff0f0","T2":"#fffde7","T3":"#f0fff0","SKIP":"#f5f5f5"}.get(row["Tier"],"")
        return [f"background-color:{c}"]*len(row)

    event = st.dataframe(
        disp.style.apply(row_bg, axis=1),
        use_container_width=True, hide_index=True,
        height=min(42*len(disp)+55, 650),
        on_select="rerun", selection_mode="single-row",
    )
    # Select row
    if event and event.selection and event.selection.rows:
        sel_idx = event.selection.rows[0]
        st.session_state.selected_id = df_active.iloc[sel_idx].id


# ══════════════════════════════════════════════════════════════════════════════
# TAB 3 — PROPERTY DETAIL
# ══════════════════════════════════════════════════════════════════════════════
with tab_detail:
    sel_id = st.session_state.selected_id
    if not sel_id:
        st.info("Click a marker on the map or select a row in the Ranked List to see property details.")
    else:
        row = df_all[df_all.id == sel_id]
        if row.empty:
            st.warning("Property not found.")
        else:
            row = row.iloc[0]
            tier  = str(row.knock_tier or "TBD")
            score = int(row.motivation_score) if pd.notna(row.motivation_score) else 0

            st.markdown(f"## {row.address.split(',')[0]}")
            st.markdown(tier_badge(tier) + f"&nbsp;&nbsp;<b>Motivation Score: {score}/100</b>",
                        unsafe_allow_html=True)

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Est. Value", fmt_money(row.est_value))
            c2.metric("Est. Equity", fmt_money(row.est_equity_usd))
            c3.metric("Equity %", f"{row.equity_pct:.0%}" if pd.notna(row.equity_pct) else "—")
            c4.metric("Mo. PITI",  fmt_money(row.monthly_piti))

            # Comp-based valuation (from parcels.duckdb if available)
            if pd.notna(row.get("lat") if hasattr(row,"get") else getattr(row,"lat",None)) and \
               pd.notna(row.get("lng") if hasattr(row,"get") else getattr(row,"lng",None)):
                try:
                    from ingestion.comp_sales import estimate_value
                    comp_r = estimate_value(row.lat, row.lng, row.sqft, row.emv)
                    if comp_r["comp_count"] > 0:
                        st.info(f"📊 Comp-based value: **${comp_r['est_value']:,.0f}** "
                                f"(${comp_r['median_ppsf']:.0f}/sqft · {comp_r['comp_count']} nearby sales)")
                except Exception:
                    pass

            st.markdown(f"**Owner:** {row.owner_name or '—'}")
            st.markdown(f"**Years Owned:** {row.years_owned:.0f}" if pd.notna(row.years_owned) else "**Years Owned:** —")
            st.markdown(f"**Signal:** {row.primary_signal or '—'}")

            # Contact info (skip trace results)
            try:
                contacts = get_con().execute(
                    "SELECT * FROM contact_info WHERE property_id = ?", [sel_id]
                ).df()
                if not contacts.empty:
                    ct = contacts.iloc[0]
                    st.divider()
                    st.subheader("📞 Contact Info")
                    src = ct.get("source","")
                    conf = ct.get("confidence","")
                    st.caption(f"Source: {src} · Confidence: {conf}")
                    phones = [ct.get(f"phone{i}") for i in range(1,4) if ct.get(f"phone{i}")]
                    emails = [ct.get(f"email{i}") for i in range(1,3) if ct.get(f"email{i}")]
                    if phones:
                        st.markdown("**Phone(s):**")
                        for p in phones: st.markdown(f"  📱 `{p}`")
                    if emails:
                        st.markdown("**Email(s):**")
                        for e in emails: st.markdown(f"  ✉️ `{e}`")
                    if ct.get("mailing_addr"):
                        st.markdown(f"**Mailing Address:** {ct['mailing_addr']}")
                    if ct.get("dob"):
                        st.markdown(f"**DOB:** {ct['dob']}")
                    if ct.get("relatives"):
                        st.markdown(f"**Relatives:** {ct['relatives']}")
                else:
                    st.divider()
                    st.caption("📞 No contact info yet — see Data tab to run skip trace.")
            except Exception:
                pass  # contact_info table may not exist yet

            # Score breakdown
            st.divider()
            st.subheader("Score Breakdown")
            factors = row.score_factors
            if factors:
                if isinstance(factors, str):
                    try: factors = json.loads(factors)
                    except: factors = {}
                for k,v in sorted(factors.items(), key=lambda x:-x[1]):
                    label = k.replace("_"," ").title()
                    pct   = v / 100
                    st.progress(pct, text=f"{label}: **+{v} pts**")
            else:
                st.caption("No scored factors found.")

            # Door-Knock Script
            st.divider()
            st.subheader("🚪 Door-Knock Script")
            script_key = f"script_{sel_id}"
            api_key = os.getenv("ANTHROPIC_API_KEY","")

            # Try cached script from file first
            if script_key not in st.session_state:
                try:
                    from agents.doorknock_script import get_script
                    cached = get_script(sel_id)
                    if cached: st.session_state[script_key] = cached
                except Exception: pass

            if script_key in st.session_state:
                sc = st.session_state[script_key]
                if "error" not in sc:
                    door_tab, letter_tab = st.tabs(["🗣️ Door Opener", "✉️ Leave-Behind Letter"])
                    with door_tab:
                        st.markdown(sc.get("door_script",""))
                    with letter_tab:
                        st.markdown(sc.get("letter",""))
                        st.caption("Replace [NAME] and [PHONE] before printing.")
                else:
                    st.error(sc["error"])
            else:
                if not api_key:
                    st.caption("Add ANTHROPIC_API_KEY to .env to generate door scripts.")
                elif st.button("Generate Door Script + Letter", key="gen_script"):
                    with st.spinner("Writing your door script..."):
                        try:
                            from agents.doorknock_script import generate_door_script
                            sc = generate_door_script(row.to_dict())
                            st.session_state[script_key] = sc
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error: {e}")

            # AI Seller Thesis
            st.divider()
            st.subheader("🤖 AI Seller Thesis")
            thesis_key = f"thesis_{sel_id}"

            if thesis_key in st.session_state.ai_theses:
                st.markdown(st.session_state.ai_theses[thesis_key])
            else:
                if st.button("Generate Seller Thesis", key="gen_thesis"):
                    api_key = os.getenv("ANTHROPIC_API_KEY","")
                    if not api_key:
                        st.error("Set ANTHROPIC_API_KEY in your .env file.")
                    else:
                        with st.spinner("Generating thesis..."):
                            try:
                                from agents.seller_thesis import generate_thesis, generate_offer_model
                                prop_dict = row.to_dict()
                                thesis = generate_thesis(prop_dict)
                                offers = generate_offer_model(prop_dict)
                                st.session_state.ai_theses[thesis_key] = thesis
                                st.session_state.ai_theses[f"offers_{sel_id}"] = offers
                                st.rerun()
                            except Exception as e:
                                st.error(f"AI error: {e}")

            if f"offers_{sel_id}" in st.session_state.ai_theses:
                st.subheader("💰 Offer Model")
                st.markdown(st.session_state.ai_theses[f"offers_{sel_id}"])

            # Score History Chart
            st.divider()
            st.subheader("📈 Score History")
            try:
                hist = get_con().execute("""
                    SELECT snapshot_date, motivation_score, knock_tier
                    FROM score_history WHERE id = ?
                    ORDER BY snapshot_date ASC
                """, [sel_id]).df()
                if not hist.empty:
                    hist["snapshot_date"] = pd.to_datetime(hist["snapshot_date"])
                    st.line_chart(hist.set_index("snapshot_date")["motivation_score"],
                                  height=180, use_container_width=True)
                    # Tier change log
                    tier_changes = hist[hist["knock_tier"] != hist["knock_tier"].shift()]
                    if len(tier_changes) > 1:
                        st.caption("**Tier changes:**")
                        for _, tc in tier_changes.iterrows():
                            st.caption(f"  {tc.snapshot_date.date()} → {tc.knock_tier}")
                else:
                    st.caption("No history yet — runs daily after first snapshot.")
            except Exception as e:
                st.caption(f"History unavailable: {e}")

            # Human Feedback
            st.divider()
            st.subheader("Log Outcome")
            fb_cols = st.columns([3,1])
            fb = fb_cols[0].selectbox("", ["—","Good lead","Bad lead","Knocked — interested",
                                           "Knocked — not interested","Follow up later","Sold","Left note"],
                                      key=f"fb_{sel_id}", label_visibility="collapsed")
            fb_note = fb_cols[1].text_input("Note", placeholder="optional", key=f"fbnote_{sel_id}",
                                            label_visibility="collapsed")
            if st.button("Save", key=f"save_fb_{sel_id}") and fb != "—":
                con = get_con()
                con.execute("INSERT OR IGNORE INTO human_feedback (id,outcome,notes) VALUES (?,?,?)",
                            [sel_id, fb, fb_note])
                st.success(f"✓ Logged: {fb}")


# ══════════════════════════════════════════════════════════════════════════════
# TAB 4 — AI ANALYSIS
# ══════════════════════════════════════════════════════════════════════════════
with tab_ai:
    st.subheader("🤖 AI Region Analysis")
    api_key = os.getenv("ANTHROPIC_API_KEY","")

    if not api_key:
        st.warning("Add `ANTHROPIC_API_KEY=your_key` to your `.env` file to enable AI features.")
    else:
        ai_scope = st.radio("Analyze", ["All properties in buy box", "Polygon region only"], horizontal=True)
        ai_df = df_active if ai_scope.startswith("All") else (
            filter_by_polygon(df_active, st.session_state.polygon)
            if st.session_state.polygon else df_active
        )

        st.caption(f"Will analyze **{len(ai_df)}** properties")

        col_sum, col_batch = st.columns(2)
        with col_sum:
            if st.button("📊 Generate Region Summary", use_container_width=True):
                with st.spinner("Analyzing region with Claude Sonnet..."):
                    try:
                        from agents.seller_thesis import generate_region_summary
                        props = ai_df.to_dict("records")
                        summary = generate_region_summary("Lakes of Radisson, Blaine MN", props)
                        st.session_state.region_summary = summary
                    except Exception as e:
                        st.error(f"Error: {e}")

        with col_batch:
            t1t2 = ai_df[ai_df.knock_tier.isin(["T1","T2"])]
            col_b1, col_b2 = st.columns(2)
            with col_b1:
                if st.button(f"🎯 Batch Theses ({len(t1t2)})", use_container_width=True):
                    from agents.seller_thesis import generate_thesis
                    prog = st.progress(0)
                    for i, (_, prow) in enumerate(t1t2.iterrows()):
                        key = f"thesis_{prow.id}"
                        if key not in st.session_state.ai_theses:
                            try:
                                st.session_state.ai_theses[key] = generate_thesis(prow.to_dict())
                            except Exception as e:
                                st.session_state.ai_theses[key] = f"Error: {e}"
                        prog.progress((i+1)/len(t1t2))
                    st.success(f"Done. View in Property Detail tab.")
            with col_b2:
                if st.button(f"🚪 Batch Door Scripts ({len(t1t2)})", use_container_width=True):
                    from agents.doorknock_script import generate_door_script
                    prog2 = st.progress(0)
                    for i, (_, prow) in enumerate(t1t2.iterrows()):
                        key = f"script_{prow.id}"
                        if key not in st.session_state:
                            try:
                                st.session_state[key] = generate_door_script(prow.to_dict())
                            except Exception as e:
                                st.session_state[key] = {"error": str(e)}
                        prog2.progress((i+1)/len(t1t2))
                    st.success(f"Scripts ready. View in Property Detail tab.")

        if st.session_state.region_summary:
            st.divider()
            st.markdown("### Region Intelligence Summary")
            st.markdown(st.session_state.region_summary)

        # Show all generated theses
        generated = {k:v for k,v in st.session_state.ai_theses.items() if k.startswith("thesis_")}
        if generated:
            st.divider()
            st.markdown(f"### Generated Theses ({len(generated)})")
            for key, thesis in generated.items():
                prop_id = key.replace("thesis_","")
                prop_row = df_all[df_all.id == prop_id]
                if not prop_row.empty:
                    addr = prop_row.iloc[0].address.split(",")[0]
                    tier = prop_row.iloc[0].knock_tier or "TBD"
                    with st.expander(f"{tier_badge(tier)} {addr}", expanded=False):
                        st.markdown(thesis, unsafe_allow_html=False)
                        if f"offers_{prop_id}" in st.session_state.ai_theses:
                            st.markdown("**Offer Model:**")
                            st.markdown(st.session_state.ai_theses[f"offers_{prop_id}"])


# ══════════════════════════════════════════════════════════════════════════════
# TAB 5 — DATA MANAGEMENT
# ══════════════════════════════════════════════════════════════════════════════
with tab_data:
    st.subheader("🔧 Data Sources & Ingestion")

    con = get_con()

    col_a, col_b = st.columns(2)
    with col_a:
        st.markdown("**Current Database**")
        prop_count = con.execute("SELECT COUNT(*) FROM properties").fetchone()[0]
        scored     = con.execute("SELECT COUNT(*) FROM property_scores WHERE motivation_score IS NOT NULL").fetchone()[0]
        signals    = con.execute("SELECT COUNT(*) FROM property_signals").fetchone()[0]
        fb_count   = con.execute("SELECT COUNT(*) FROM human_feedback").fetchone()[0]
        st.metric("Properties", prop_count)
        st.metric("Scored",     scored)
        st.metric("Signals",    signals)
        st.metric("Feedback logs", fb_count)

    with col_b:
        st.markdown("**Anoka County Parcels**")
        try:
            parcel_count = con.execute("SELECT COUNT(*) FROM parcels_raw").fetchone()[0]
            st.metric("Parcels loaded", parcel_count)
        except: st.metric("Parcels loaded", 0)

        if st.button("⬇️ Download + Load Anoka County Parcels", use_container_width=True):
            st.info("This downloads ~250 MB from MN Geospatial Commons. Run in a terminal for best experience:")
            st.code("python -m ingestion.anoka_county Blaine", language="bash")

        if st.button("🔄 Re-score all properties", use_container_width=True):
            with st.spinner("Re-scoring..."):
                props = con.execute("SELECT * FROM properties").df()
                for _, p in props.iterrows():
                    pi = PropertyInput(
                        address=p.address, owner_name=str(p.owner_name or ""),
                        emv=p.emv, prior_sale_price=p.prior_sale_price,
                        prior_sale_year=int(p.prior_sale_year) if pd.notna(p.prior_sale_year) else None,
                        years_owned=p.years_owned, homestead=str(p.homestead or ""),
                        owner_type=str(p.owner_type or ""),
                    )
                    r = compute_score(pi)
                    con.execute("""
                        UPDATE property_scores SET motivation_score=?, knock_tier=?,
                        primary_signal=?, score_factors=?, updated_at=current_timestamp
                        WHERE id=?
                    """, [r.total, r.tier, r.primary_signal, json.dumps(r.factors), p.id])
                st.cache_data.clear()
                st.success(f"Re-scored {len(props)} properties.")

    st.divider()
    st.markdown("**Feedback Log**")
    try:
        fb_df = con.execute("""
            SELECT h.id, p.address, h.outcome, h.notes, h.recorded_at
            FROM human_feedback h
            LEFT JOIN properties p ON h.id = p.id
            ORDER BY h.recorded_at DESC
            LIMIT 50
        """).df()
        if not fb_df.empty:
            st.dataframe(fb_df, use_container_width=True, hide_index=True)
        else:
            st.caption("No feedback logged yet.")
    except Exception as e:
        st.caption(f"Feedback table: {e}")

    st.divider()
    st.markdown("**📞 Skip Trace — Contact Info**")
    st.caption("Get phone numbers and emails for property owners.")

    try:
        ct_count = con.execute("SELECT COUNT(*) FROM contact_info").fetchone()[0]
        st.metric("Contacts in DB", ct_count)
    except Exception:
        ct_count = 0
        st.metric("Contacts in DB", 0)

    # Download the pre-generated upload CSV
    upload_csv_path = os.path.join(os.path.dirname(__file__), "..", "data", "skip_trace_upload.csv")
    if os.path.exists(upload_csv_path):
        with open(upload_csv_path, "rb") as f:
            st.download_button(
                "⬇️ Download Upload CSV (all 52 owners)",
                f, "skip_trace_upload.csv", "text/csv",
                help="Upload this to batchskiptracing.com — ~$0.18/record = ~$9.36 for all 52"
            )
        st.markdown("[→ Upload at batchskiptracing.com](https://www.batchskiptracing.com)", unsafe_allow_html=False)

    # Import results
    st.markdown("**Import BatchSkipTracing Results:**")
    uploaded = st.file_uploader("Upload results CSV from BatchSkipTracing", type=["csv"],
                                 key="skip_import")
    if uploaded and st.button("Import Contact Info", key="do_skip_import"):
        import tempfile, csv as csv_mod
        with tempfile.NamedTemporaryFile(suffix=".csv", delete=False, mode="wb") as tmp:
            tmp.write(uploaded.read())
            tmp_path = tmp.name
        try:
            from agents.skip_trace import import_batch_results
            n = import_batch_results(tmp_path)
            st.success(f"Imported {n} contact records.")
            st.cache_data.clear()
        except Exception as e:
            st.error(f"Import error: {e}")

    if ct_count > 0:
        st.markdown("**Current Contact Info:**")
        ct_df = con.execute("""
            SELECT c.property_id, p.address, c.phone1, c.phone2, c.email1,
                   c.mailing_addr, c.confidence, c.source
            FROM contact_info c LEFT JOIN properties p ON c.property_id = p.id
            ORDER BY p.address
        """).df()
        st.dataframe(ct_df, use_container_width=True, hide_index=True)

    st.divider()
    st.markdown("**Quick Export**")
    if st.button("📥 Export T1/T2 as CSV"):
        t1t2 = df_all[df_all.knock_tier.isin(["T1","T2"])]
        csv_data = t1t2.to_csv(index=False)
        st.download_button("Download CSV", csv_data, "t1_t2_targets.csv", "text/csv")

st.caption("v0.2 · Real Estate Seller Intelligence · Lakes of Radisson sample · github.com/davidohnstad40-netizen/real-estate-intel")
