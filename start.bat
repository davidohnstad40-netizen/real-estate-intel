@echo off
REM Start the REI Seller Intelligence Platform

echo Starting REI Platform...
set DB_PATH=./data/rei.duckdb
set ANTHROPIC_API_KEY=%ANTHROPIC_API_KEY%

start "REI Dashboard" cmd /k "python -m streamlit run app/main.py --server.port 8504"
timeout /t 5 /nobreak > nul

echo.
echo Dashboard running at: http://localhost:8504
echo.
echo To share with your realtor, open a second window and run:
echo   share.bat
echo.
echo This gives your realtor a public URL like:
echo   https://rei-blaine-targets.loca.lt
echo.
