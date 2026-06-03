"""
Email Alert System
==================
Sends an HTML email when tier upgrades are detected or on the daily schedule.

Config (add to .env):
  ALERT_EMAIL_TO       = you@email.com
  ALERT_EMAIL_FROM     = sender@gmail.com
  ALERT_EMAIL_PASSWORD = your-gmail-app-password   (not your login password --
                         use Gmail > Security > App Passwords)

Gmail App Password setup:
  1. Enable 2FA on your Google account
  2. Go to myaccount.google.com/apppasswords
  3. Create app password for "Mail" → copy the 16-char code
  4. Paste into ALERT_EMAIL_PASSWORD in .env

For Outlook/Hotmail: set ALERT_SMTP_HOST=smtp-mail.outlook.com, ALERT_SMTP_PORT=587
"""

import os, sys, smtplib, datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


TIER_COLOR = {"T1": "#C00000", "T2": "#D6A800", "T3": "#375623", "SKIP": "#888888"}
TIER_EMOJI = {"T1": "🔴", "T2": "🟡", "T3": "🟢", "SKIP": "⛔"}


def _build_html(upgrades: list, stats: dict, top5: list, today: datetime.date) -> str:
    upgrade_html = ""
    if upgrades:
        rows = "".join(f"""
        <tr>
          <td style="padding:6px 12px;font-weight:bold">{u['address']}</td>
          <td style="padding:6px 12px;color:{TIER_COLOR.get(u['old_tier'],'#888')}">{u['old_tier']}</td>
          <td style="padding:6px 12px;font-size:18px">→</td>
          <td style="padding:6px 12px;color:{TIER_COLOR.get(u['new_tier'],'#888')};font-weight:bold">{u['new_tier']}</td>
          <td style="padding:6px 12px">{u['score']}/100</td>
          <td style="padding:6px 12px;color:#555;font-size:12px">{(u.get('signal','') or '')[:60]}</td>
        </tr>""" for u in upgrades)
        upgrade_html = f"""
        <h2 style="color:#C00000">🚨 {len(upgrades)} Tier Upgrade{'s' if len(upgrades)>1 else ''} Detected</h2>
        <table style="border-collapse:collapse;width:100%;margin-bottom:20px">
          <tr style="background:#f5f5f5;font-weight:bold">
            <th style="padding:6px 12px;text-align:left">Address</th>
            <th style="padding:6px 12px;text-align:left">From</th>
            <th style="padding:6px 12px"></th>
            <th style="padding:6px 12px;text-align:left">To</th>
            <th style="padding:6px 12px;text-align:left">Score</th>
            <th style="padding:6px 12px;text-align:left">Signal</th>
          </tr>
          {rows}
        </table>"""
    else:
        upgrade_html = '<p style="color:#555">No tier upgrades today.</p>'

    top5_html = ""
    for i, (addr, tier, score, signal) in enumerate(top5, 1):
        color = TIER_COLOR.get(tier, "#888")
        emoji = TIER_EMOJI.get(tier, "⚪")
        top5_html += f"""
        <tr>
          <td style="padding:6px 12px;color:#888">{i}</td>
          <td style="padding:6px 12px;font-weight:bold">{addr}</td>
          <td style="padding:6px 12px;color:{color};font-weight:bold">{emoji} {tier}</td>
          <td style="padding:6px 12px">{score}/100</td>
          <td style="padding:6px 12px;color:#555;font-size:12px">{(signal or '')[:55]}</td>
        </tr>"""

    t1 = stats.get("T1", 0); t2 = stats.get("T2", 0)
    t3 = stats.get("T3", 0); avg = stats.get("avg", 0.0)

    return f"""
<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;max-width:700px;margin:0 auto;padding:20px;color:#333">

  <div style="background:#1a1a2e;color:white;padding:16px 20px;border-radius:8px;margin-bottom:24px">
    <h1 style="margin:0;font-size:20px">🏘️ REI Daily Intel</h1>
    <p style="margin:4px 0 0;color:#aaa;font-size:13px">Lakes of Radisson · Blaine MN · {today.strftime('%A, %B %d %Y')}</p>
  </div>

  <div style="display:flex;gap:16px;margin-bottom:24px">
    <div style="flex:1;background:#fff0f0;border-radius:8px;padding:12px 16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#C00000">{t1}</div>
      <div style="color:#888;font-size:12px">T1 -- Knock Now</div>
    </div>
    <div style="flex:1;background:#fffde7;border-radius:8px;padding:12px 16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#D6A800">{t2}</div>
      <div style="color:#888;font-size:12px">T2 -- Knock Next</div>
    </div>
    <div style="flex:1;background:#f0fff0;border-radius:8px;padding:12px 16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#375623">{t3}</div>
      <div style="color:#888;font-size:12px">T3 -- Cold Knock</div>
    </div>
    <div style="flex:1;background:#f8f8f8;border-radius:8px;padding:12px 16px;text-align:center">
      <div style="font-size:28px;font-weight:bold;color:#444">{avg:.0f}</div>
      <div style="color:#888;font-size:12px">Avg Score</div>
    </div>
  </div>

  {upgrade_html}

  <h2 style="color:#333;border-bottom:2px solid #eee;padding-bottom:8px">Top 5 by Score</h2>
  <table style="border-collapse:collapse;width:100%;margin-bottom:24px">
    <tr style="background:#f5f5f5;font-weight:bold">
      <th style="padding:6px 12px;text-align:left">#</th>
      <th style="padding:6px 12px;text-align:left">Address</th>
      <th style="padding:6px 12px;text-align:left">Tier</th>
      <th style="padding:6px 12px;text-align:left">Score</th>
      <th style="padding:6px 12px;text-align:left">Signal</th>
    </tr>
    {top5_html}
  </table>

  <p style="color:#aaa;font-size:11px;border-top:1px solid #eee;padding-top:12px">
    Real Estate Seller Intelligence Platform · Auto-generated daily report ·
    <a href="http://localhost:8504" style="color:#0078D4">Open Dashboard</a>
  </p>
</body>
</html>"""


