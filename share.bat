@echo off
REM Share the REI platform with your realtor via localtunnel
REM Run this AFTER starting the app (python -m streamlit run app/main.py)
REM The URL printed below can be shared directly -- no VPN, no setup needed

echo.
echo Starting REI platform share tunnel...
echo Share the URL below with your realtor.
echo They open it in any browser -- no install needed.
echo.
echo Press Ctrl+C to stop sharing.
echo.
lt --port 8504 --subdomain rei-blaine-targets
