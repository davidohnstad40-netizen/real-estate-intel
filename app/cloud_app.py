"""
cloud_app.py -- Read-only Streamlit app for realtor sharing.
Reads from data/snapshot/*.parquet -- no DuckDB required.

Deploy: Streamlit Community Cloud
    Main file: app/cloud_app.py
    Requirements: requirements_cloud.txt

Local preview:
    streamlit run app/cloud_app.py --server.port 8504
"""
import sys, os, json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import streamlit as st
import pandas as pd
import folium
from folium.plugins import MarkerCluster
from streamlit_folium import st_folium

# ?? page config ????????????????????????????????????????????????????????????????
st.set_page_config(
    page_title="REI Targets",
    layout="wide",
    initial_sidebar_state="expanded",
)

# ?? password gate ??????????????????????????????????????????????????????????????
_pw = os.getenv("SHARE_PASSWORD", "")
if _pw:
    entered = st.sidebar.text_input("Access code", type="password", key="pw")
    if entered != _pw:
        st.sidebar.error("Enter access code to view")
        st.stop()

# ?? light CSS ?????????????????????????????????????????????????????????????????
st.markdown("""<style>
  .block-container{padding-top:1rem}
  div[data-testid="metric-container"]{background:#f8f8f8;border-radius:8px;padding:8px}
  .stTabs [data-baseweb="tab-list"]{gap:4px}
  .stTabs [data-baseweb="tab"]{padding:8px 16px;border-radius:6px 6px 0 0}
</style>""", unsafe_allow_html=True)


# ?? snapshot loader ????????????????????????????????????????????????????????????
@st.cache_data(ttl=300)
def load_snapshot():
    """
    Load properties + scores from Parquet snapshot.
    Searches for snapshot relative to this file first, then cwd.
    Returns (df_merged, snapshot_date_str).
    """
    candidates = [
        os.path.join(os.path.dirname(__file__), "..", "data", "snapshot"),
        os.path.join(os.getcwd(), "data", "snapshot"),
        "data/snapshot",
    ]

    snap_dir = None
    for c in candidates:
        if os.path.isdir(c):
            snap_dir = os.path.abspath(c)
            break

    if snap_dir is None:
        return pd.DataFrame(), "Unknown"

    # Read metadata for snapshot date
    snap_date = "Unknown"
    meta_path = os.path.join(snap_dir, "snapshot_meta.json")
    if os.path.exists(meta_path):
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            raw_ts = meta.get("generated_at", "")
            if raw_ts:
                # Trim to date portion for display
                snap_date = raw_ts[:10]
        except Exception:
            pass

    # Load properties
    prop_path = os.path.join(snap_dir, "properties.parquet")
    if not os.path.exists(prop_path):
        return pd.DataFrame(), snap_date

    df_props = pd.read_parquet(prop_path, engine="pyarrow")

    # Load scores
    score_path = os.path.join(snap_dir, "scores.parquet")
    if os.path.exists(score_path):
        df_scores = pd.read_parquet(score_path, engine="pyarrow")
        df = df_props.merge(df_scores, on="id", how="left")
    else:
        df = df_props.copy()
        for col in ["motivation_score", "knock_tier", "primary_signal",
                    "est_equity_usd", "equity_pct", "monthly_piti", "score_factors"]:
            if col not in df.columns:
                df[col] = None

    # Fill missing tier
    if "knock_tier" in df.columns:
        df["knock_tier"] = df["knock_tier"].fillna("TBD")
    else:
        df["knock_tier"] = "TBD"

    if "motivation_score" in df.columns:
        df["motivation_score"] = pd.to_numeric(df["motivation_score"], errors="coerce").fillna(0)
    else:
        df["motivation_score"] = 0

    # Sort by score descending
    df = df.sort_values("motivation_score", ascending=False).reset_index(drop=True)
    return df, snap_date


# ?? helpers ???????????????????????????????????????????????????????????????????
TIER_COLOR = {"T1": "#C00000", "T2": "#D6A800", "T3": "#375623",
              "LISTED": "#0078D4", "SKIP": "#888888", "TBD": "#aaaaaa"}