def send_alert(upgrades: list, stats: dict, top5: list) -> bool:
    """
    Send the daily HTML email. Returns True if sent, False if skipped (no config).
    """
    to_addr   = os.getenv("ALERT_EMAIL_TO")
    from_addr = os.getenv("ALERT_EMAIL_FROM")
    password  = os.getenv("ALERT_EMAIL_PASSWORD")

    if not all([to_addr, from_addr, password]):
        print("[alerts] ALERT_EMAIL_* not configured -- skipping email.")
        return False

    smtp_host = os.getenv("ALERT_SMTP_HOST", "smtp.gmail.com")
    smtp_port = int(os.getenv("ALERT_SMTP_PORT", "465"))

    today   = datetime.date.today()
    subject = (f"🚨 REI Alert: {len(upgrades)} tier upgrade{'s' if len(upgrades)>1 else ''} -- {today}"
               if upgrades else f"🏘️ REI Daily Intel -- {today}")

    html = _build_html(upgrades, stats, top5, today)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"]    = from_addr
    msg["To"]      = to_addr
    msg.attach(MIMEText(html, "html"))

    try:
        if smtp_port == 465:
            with smtplib.SMTP_SSL(smtp_host, smtp_port) as server:
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addr, msg.as_string())
        else:  # TLS (587)
            with smtplib.SMTP(smtp_host, smtp_port) as server:
                server.starttls()
                server.login(from_addr, password)
                server.sendmail(from_addr, to_addr, msg.as_string())
        print(f"[alerts] Email sent -> {to_addr}")
        return True
    except Exception as e:
        print(f"[alerts] Email failed: {e}")
        return False


if __name__ == "__main__":
    # Quick test -- sends a sample email with no upgrades
    from db.schema import get_db
    con = get_db(read_only=True)
    tier_rows = con.execute("""
        SELECT knock_tier, COUNT(*), AVG(motivation_score)
        FROM property_scores GROUP BY knock_tier
    """).fetchall()
    top5 = con.execute("""
        SELECT p.address, ps.knock_tier, ps.motivation_score, ps.primary_signal
        FROM property_scores ps JOIN properties p ON p.id = ps.id
        ORDER BY ps.motivation_score DESC LIMIT 5
    """).fetchall()
    con.close()
    stats = {tier: cnt for tier, cnt, _ in tier_rows}
    stats["avg"] = sum(avg*cnt for _, cnt, avg in tier_rows if avg) / max(sum(cnt for _,cnt,_ in tier_rows),1)
    ok = send_alert([], stats, top5)
    print("Done." if ok else "No email config found -- add ALERT_EMAIL_* to .env")
