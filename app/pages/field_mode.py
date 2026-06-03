"""
Field Mode — Mobile-optimized door-knock companion
===================================================
Stripped-down view for use on your phone while door knocking.
Shows: today's route, phone number, door script, quick log.

Access at: http://localhost:8504/field_mode
(or via Tailscale: http://mac-mini.tail.../field_mode)
"""
import sys, os, math, json, datetime
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

import streamlit as st
import pandas as pd
from db.schema import get_db

st.set_page_config(
    page_title="REI Field Mode",
    page_icon="🚪",
    layout="centered",
    initial_sidebar_state="collapsed",
)

# Minimal styling — large touch targets, high contrast
st.markdown("""<style>
  .block-container { padding: 0.5rem 1rem; max-width: 480px; margin: 0 auto; }
  button { min-height: 48px !important; font-size: 16px !important; }
  .stSelectbox > div { font-size: 16px; }
  .big-phone { font-size: 28px; font-weight: bold; color: #0078D4;
               background: #e8f4fd; border-radius: 8px;
               padding: 12px 16px; margin: 8px 0; text-align: center; }
  .tier-badge-T1 { background:#C00000;color:white;border-radius:6px;
                   padding:4px 12px;font-weight:bold;font-size:14px; }
  .tier-badge-T2 { background:#D6A800;color:white;border-radius:6px;
                   padding:4px 12px;font-weight:bold;font-size:14px; }
  .tier-badge-T3 { background:#375623;color:white;border-radius:6px;
                   padding:4px 12px;font-size:14px; }
</style>""", unsafe_allow_html=True)

# ── load data ─────────────────────────────────────────────────────────────────
@st.cache_data(ttl=60)
def load_t1t2():
    con = get_db()
    df = con.execute("""
        SELECT p.id, p.address, p.owner_name, p.lat, p.lng,
               s.motivation_score, s.knock_tier, s.primary_signal,
               s.score_factors,
               c.phone1, c.phone2, c.email1
        FROM properties p
        LEFT JOIN property_scores s ON p.id = s.id
        LEFT JOIN contact_info    c ON p.id = c.property_id
        WHERE s.knock_tier IN ('T1','T2')
          AND p.lat IS NOT NULL AND p.lng IS NOT NULL
        ORDER BY s.motivation_score DESC
    """).df()
    return df

def nearest_neighbor(df, start=(45.184, -93.186)):
    props = df.to_dict("records")
    remaining = [p for p in props if p.get("lat") and p.get("lng")]
    route, cur = [], start
    while remaining:
        n = min(remaining, key=lambda p: math.sqrt(
            (p["lat"]-cur[0])**2 + ((p["lng"]-cur[1])*math.cos(math.radians(cur[0])))**2
        ))
        route.append(n)
        remaining.remove(n)
        cur = (n["lat"], n["lng"])
    return route

def log_contact(property_id, outcome, notes, follow_up_date):
    con = get_db()
    con.execute("""
        CREATE TABLE IF NOT EXISTS contact_log (
            log_id VARCHAR PRIMARY KEY, property_id VARCHAR,
            contact_date DATE, method VARCHAR, outcome VARCHAR,
            notes TEXT, follow_up_date DATE,
            created_at TIMESTAMP DEFAULT current_timestamp)""")
    con.execute("""
        INSERT OR IGNORE INTO contact_log
        (log_id, property_id, contact_date, method, outcome, notes, follow_up_date)
        VALUES (?,?,?,?,?,?,?)
    """, [str(datetime.datetime.now().timestamp()), property_id,
          datetime.date.today(), "Door knock", outcome, notes,
          follow_up_date if follow_up_date else None])
    con.close()

# ── session state ─────────────────────────────────────────────────────────────
if "field_idx"   not in st.session_state: st.session_state.field_idx   = 0
if "logged_ids"  not in st.session_state: st.session_state.logged_ids  = set()
if "show_script" not in st.session_state: st.session_state.show_script = False

# ── header ────────────────────────────────────────────────────────────────────
col_title, col_mode = st.columns([3, 1])
col_title.markdown("## 🚪 Field Mode")
if col_mode.button("🔄"):
    st.cache_data.clear()
    st.session_state.field_idx = 0
    st.rerun()

df = load_t1t2()
if df.empty:
    st.warning("No T1/T2 properties with coordinates. Check the main app.")
    st.stop()

route = nearest_neighbor(df)
total = len(route)

# Progress bar
progress = len(st.session_state.logged_ids) / total if total else 0
st.progress(progress, text=f"{len(st.session_state.logged_ids)}/{total} knocked")

# Navigation
nav_l, nav_c, nav_r = st.columns([1, 3, 1])
idx = st.session_state.field_idx
if nav_l.button("⬅️") and idx > 0:
    st.session_state.field_idx -= 1; st.session_state.show_script = False; st.rerun()