TIER_LABEL = {"T1": "T1 -- Knock First", "T2": "T2 -- Knock Next",
              "T3": "T3 -- Cold Knock", "LISTED": "Listed",
              "SKIP": "Skip", "TBD": "TBD"}


def tier_badge(tier):
    c = TIER_COLOR.get(tier, "#aaa")
    l = TIER_LABEL.get(tier, tier)
    return (
        f'<span style="background:{c};color:white;border-radius:4px;'
        f'padding:2px 8px;font-size:12px;font-weight:bold">{l}</span>'
    )


def fmt_money(v, dash="--"):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return dash
        return f"${float(v):,.0f}"
    except Exception:
        return dash


def fmt_pct(v, dash="--"):
    try:
        if v is None or (isinstance(v, float) and pd.isna(v)):
            return dash
        return f"{float(v):.0%}"
    except Exception:
        return dash


# ?? session state init ????????????????????????????????????????????????????????
if "selected_id" not in st.session_state:
    st.session_state.selected_id = None
if "cloud_page" not in st.session_state:
    st.session_state.cloud_page = 0

PAGE_SIZE = 25

# ?? load data ?????????????????????????????????????????????????????????????????
df_all, snap_date = load_snapshot()

# ?? sidebar filters ???????????????????????????????????????????????????????????
with st.sidebar:
    st.markdown("## REI Platform")
    st.caption("Lakes of Radisson -- Blaine MN")
    st.caption(f"Last updated: **{snap_date}**")
    st.divider()

    st.subheader("Filters")

    all_tiers = ["T1", "T2", "T3", "LISTED", "SKIP", "TBD"]
    sel_tiers = st.multiselect(
        "Tier",
        all_tiers,
        default=["T1", "T2", "T3", "TBD"],
    )

    min_score = st.slider("Min Motivation Score", 0, 100, 0)

    st.divider()
    if st.button("Clear selection"):
        st.session_state.selected_id = None
        st.rerun()

# ?? apply filters ?????????????????????????????????????????????????????????????
if df_all.empty:
    st.error(
        "No snapshot data found. Run `python -m ingestion.export_snapshot` "
        "locally first, then deploy the data/snapshot/ folder."
    )
    st.stop()

df_filtered = df_all.copy()
if sel_tiers:
    df_filtered = df_filtered[df_filtered["knock_tier"].isin(sel_tiers)]
df_filtered = df_filtered[df_filtered["motivation_score"] >= min_score]
df_filtered = df_filtered.reset_index(drop=True)

# ?? header ????????????????????????????????????????????????????????????????????
st.markdown(f"# REI Target List")
st.caption(f"Last updated: {snap_date} -- Read-only view")

tier_c = df_filtered["knock_tier"].value_counts()
m1, m2, m3, m4, m5 = st.columns(5)
m1.metric("Showing", len(df_filtered))
m2.metric("T1", int(tier_c.get("T1", 0)))
m3.metric("T2", int(tier_c.get("T2", 0)))
m4.metric("T3", int(tier_c.get("T3", 0)))
m5.metric("Avg Score",
          f"{df_filtered['motivation_score'].mean():.0f}" if not df_filtered.empty else "--")

# ?? tabs ??????????????????????????????????????????????????????????????????????
tab_map, tab_list, tab_detail = st.tabs(["Map", "Ranked List", "Property Detail"])


