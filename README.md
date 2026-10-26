# 投資雷達

一頁看完台股、全球股市、美國 AI 股、原物料匯率、國際局勢與本週財經重點的個人投資儀表板。
網頁有三個分頁：**今日早報**、**今天的世界**、**我的資產**。手機、平板、電腦都能開。

- 資料全部來自免費公開來源，不需要任何 API 金鑰
- 不使用 AI：新聞分類、地區標記、早報文字都是程式依規則整理
- 「我的資產」的持股只存在你自己的瀏覽器，不會上傳到任何地方

> 本工具僅供資訊整理，不構成投資建議。

---

## 檔案說明

| 檔案 | 用途 |
|---|---|
| `index.html` | 網頁本體（單一檔案） |
| `fetch_data.py` | 抓資料的程式，產生 `data/data.json` |
| `config.json` | 要追蹤哪些指數、股票、新聞關鍵字，都在這裡改 |
| `data/data.json` | 網頁讀取的資料（附的是示範資料） |
| `.github/workflows/update.yml` | GitHub 自動定時抓資料並發布網站 |
| `啟動-Windows.bat`、`啟動-Mac.command` | 在自己電腦上一鍵抓資料並開網頁 |

---

## 方法一：放上 GitHub，手機隨時看（推薦）

免費，每 3 小時自動更新一次，網址固定，手機加到主畫面就像 App。

1. 登入 GitHub，按右上角 **+ → New repository**，名稱例如 `invest-radar`，選 **Public**（免費方案的 GitHub Pages 需要公開倉庫；持股不會存在這裡，所以沒有隱私問題）。
2. 在新倉庫頁面按 **uploading an existing file**，把整個資料夾的檔案拖進去（包含 `.github` 資料夾；若拖曳時 `.github` 沒被帶上，可改用 GitHub Desktop 上傳）。按 **Commit changes**。
3. 進 **Settings → Pages**，在 **Build and deployment → Source** 選 **GitHub Actions**。
4. 進 **Actions** 分頁，左邊點「更新投資資料」，按 **Run workflow**。約 2–3 分鐘完成。
5. 網址是 `https://你的帳號.github.io/invest-radar/`。
6. （建議）編輯 `config.json`，把 `"repo_url"` 填成 `https://github.com/你的帳號/invest-radar`，網頁右上角就會出現「立即抓新資料」按鈕，點了直接到手動更新的頁面。

注意：
- GitHub 排程不保證準時，常延遲幾分鐘到幾十分鐘。
- 公開倉庫連續 60 天沒有任何更動，GitHub 會自動停用排程，到 Actions 頁面按一下重新啟用即可。

## 方法二：在自己電腦上執行

需要先安裝 Python 3.10 以上（Windows 安裝時勾選 **Add Python to PATH**）。

- **Windows**：雙擊 `啟動-Windows.bat`
- **Mac**：第一次先在終端機執行 `chmod +x 啟動-Mac.command`，之後雙擊即可

會自動安裝套件、抓資料、開啟 `http://localhost:8000`。要更新資料就重新執行一次。

手動指令：

```bash
pip install -r requirements.txt
python fetch_data.py          # 抓真實資料
python fetch_data.py --demo   # 產生示範資料（不連網）
python -m http.server 8000    # 再用瀏覽器開 http://localhost:8000
```

> 不能直接雙擊 `index.html` 開啟，瀏覽器會擋住讀取 `data.json`，一定要透過上面的方式。

---

## 自訂追蹤內容（config.json）

- `indices`：全球股市卡片（建議維持 9 個，排版剛好 3×3）
- `macro`：原物料、匯率、利率那一排
- `ai_stocks`：美國 AI 股九宮格與等權指數
- `watchlist`：**我的資產要自動帶入現價的股票**。持股代號不在這裡（也不在上面任何清單）就抓不到現價，只能手動輸入
- `news`：各區塊的新聞搜尋關鍵字
- `central_banks`：央行會議日期（目前放了 Fed 2026–2027 的 FOMC 日期）

代號用 Yahoo Finance 格式：台股上市 `2330.TW`、上櫃 `6488.TWO`、美股 `NVDA`、指數 `^TWII`。

## 資料來源與限制

| 來源 | 內容 | 說明 |
|---|---|---|
| Yahoo Finance | 股價、指數、原物料、匯率 | 透過 yfinance 套件，失敗時改用公開圖表 API。非官方介面，Yahoo 改版時可能暫時抓不到 |
| Google 新聞 RSS | 國際局勢、AI、台股、總經新聞 | 只取標題與連結 |
| 臺灣證券交易所 | 外資買賣超 | 收盤後約 15:00 公布 |
| yfinance | 美股財報日期 | 不一定每檔都抓得到 |

- **國際局勢是關鍵字規則判斷**：例如標題有「空襲、飛彈」歸為戰爭，有「伊朗」就標在伊朗。會有分錯或漏抓，點進原文確認。要調整可改 `fetch_data.py` 裡的 `CATEGORIES` 和 `PLACES`。
- 「升溫／新／持續／降溫」是跟上一次抓取的新聞數量比較。
- 地圖底圖從 jsDelivr 載入，載入失敗時只顯示座標點。
- 某一項抓不到時，其他區塊照常顯示；網頁右上角「資料來源」裡會列出這次抓取的錯誤。

## 我的資產

- 在網頁上新增持股（代號、股數、平均成本），自動計算市值、損益、報酬率、今日損益與配置比例。
- 美元計價的持股會用最新美元/台幣匯率換算成台幣；其他幣別目前不換算。
- 持股存在瀏覽器裡，換手機或清除瀏覽資料會不見，請用「匯出」把文字存起來，換裝置時「匯入」。
