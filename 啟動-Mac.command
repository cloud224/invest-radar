#!/bin/bash
cd "$(dirname "$0")"
echo "安裝需要的套件（第一次會比較久）..."
python3 -m pip install -r requirements.txt
echo
echo "抓取最新資料..."
python3 fetch_data.py
echo
echo "開啟網頁：http://localhost:8000 （關閉這個視窗就會停止）"
open "http://localhost:8000/index.html"
python3 -m http.server 8000