# ??????????????????????????????????????????????????????????????????????????????
# TAB 1 -- MAP
# ??????????????????????????????????????????????????????????????????????????????
with tab_map:
    has_coords = df_all.dropna(subset=["lat", "lng"])
    clat = float(has_coords["lat"].mean()) if not has_coords.empty else 45.184
    clng = float(has_coords["lng"].mean()) if not has_coords.empty else -93.186

    m = folium.Map(location=[clat, clng], zoom_start=15, tiles="CartoDB positron")

    # MarkerCluster for performance with large datasets
    cluster = MarkerCluster(
        options={
            "maxClusterRadius": 40,
            "disableClusteringAtZoom": 17,
        }
    ).add_to(m)

    active_ids = set(df_filtered["id"].values) if "id" in df_filtered.columns else set()

    for _, row in has_coords.iterrows():
        tier = str(row.get("knock_tier") or "TBD")
        color = TIER_COLOR.get(tier, "#999")
        score = int(row["motivation_score"]) if pd.notna(row["motivation_score"]) else 0
        eq = fmt_money(row.get("est_equity_usd"))
        in_filter = row.get("id", "") in active_ids
        opacity = 0.9 if in_filter else 0.25

        addr_short = str(row.get("address", "")).split(",")[0]
        owner = str(row.get("owner_name") or "--")[:30]
        signal = str(row.get("primary_signal") or "")[:70]

        popup_html = (
            f"<div style='font-family:Arial;font-size:12px;min-width:220px'>"
            f"<b>{addr_short}</b><br>"
            f"<b style='color:{color}'>{TIER_LABEL.get(tier, tier)}</b>"
            f" &nbsp; Score: <b>{score}</b><br>"
            f"<i style='font-size:11px'>{signal}</i><br>"
            f"Est. Equity: {eq} &nbsp;|&nbsp; Owner: {owner}"
            f"</div>"
        )

        marker = folium.CircleMarker(
            location=[float(row["lat"]), float(row["lng"])],
            radius=9 if tier == "T1" else 7,
            color=color,
            fill=True,
            fill_color=color,
            fill_opacity=opacity,
            weight=2 if in_filter else 1,
            popup=folium.Popup(popup_html, max_width=260),
            tooltip=f"{addr_short} | Score {score} | {tier}",
        )
        marker.add_to(cluster)

    # Legend
    m.get_root().html.add_child(folium.Element(
        "<div style='position:fixed;bottom:30px;left:30px;z-index:9999;"
        "background:white;padding:10px 14px;border-radius:8px;"
        "border:1px solid #ddd;font-family:Arial;font-size:12px;"
        "box-shadow:2px 2px 6px rgba(0,0,0,0.15)'>"
        "<b>Knock Priority</b><br>"
        "<span style='color:#C00000'>&#9679;</span> T1 -- Knock first<br>"
        "<span style='color:#D6A800'>&#9679;</span> T2 -- Knock next<br>"
        "<span style='color:#375623'>&#9679;</span> T3 -- Cold knock<br>"
        "<span style='color:#888'>&#9679;</span> Skip / filtered out"
        "</div>"
    ))

    map_out = st_folium(m, width=None, height=560, returned_objects=["last_clicked"])

    # Capture click to select property
    if map_out and map_out.get("last_clicked"):
        clk = map_out["last_clicked"]
        geo_df = df_all.dropna(subset=["lat", "lng"]).copy()
        if not geo_df.empty:
            geo_df["_dist"] = (
                (geo_df["lat"] - clk["lat"]) ** 2
                + (geo_df["lng"] - clk["lng"]) ** 2
            ) ** 0.5
            nearest = geo_df.loc[geo_df["_dist"].idxmin()]
            if nearest["_dist"] < 0.001:
                st.session_state.selected_id = nearest["id"]
                st.info(
                    f"Selected: **{str(nearest['address']).split(',')[0]}** "
                    "-- switch to Property Detail tab."
                )


