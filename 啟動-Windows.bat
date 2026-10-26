@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
echo 安裝需要的套件（第一次會比較久）...
python -m pip install -r requirements.txt
echo.
echo 抓取最新資料...
python fetch_data.py
echo.
echo 開啟網頁：http://localhost:8000  （關閉這個視窗就會停止）
start "" http://localhost:8000/index.html
python -m http.server 8000
