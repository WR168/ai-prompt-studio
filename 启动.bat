@echo off
rem %~dp0 = 本 bat 所在目录（自动定位项目根，无需写死盘符路径）
cd /d %~dp0
call .venv\Scripts\activate.bat
start "" /min streamlit run app.py --server.headless=true
timeout /t 5 /nobreak >nul
start msedge "http://localhost:8501"
pause