# ??????????????????????????????????????????????????????????????????????????????
# TAB 2 -- RANKED LIST (paginated, 25 rows per page)
# ??????????????????????????????????????????????????????????????????????????????
with tab_list:
    total_rows = len(df_filtered)
    total_pages = max(1, (total_rows + PAGE_SIZE - 1) // PAGE_SIZE)

    # Clamp page index
    if st.session_state.cloud_page >= total_pages:
        st.session_state.cloud_page = total_pages - 1
    if st.session_state.cloud_page < 0:
        st.session_state.cloud_page = 0

    cur_page = st.session_state.cloud_page
    start_idx = cur_page * PAGE_SIZE
    end_idx = min(start_idx + PAGE_SIZE, total_rows)
    df_page = df_filtered.iloc[start_idx:end_idx]

    st.subheader(f"Ranked Door-Knock List -- {total_rows} properties")
    st.caption(
        f"Page {cur_page + 1} of {total_pages} "
        f"(rows {start_idx + 1}--{end_idx})"
    )

    # Prev / Next buttons
    col_prev, col_info, col_next = st.columns([1, 3, 1])
    with col_prev:
        if st.button("Prev", disabled=(cur_page == 0), use_container_width=True):
            st.session_state.cloud_page -= 1
            st.rerun()
    with col_info:
        st.markdown(
            f"<div style='text-align:center;padding-top:6px;color:#666'>"
            f"Page {cur_page + 1} / {total_pages}</div>",
            unsafe_allow_html=True,
        )
    with col_next:
        if st.button("Next", disabled=(cur_page >= total_pages - 1), use_container_width=True):
            st.session_state.cloud_page += 1
            st.rerun()

    # Build display dataframe
    cols_available = df_page.columns.tolist()

    def safe_col(col, default=None):
        return df_page[col] if col in cols_available else default

    disp = pd.DataFrame()
    disp["Tier"]   = safe_col("knock_tier", "TBD")
    disp["Score"]  = safe_col("motivation_score", 0).fillna(0).astype(int)
    disp["Address"] = safe_col("address", "--").apply(
        lambda x: str(x).split(",")[0] if x else "--"
    )
    disp["Owner"]  = safe_col("owner_name", "--").fillna("--").apply(
        lambda x: str(x)[:28]
    )

    yrs = safe_col("years_owned")
    if yrs is not None:
        disp["Yrs Owned"] = yrs.apply(
            lambda x: f"{x:.0f}" if pd.notna(x) else "--"
        )
    else:
        disp["Yrs Owned"] = "--"

    disp["EMV"]       = safe_col("emv").apply(fmt_money) if safe_col("emv") is not None else "--"
    disp["Equity $"]  = safe_col("est_equity_usd").apply(fmt_money) if safe_col("est_equity_usd") is not None else "--"
    disp["Equity %"]  = safe_col("equity_pct").apply(fmt_pct) if safe_col("equity_pct") is not None else "--"
    disp["PITI/mo"]   = safe_col("monthly_piti").apply(fmt_money) if safe_col("monthly_piti") is not None else "--"
    disp["Signal"]    = safe_col("primary_signal", "").fillna("").apply(
        lambda x: str(x)[:60]
    )

    # Row colors
    def row_bg(row):
        c = {
            "T1": "#fff0f0", "T2": "#fffde7", "T3": "#f0fff0",
            "LISTED": "#e3f2fd", "SKIP": "#f5f5f5",
        }.get(row["Tier"], "")
        return [f"background-color:{c}"] * len(row)

    event = st.dataframe(
        disp.style.apply(row_bg, axis=1),
        use_container_width=True,
        hide_index=True,
        height=min(42 * len(disp) + 55, 700),
        on_select="rerun",
        selection_mode="single-row",
    )

    if event and event.selection and event.selection.rows:
        sel_idx = event.selection.rows[0]
        sel_row = df_page.iloc[sel_idx]
        st.session_state.selected_id = sel_row["id"]
        st.info(
            f"Selected: **{str(sel_row.get('address','')).split(',')[0]}** "
            "-- switch to Property Detail tab."
        )

    # Bottom pagination repeat
    col_p2, _, col_n2 = st.columns([1, 3, 1])
    with col_p2:
        if st.button("Prev ", disabled=(cur_page == 0), use_container_width=True):
            st.session_state.cloud_page -= 1
            st.rerun()
    with col_n2:
        if st.button("Next ", disabled=(cur_page >= total_pages - 1), use_container_width=True):
            st.session_state.cloud_page += 1
            st.rerun()


# ??????????????????????????????????????????????????????????????????????????????
# TAB 3 -- PROPERTY DETAIL
# ??????????????????????????????????????????????????????????????????????????????
with tab_detail:
    sel_id = st.session_state.selected_id

    if not sel_id:
        st.info(
            "Click a marker on the Map tab or select a row in the Ranked List "
            "to view property details here."
        )
    else:
        rows = df_all[df_all["id"] == sel_id]
        if rows.empty:
            st.warning("Property not found in snapshot.")
        else:
            row = rows.iloc[0]
            tier  = str(row.get("knock_tier") or "TBD")
            score = int(row["motivation_score"]) if pd.notna(row["motivation_score"]) else 0
            addr_short = str(row.get("address", "")).split(",")[0]

            st.markdown(f"## {addr_short}")
            st.markdown(
                tier_badge(tier) + f"&nbsp;&nbsp;<b>Motivation Score: {score}/100</b>",
                unsafe_allow_html=True,
            )

            c1, c2, c3, c4 = st.columns(4)
            c1.metric("Est. Value",  fmt_money(row.get("est_value")))
            c2.metric("Est. Equity", fmt_money(row.get("est_equity_usd")))
            c3.metric("Equity %",    fmt_pct(row.get("equity_pct")))
            c4.metric("Mo. PITI",    fmt_money(row.get("monthly_piti")))

            st.divider()
            col_left, col_right = st.columns(2)

            with col_left:
                st.markdown("**Property Details**")
                st.markdown(f"**Owner:** {row.get('owner_name') or '--'}")
                yrs_owned = row.get("years_owned")
                st.markdown(
                    f"**Years Owned:** {float(yrs_owned):.0f}"
                    if pd.notna(yrs_owned) else "**Years Owned:** --"
                )
                beds  = row.get("beds")
                baths = row.get("baths")
                sqft  = row.get("sqft")
                yr_b  = row.get("year_built")
                st.markdown(
                    f"**Beds/Baths:** "
                    f"{int(beds) if pd.notna(beds) else '--'}bd / "
                    f"{float(baths) if pd.notna(baths) else '--'}ba"
                )
                st.markdown(
                    f"**Sqft:** {int(sqft):,}" if pd.notna(sqft) else "**Sqft:** --"
                )
                st.markdown(
                    f"**Year Built:** {int(yr_b)}" if pd.notna(yr_b) else "**Year Built:** --"
                )
                emv = row.get("emv")
                st.markdown(
                    f"**County EMV:** {fmt_money(emv)}"
                )

            with col_right:
                st.markdown("**Purchase History**")
                psp  = row.get("prior_sale_price")
                psy  = row.get("prior_sale_year")
                st.markdown(
                    f"**Purchased:** {fmt_money(psp)} "
                    f"({int(psy) if pd.notna(psy) else '--'})"
                )
                st.markdown(f"**Homestead:** {row.get('homestead') or '--'}")
                st.markdown(f"**Owner Type:** {row.get('owner_type') or '--'}")
                pin = row.get("anoka_pin")
                if pin and str(pin).strip() not in ("", "nan", "None"):
                    st.markdown(f"**Anoka PIN:** `{pin}`")

            # Primary signal
            st.divider()
            primary_signal = row.get("primary_signal") or ""
            if primary_signal:
                st.markdown(f"**Primary Signal:** {primary_signal}")

            # Score breakdown
            st.subheader("Score Breakdown")
            raw_factors = row.get("score_factors")
            factors = {}
            if raw_factors:
                if isinstance(raw_factors, str):
                    try:
                        factors = json.loads(raw_factors)
                    except Exception:
                        factors = {}
                elif isinstance(raw_factors, dict):
                    factors = raw_factors

            if factors:
                for k, v in sorted(factors.items(), key=lambda x: -x[1]):
                    label = k.replace("_", " ").title()
                    pct   = min(max(float(v) / 100, 0), 1)
                    st.progress(pct, text=f"{label}: **+{v} pts**")
            else:
                st.caption("No score breakdown available.")

            # Mini map for this property
            if pd.notna(row.get("lat")) and pd.notna(row.get("lng")):
                st.divider()
                st.subheader("Location")
                mini_m = folium.Map(
                    location=[float(row["lat"]), float(row["lng"])],
                    zoom_start=17,
                    tiles="CartoDB positron",
                )
                folium.Marker(
                    location=[float(row["lat"]), float(row["lng"])],
                    popup=addr_short,
                    icon=folium.Icon(color="red", icon="home", prefix="fa"),
                ).add_to(mini_m)
                st_folium(mini_m, width=None, height=260, returned_objects=[])


# ?? footer ????????????????????????????????????????????????????????????????????
st.divider()
st.caption(
    f"Powered by Lakes of Radisson Intelligence Platform | "
    f"Last updated: {snap_date} | Read-only shared view"
)