nav_c.markdown(f"<div style='text-align:center;padding:8px;color:#888'>"
               f"Stop {idx+1} of {total}</div>", unsafe_allow_html=True)
if nav_r.button("➡️") and idx < total - 1:
    st.session_state.field_idx += 1; st.session_state.show_script = False; st.rerun()

# ── current property ──────────────────────────────────────────────────────────
prop = route[idx]
tier = prop.get("knock_tier","TBD")
score = int(prop.get("motivation_score") or 0)
addr_short = prop["address"].split(",")[0]
owner = prop.get("owner_name","Unknown")

# Logged indicator
if prop["id"] in st.session_state.logged_ids:
    st.success("✓ Already logged today")

# Address + tier
tier_cls = f"tier-badge-{tier}" if tier in ("T1","T2","T3") else "tier-badge-T3"
st.markdown(f"### {addr_short}")
st.markdown(f'<span class="{tier_cls}">{tier}</span> &nbsp; Score: **{score}**',
            unsafe_allow_html=True)
st.caption(f"Owner: {owner}")
st.caption(prop.get("primary_signal","")[:80] if prop.get("primary_signal") else "")

# Phone number — big and tappable
phone = prop.get("phone1") or prop.get("phone2")
if phone:
    st.markdown(f'<div class="big-phone"><a href="tel:{phone}" style="text-decoration:none;color:#0078D4">📱 {phone}</a></div>',
                unsafe_allow_html=True)
    if prop.get("email1"):
        st.caption(f"✉️ {prop['email1']}")
else:
    st.caption("📞 No phone on file — upload BatchSkipTracing results in Data tab")

# Google Maps link
maps_url = f"https://maps.google.com/?q={prop['address'].replace(' ', '+')}"
st.markdown(f"[🗺️ Navigate]({maps_url})", unsafe_allow_html=True)

st.divider()

# ── Door Script (expandable) ──────────────────────────────────────────────────
script_key = f"script_{prop['id']}"
script_data = st.session_state.get(script_key)

# Try loading from cached file
if not script_data:
    sf = os.path.join(os.path.dirname(__file__), "..", "..", "data", "door_scripts.json")
    if os.path.exists(sf):
        with open(sf, encoding="utf-8") as f:
            all_scripts = json.load(f)
        cached = all_scripts.get(prop["address"], all_scripts.get(addr_short))
        if cached:
            st.session_state[script_key] = cached
            script_data = cached

if script_data and "door_script" in script_data:
    with st.expander("🗣️ Door Script", expanded=st.session_state.show_script):
        st.markdown(script_data["door_script"])
    with st.expander("✉️ Leave-Behind Letter"):
        st.markdown(script_data["letter"])
        st.caption("Replace [NAME] and [PHONE]")
elif os.getenv("ANTHROPIC_API_KEY"):
    if st.button("⚡ Generate Script", use_container_width=True):
        with st.spinner("Writing..."):
            try:
                from agents.doorknock_script import generate_door_script
                row_dict = df[df.id == prop["id"]].iloc[0].to_dict()
                sc = generate_door_script(row_dict)
                st.session_state[script_key] = sc
                st.rerun()
            except Exception as e:
                st.error(str(e))
else:
    st.caption("Add ANTHROPIC_API_KEY to .env for door scripts")

st.divider()

# ── Quick Log ─────────────────────────────────────────────────────────────────
st.markdown("**Log this visit**")
outcome = st.selectbox("Outcome", [
    "No answer", "Left note", "Spoke — interested", "Spoke — not interested",
    "Callback scheduled", "Left voicemail", "Come back later"
], label_visibility="collapsed", key=f"out_{idx}")

notes = st.text_input("Quick note (optional)", placeholder="e.g. spoke with wife, call back Sat",
                       key=f"note_{idx}", label_visibility="collapsed")

col_log, col_fu = st.columns(2)
follow_up = col_fu.date_input("Follow-up", value=None, key=f"fu_{idx}",
                               label_visibility="collapsed")

if col_log.button("✓ Log", use_container_width=True, type="primary", key=f"log_{idx}"):
    log_contact(prop["id"], outcome, notes, follow_up)
    st.session_state.logged_ids.add(prop["id"])
    # Auto-advance to next stop
    if idx < total - 1:
        st.session_state.field_idx += 1
        st.session_state.show_script = False
    st.success(f"Logged: {outcome}")
    st.rerun()

# ── Route overview ────────────────────────────────────────────────────────────
st.divider()
with st.expander(f"Full route ({total} stops)"):
    for i, p in enumerate(route):
        a = p["address"].split(",")[0]
        t = p.get("knock_tier","?")
        done = "✓ " if p["id"] in st.session_state.logged_ids else ""
        current = "▶ " if i == idx else "   "
        st.markdown(f"{current}{done}**{i+1}.** {a} · {t}")

st.caption("REI Field Mode · http://localhost:8504/field_mode")
