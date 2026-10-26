#!/usr/bin/env python3
"""
投資情報儀表板：資料抓取程式

抓取免費公開資料並輸出 data/data.json，給 index.html 顯示。
  - 行情：Yahoo Finance（優先用 yfinance 套件，失敗時改用公開圖表 API）
  - 新聞：Google 新聞 RSS（繁體中文）
  - 外資買賣超：臺灣證券交易所公開資料
  - 財報日期：yfinance（可能抓不到，抓不到就略過）

不使用任何 AI，新聞分類與地區標記都是關鍵字規則。

用法：
  python fetch_data.py            抓真實資料
  python fetch_data.py --demo     產生示範資料（不連網，用來測試畫面）
"""
from __future__ import annotations

import argparse
import html
import json
import math
import random
import re
import sys
import time
import traceback
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import quote_plus

try:
    from zoneinfo import ZoneInfo
except ImportError:  # Python < 3.9
    ZoneInfo = None  # type: ignore

ROOT = Path(__file__).resolve().parent
TPE = ZoneInfo("Asia/Taipei") if ZoneInfo else timezone(timedelta(hours=8))
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/128.0 Safari/537.36")

WARNINGS: list[str] = []


def warn(msg: str) -> None:
    WARNINGS.append(msg)
    print("  ! " + msg, file=sys.stderr)


# ---------------------------------------------------------------- HTTP

_session = None


def http_get(url: str, params: dict | None = None, timeout: int = 20, tries: int = 3):
    import requests  # 延後載入，--demo 模式不需要

    global _session
    if _session is None:
        _session = requests.Session()
        _session.headers.update({"User-Agent": UA, "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.8"})
    last = None
    for i in range(tries):
        try:
            r = _session.get(url, params=params, timeout=timeout)
            if r.status_code == 429:
                time.sleep(3 * (i + 1))
                last = RuntimeError("HTTP 429")
                continue
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{url} 抓取失敗：{last}")


# ---------------------------------------------------------------- 行情

def _hist_yfinance(sym: str, period: str):
    import yfinance as yf  # type: ignore

    df = yf.Ticker(sym).history(period=period, interval="1d", auto_adjust=False)
    if df is None or df.empty:
        raise RuntimeError("yfinance 無資料")
    df = df.dropna(subset=["Close"])
    dates = [d.strftime("%Y-%m-%d") for d in df.index]
    closes = [float(x) for x in df["Close"].tolist()]
    currency = ""
    try:
        currency = yf.Ticker(sym).fast_info.get("currency") or ""
    except Exception:  # noqa: BLE001
        pass
    return dates, closes, currency


def _hist_chart_api(sym: str, period: str):
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{quote_plus(sym)}"
    r = http_get(url, {"range": period, "interval": "1d", "includePrePost": "false"})
    res = r.json()["chart"]["result"][0]
    meta = res.get("meta", {})
    ts = res.get("timestamp") or []
    closes = res["indicators"]["quote"][0].get("close") or []
    offset = int(meta.get("gmtoffset") or 0)
    dates, out = [], []
    for t, c in zip(ts, closes):
        if c is None:
            continue
        d = datetime.fromtimestamp(t + offset, tz=timezone.utc).strftime("%Y-%m-%d")
        if dates and dates[-1] == d:  # 同一天重複（盤中資料）以最新為準
            out[-1] = float(c)
            continue
        dates.append(d)
        out.append(float(c))
    if not out:
        raise RuntimeError("圖表 API 無資料")
    return dates, out, meta.get("currency") or ""


def get_history(sym: str, period: str = "1y") -> dict | None:
    errors = []
    for fn in (_hist_yfinance, _hist_chart_api):
        try:
            dates, closes, cur = fn(sym, period)
            if len(closes) >= 2:
                return {"d": dates, "c": [round(x, 4) for x in closes], "currency": cur}
        except ImportError:
            continue
        except Exception as e:  # noqa: BLE001
            errors.append(f"{fn.__name__}: {e}")
    warn(f"{sym} 行情抓不到（{'; '.join(errors) or '沒有可用的抓取方式'}）")
    return None


def pct(a: float, b: float) -> float | None:
    if b in (0, None) or a is None:
        return None
    return round((a / b - 1) * 100, 2)


def metrics(h: dict) -> dict:
    c = h["c"]
    last, prev = c[-1], c[-2]
    hi, lo = max(c), min(c)

    def back(n):
        return c[-1 - n] if len(c) > n else c[0]

    pos = (last - lo) / (hi - lo) if hi > lo else 0.5
    return {
        "last": round(last, 4),
        "prev": round(prev, 4),
        "chg": round(last - prev, 4),
        "chgPct": pct(last, prev),
        "d5Pct": pct(last, back(5)),
        "m1Pct": pct(last, back(21)),
        "y1Pct": pct(last, c[0]),
        "hi": round(hi, 4),
        "lo": round(lo, 4),
        "pos": round(pos, 4),
        "fromHighPct": pct(last, hi),
        "date": h["d"][-1],
        "currency": h.get("currency", ""),
    }


def pos_label(p: float) -> str:
    if p >= 0.8:
        return "高檔"
    if p >= 0.6:
        return "偏高"
    if p >= 0.4:
        return "中間"
    if p >= 0.2:
        return "偏低"
    return "低檔"


# ---------------------------------------------------------------- 新聞

def clean_title(t: str) -> tuple[str, str]:
    t = html.unescape(t or "").strip()
    src = ""
    m = re.match(r"^(.*)\s+-\s+([^-]{1,30})$", t)
    if m:
        t, src = m.group(1).strip(), m.group(2).strip()
    return t, src


def fetch_rss(query: str, max_age_h: int) -> list[dict]:
    url = ("https://news.google.com/rss/search?q=" + quote_plus(query + f" when:{max(1, max_age_h // 24)}d")
           + "&hl=zh-TW&gl=TW&ceid=TW:zh-Hant")
    r = http_get(url)
    root = ET.fromstring(r.content)
    out = []
    now = datetime.now(timezone.utc)
    for it in root.iter("item"):
        title, src = clean_title(it.findtext("title") or "")
        s = it.find("source")
        if s is not None and s.text:
            src = s.text.strip()
        try:
            pub = parsedate_to_datetime(it.findtext("pubDate") or "")
            if pub.tzinfo is None:
                pub = pub.replace(tzinfo=timezone.utc)
        except Exception:  # noqa: BLE001
            pub = now
        if (now - pub).total_seconds() > max_age_h * 3600:
            continue
        out.append({
            "title": title,
            "source": src,
            "link": (it.findtext("link") or "").strip(),
            "ts": int(pub.timestamp()),
        })
    return out


def norm_key(t: str) -> str:
    return re.sub(r"[\s\W_]+", "", t)[:18]


def gather_news(queries: list[str], max_age_h: int, limit: int = 60) -> list[dict]:
    seen, items = set(), []
    for q in queries:
        try:
            for n in fetch_rss(q, max_age_h):
                k = norm_key(n["title"])
                if k and k not in seen:
                    seen.add(k)
                    items.append(n)
        except Exception as e:  # noqa: BLE001
            warn(f"新聞「{q}」抓不到：{e}")
        time.sleep(0.6)
    items.sort(key=lambda x: -x["ts"])
    return items[:limit]


# ---------------------------------------------------------------- 局勢分類（關鍵字規則）

CATEGORIES = [
    # key, 名稱, 關鍵字（依序比對，先命中者為準）
    ("ceasefire", "停火·談判", ["停火", "和談", "休戰", "斡旋", "談判", "和平協議", "會談"]),
    ("epidemic", "疫情", ["疫情", "病例", "伊波拉", "霍亂", "禽流感", "登革熱", "猴痘", "病毒", "感染", "世衛"]),
    ("disaster", "天災", ["地震", "颱風", "颶風", "洪水", "海嘯", "野火", "山火", "土石流", "暴雨"]),
    ("war", "戰爭·空襲", ["空襲", "轟炸", "飛彈", "導彈", "砲擊", "交火", "開戰", "戰爭", "戰事", "無人機攻擊", "襲擊", "入侵", "攻擊"]),
    ("unrest", "動亂·抗議", ["抗議", "示威", "暴動", "騷亂", "罷工", "政變", "內戰", "叛軍", "衝突", "被捕"]),
    ("military", "軍事威脅", ["軍演", "演習", "繞台", "艦隊", "航艦", "部署", "核武", "試射", "軍事", "威脅", "封鎖"]),
    ("politics", "政治", ["選舉", "大選", "制裁", "關稅", "國會", "公投", "下台", "內閣", "總統"]),
]
CAT_NAME = {k: n for k, n, _ in CATEGORIES}
INTENSE = ["死亡", "喪生", "罹難", "傷亡", "身亡", "大規模", "升級", "擴大", "全面", "緊急", "數百", "上千", "逾千"]

# 地名：(顯示名, 緯度, 經度, 權重)  權重低 = 太常出現，只在沒有其他地名時採用
PLACES = {
    "伊朗": ("伊朗", 32.4, 53.7, 3), "以色列": ("以色列", 31.5, 34.9, 3), "加薩": ("加薩", 31.4, 34.4, 3),
    "黎巴嫩": ("黎巴嫩", 33.9, 35.9, 3), "真主黨": ("黎巴嫩", 33.9, 35.9, 2), "哈瑪斯": ("加薩", 31.4, 34.4, 2),
    "烏克蘭": ("烏克蘭", 49.0, 31.4, 3), "基輔": ("烏克蘭", 50.4, 30.5, 3), "俄羅斯": ("俄羅斯", 55.8, 37.6, 2),
    "俄國": ("俄羅斯", 55.8, 37.6, 2), "俄烏": ("烏克蘭", 49.0, 31.4, 3), "莫斯科": ("俄羅斯", 55.8, 37.6, 2),
    "葉門": ("葉門", 15.6, 48.0, 3), "胡塞": ("葉門", 15.4, 44.2, 3), "沙烏地": ("沙烏地", 24.7, 46.7, 3),
    "沙國": ("沙烏地", 24.7, 46.7, 3), "卡達": ("卡達", 25.3, 51.5, 3), "伊拉克": ("伊拉克", 33.3, 44.4, 3),
    "敘利亞": ("敘利亞", 34.8, 38.9, 3), "約旦": ("約旦", 31.9, 35.9, 3), "土耳其": ("土耳其", 39.0, 35.2, 3),
    "埃及": ("埃及", 26.8, 30.8, 3), "蘇丹": ("蘇丹", 15.6, 32.5, 3), "南蘇丹": ("南蘇丹", 7.9, 30.0, 4),
    "利比亞": ("利比亞", 26.3, 17.2, 3), "衣索比亞": ("衣索比亞", 9.1, 40.5, 3), "索馬利亞": ("索馬利亞", 5.2, 46.2, 3),
    "剛果": ("剛果", -2.9, 23.7, 3), "奈及利亞": ("奈及利亞", 9.1, 8.7, 3), "肯亞": ("肯亞", 0.0, 37.9, 3),
    "南非": ("南非", -30.6, 22.9, 3), "馬利": ("馬利", 17.6, -4.0, 3), "尼日": ("尼日", 17.6, 8.1, 3),
    "烏干達": ("烏干達", 1.4, 32.3, 3), "法國": ("法國", 46.6, 2.2, 3), "巴黎": ("法國", 48.9, 2.35, 3),
    "德國": ("德國", 51.2, 10.4, 3), "英國": ("英國", 52.4, -1.5, 3), "倫敦": ("英國", 51.5, -0.1, 3),
    "義大利": ("義大利", 42.8, 12.6, 3), "西班牙": ("西班牙", 40.4, -3.7, 3), "波蘭": ("波蘭", 52.1, 19.4, 3),
    "塞爾維亞": ("塞爾維亞", 44.0, 20.9, 3), "匈牙利": ("匈牙利", 47.2, 19.5, 3), "希臘": ("希臘", 39.1, 21.8, 3),
    "荷蘭": ("荷蘭", 52.1, 5.3, 3), "比利時": ("比利時", 50.5, 4.5, 3), "歐盟": ("歐盟", 50.85, 4.35, 2),
    "羅馬尼亞": ("羅馬尼亞", 45.9, 24.9, 3), "喬治亞": ("喬治亞", 42.3, 43.4, 3), "白俄": ("白俄羅斯", 53.7, 27.9, 3),
    "格陵蘭": ("格陵蘭", 72.0, -40.0, 3), "北韓": ("北韓", 40.3, 127.5, 3), "南韓": ("南韓", 36.5, 127.8, 3),
    "韓國": ("南韓", 36.5, 127.8, 2), "日本": ("日本", 36.2, 138.3, 2), "菲律賓": ("菲律賓", 12.9, 121.8, 3),
    "南海": ("南海", 13.0, 114.0, 3), "台海": ("台海", 24.0, 119.6, 3), "共艦": ("台海", 24.0, 119.6, 3),
    "共機": ("台海", 24.0, 119.6, 3), "解放軍": ("台海", 24.0, 119.6, 2), "台灣": ("台灣", 23.7, 121.0, 1),
    "中國": ("中國", 35.9, 104.2, 1), "北京": ("中國", 39.9, 116.4, 1), "香港": ("香港", 22.3, 114.2, 2),
    "印度": ("印度", 22.0, 79.0, 2), "巴基斯坦": ("巴基斯坦", 30.4, 69.3, 3), "阿富汗": ("阿富汗", 33.9, 67.7, 3),
    "孟加拉": ("孟加拉", 23.7, 90.4, 3), "尼泊爾": ("尼泊爾", 28.4, 84.1, 3), "斯里蘭卡": ("斯里蘭卡", 7.9, 80.8, 3),
    "緬甸": ("緬甸", 21.9, 95.9, 3), "泰國": ("泰國", 15.9, 100.9, 3), "柬埔寨": ("柬埔寨", 12.6, 104.9, 3),
    "越南": ("越南", 14.1, 108.3, 3), "印尼": ("印尼", -2.5, 118.0, 3), "馬來西亞": ("馬來西亞", 4.2, 102.0, 3),
    "澳洲": ("澳洲", -25.3, 133.8, 2), "紐西蘭": ("紐西蘭", -40.9, 174.9, 3), "委內瑞拉": ("委內瑞拉", 6.4, -66.6, 3),
    "墨西哥": ("墨西哥", 23.6, -102.6, 3), "巴西": ("巴西", -14.2, -51.9, 3), "阿根廷": ("阿根廷", -38.4, -63.6, 3),
    "哥倫比亞": ("哥倫比亞", 4.6, -74.3, 3), "秘魯": ("秘魯", -9.2, -75.0, 3), "智利": ("智利", -35.7, -71.5, 3),
    "古巴": ("古巴", 21.5, -77.8, 3), "海地": ("海地", 18.97, -72.3, 3), "加拿大": ("加拿大", 56.1, -106.3, 2),
    "荷莫茲": ("荷莫茲海峽", 26.6, 56.3, 4), "紅海": ("紅海", 20.0, 38.5, 3), "巴拿馬": ("巴拿馬", 8.5, -80.8, 3),
    "美國": ("美國", 38.9, -77.0, 1), "白宮": ("美國", 38.9, -77.0, 1), "川普": ("美國", 38.9, -77.0, 0),
    "五角大廈": ("美國", 38.9, -77.0, 1), "美軍": ("美國", 38.9, -77.0, 0),
}


def classify(title: str) -> str | None:
    for key, _, kws in CATEGORIES:
        if any(k in title for k in kws):
            return key
    return None


def locate(title: str):
    best = None
    for kw, (name, lat, lon, w) in PLACES.items():
        i = title.find(kw)
        if i < 0:
            continue
        cand = (w, -i, name, lat, lon)
        if best is None or cand[:2] > best[:2]:
            best = cand
    if best is None or best[0] <= 0:
        return None
    return best[2], best[3], best[4]


def build_hotspots(news: list[dict], prev: dict | None) -> list[dict]:
    groups: dict[tuple, dict] = {}
    for n in news:
        cat = classify(n["title"])
        loc = locate(n["title"])
        if not cat or not loc:
            continue
        name, lat, lon = loc
        g = groups.setdefault((name, cat), {
            "place": name, "cat": cat, "catName": CAT_NAME[cat], "lat": lat, "lon": lon,
            "count": 0, "intense": 0, "items": [],
        })
        g["count"] += 1
        g["intense"] += sum(1 for k in INTENSE if k in n["title"])
        if len(g["items"]) < 5:
            g["items"].append(n)
    prev_counts = {}
    for h in (prev or {}).get("world", {}).get("hotspots", []) or []:
        prev_counts[(h.get("place"), h.get("cat"))] = h.get("count", 0)
    out = []
    for key, g in groups.items():
        score = g["count"] + 1.5 * min(g["intense"], 4)
        if score < 2:  # 只有一則、沒有強烈字眼的不列入
            continue
        g["score"] = round(score, 1)
        g["level"] = 3 if score >= 7 else 2 if score >= 4 else 1
        pc = prev_counts.get(key)
        if prev is None:
            g["status"] = "持續"
        elif pc is None:
            g["status"] = "新"
        elif g["count"] >= pc * 1.5 and g["count"] - pc >= 2:
            g["status"] = "升溫"
        elif g["count"] <= pc * 0.5:
            g["status"] = "降溫"
        else:
            g["status"] = "持續"
        g["headline"] = g["items"][0]["title"]
        out.append(g)
    out.sort(key=lambda x: -x["score"])
    return out[:14]


# ---------------------------------------------------------------- 其他資料

def twse_foreign() -> dict | None:
    """臺灣證交所 三大法人買賣金額統計（最近一個交易日）。"""
    try:
        r = http_get("https://www.twse.com.tw/rwd/zh/fund/BFI82U", {"response": "json"})
        j = r.json()
        if j.get("stat") not in ("OK", "ok", None):
            raise RuntimeError(j.get("stat"))
        net = 0.0
        found = False
        for row in j.get("data", []):
            if row and str(row[0]).startswith("外資"):
                net += float(str(row[3]).replace(",", ""))
                found = True
        if not found:
            raise RuntimeError("找不到外資欄位")
        d = str(j.get("date", ""))
        return {"date": f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d, "netYi": round(net / 1e8, 1)}
    except Exception as e:  # noqa: BLE001
        warn(f"證交所外資資料抓不到：{e}")
        return None


def earnings_dates(stocks: list[dict]) -> list[dict]:
    out = []
    try:
        import yfinance as yf  # type: ignore
    except ImportError:
        warn("沒有安裝 yfinance，略過財報日期")
        return out
    today = datetime.now(TPE).date()
    for s in stocks:
        if s["sym"].endswith(".TW"):
            continue
        try:
            cal = yf.Ticker(s["sym"]).calendar
            ed = None
            if isinstance(cal, dict):
                v = cal.get("Earnings Date")
                ed = v[0] if isinstance(v, (list, tuple)) and v else v
            elif cal is not None and hasattr(cal, "loc"):
                ed = cal.loc["Earnings Date"][0]
            if ed is None:
                continue
            if isinstance(ed, datetime):
                ed = ed.date()
            if isinstance(ed, date) and today - timedelta(days=1) <= ed <= today + timedelta(days=45):
                out.append({"date": ed.isoformat(), "sym": s["sym"], "name": s["name"]})
        except Exception as e:  # noqa: BLE001
            warn(f"{s['sym']} 財報日期抓不到：{e}")
        time.sleep(0.4)
    out.sort(key=lambda x: x["date"])
    return out


def upcoming_meetings(cfg: dict, days: int = 21) -> list[dict]:
    now = datetime.now(TPE)
    out = []
    for cb in cfg.get("central_banks", []):
        tz = ZoneInfo(cb["tz"]) if ZoneInfo else timezone.utc
        hh, mm = (int(x) for x in cb.get("time", "14:00").split(":"))
        for ds in cb["dates"]:
            y, m, d = (int(x) for x in ds.split("-"))
            local = datetime(y, m, d, hh, mm, tzinfo=tz)
            tpe = local.astimezone(TPE)
            if now - timedelta(hours=12) <= tpe <= now + timedelta(days=days):
                out.append({
                    "name": cb["name"], "short": cb.get("short", "央行"), "date": ds,
                    "tpe": tpe.strftime("%m/%d %H:%M"), "ts": int(tpe.timestamp()),
                })
    out.sort(key=lambda x: x["ts"])
    return out


# ---------------------------------------------------------------- 文字整理（規則產生，非 AI）

def arrow(p) -> str:
    if p is None:
        return "—"
    return f"漲 {p:.2f}%" if p > 0 else f"跌 {abs(p):.2f}%" if p < 0 else "平盤"


def fmt_num(x, nd=2) -> str:
    if x is None:
        return "—"
    return f"{x:,.{nd}f}"


def build_finance_points(D: dict, cfg: dict) -> list[dict]:
    pts = []
    for m in D.get("calendar", {}).get("meetings", [])[:2]:
        pts.append({"tag": m["short"], "level": "info",
                    "title": f"{m['name']} {m['date'][5:].replace('-', '/')}",
                    "text": f"台灣時間 {m['tpe']} 公布"})
    earn = D.get("calendar", {}).get("earnings", [])
    week = [e for e in earn if e["date"] <= (datetime.now(TPE).date() + timedelta(days=8)).isoformat()]
    if week:
        names = "、".join(e["name"] for e in week[:5])
        pts.append({"tag": "財報", "level": "warn", "title": f"{names} 公布財報",
                    "text": "、".join(f"{e['name']} {e['date'][5:].replace('-', '/')}" for e in week[:5])})
    q = D.get("quotes", {})
    brent = q.get("BZ=F")
    if brent:
        h = D["series"].get("BZ=F", {}).get("c", [])
        six = h[-126:] if h else []
        note = ""
        if six and brent["last"] >= max(six) * 0.995:
            note = "，創近六個月新高"
        elif six and brent["last"] <= min(six) * 1.005:
            note = "，創近六個月新低"
        pts.append({"tag": "油價", "level": "hot" if note else "info",
                    "title": f"布蘭特油價 {fmt_num(brent['last'])} 美元",
                    "text": f"今日{arrow(brent['chgPct'])}，近五天 {brent['d5Pct']:+.2f}%{note}"})
    fx = D.get("twse_foreign")
    if fx:
        act = "買超" if fx["netYi"] >= 0 else "賣超"
        pts.append({"tag": "外資", "level": "hot" if abs(fx["netYi"]) >= 100 else "info",
                    "title": f"外資{act}台股 {abs(fx['netYi']):,.1f} 億元",
                    "text": f"證交所 {fx['date']} 三大法人統計"})
    gold = q.get("GC=F")
    if gold:
        pts.append({"tag": "避險", "level": "info", "title": f"黃金 {fmt_num(gold['last'], 0)} 美元",
                    "text": f"今日{arrow(gold['chgPct'])}，一年內位置 {pos_label(gold['pos'])}"})
    vix = q.get("^VIX")
    if vix:
        lv = "偏高，市場緊張" if vix["last"] >= 25 else "正常" if vix["last"] >= 15 else "偏低，市場樂觀"
        pts.append({"tag": "情緒", "level": "hot" if vix["last"] >= 25 else "info",
                    "title": f"VIX 恐慌指數 {fmt_num(vix['last'])}", "text": f"{lv}（今日{arrow(vix['chgPct'])}）"})
    twd = q.get("TWD=X")
    if twd:
        pts.append({"tag": "匯率", "level": "info", "title": f"美元兌台幣 {fmt_num(twd['last'], 3)}",
                    "text": f"今日{arrow(twd['chgPct'])}（數字變大代表台幣貶值）"})
    return pts


def build_oneliner(D: dict) -> str:
    parts = []
    t = D.get("taiex")
    if t:
        parts.append(f"台股{arrow(t['chgPct'])}")
    brent = D.get("quotes", {}).get("BZ=F")
    if brent:
        parts.append(f"布蘭特油價 {brent['last']:.1f} 美元")
    s = "、".join(parts)
    hs = D.get("world", {}).get("hotspots", [])[:3]
    if hs:
        s += "；" + "、".join(f"{h['place']}{h['catName'].split('·')[0]}" for h in hs)
    ms = D.get("calendar", {}).get("meetings", [])
    if ms:
        s += f"；{ms[0]['date'][5:].replace('-', '/')} {ms[0]['name']}"
    return s + "。" if s else ""


def build_brief(D: dict, cfg: dict) -> list[dict]:
    secs = []
    q = D.get("quotes", {})

    us = [i for i in D.get("indices", []) if i["sym"] in ("^GSPC", "^IXIC")]
    if us or q.get("^SOX"):
        b = [f"{i['name']} {fmt_num(i['last'])}，{arrow(i['chgPct'])}（近五天 {i['d5Pct']:+.2f}%）" for i in us]
        sox = q.get("^SOX")
        if sox:
            b.append(f"費城半導體 {fmt_num(sox['last'])}，{arrow(sox['chgPct'])}")
        ai = sorted(D.get("ai", {}).get("stocks", []), key=lambda x: -(x.get("chgPct") or 0))
        if ai:
            b.append(f"AI 股表現最好：{ai[0]['name']} {ai[0]['chgPct']:+.2f}%；最差：{ai[-1]['name']} {ai[-1]['chgPct']:+.2f}%")
        secs.append({"title": "美股", "bullets": b})

    t = D.get("taiex")
    if t:
        b = [f"加權指數 {fmt_num(t['last'])}（{t['date']}），{arrow(t['chgPct'])}",
             f"近五天 {t['d5Pct']:+.2f}%、近一個月 {t['m1Pct']:+.2f}%、近一年 {t['y1Pct']:+.2f}%",
             f"位於一年區間的 {round(t['pos'] * 100)}% 位置（{pos_label(t['pos'])}），距一年最高點 {t['fromHighPct']:+.2f}%"]
        fx = D.get("twse_foreign")
        if fx:
            b.append(f"外資{'買超' if fx['netYi'] >= 0 else '賣超'} {abs(fx['netYi']):,.1f} 億元（{fx['date']}）")
        secs.append({"title": "台股", "bullets": b})

    asia = [i for i in D.get("indices", []) if i["sym"] in ("^N225", "^KS11", "^HSI", "000001.SS")]
    eu = [i for i in D.get("indices", []) if i["sym"] in ("^GDAXI", "^FTSE", "^NSEI")]
    if asia or eu:
        secs.append({"title": "亞洲與歐洲股市",
                     "bullets": ["、".join(f"{i['name']} {i['chgPct']:+.2f}%" for i in asia),
                                 "、".join(f"{i['name']} {i['chgPct']:+.2f}%" for i in eu)]})

    mb = []
    for m in D.get("macro", []):
        unit = m.get("unit") or ""
        mb.append(f"{m['name']} {fmt_num(m['last'], 3 if m['sym'] == 'TWD=X' else 2)}{(' ' + unit) if unit and unit != '%' else unit}，{arrow(m['chgPct'])}")
    if mb:
        secs.append({"title": "原物料、匯率與利率", "bullets": mb})

    hs = D.get("world", {}).get("hotspots", [])[:5]
    if hs:
        secs.append({"title": "國際局勢", "bullets": [f"【{h['place']}·{h['catName']}·{h['status']}】{h['headline']}" for h in hs],
                     "links": [h["items"][0]["link"] for h in hs]})

    ai_news = (D.get("ai", {}).get("models", [])[:2] + D.get("ai", {}).get("money", [])[:2])
    if ai_news:
        secs.append({"title": "AI 動態", "bullets": [n["title"] for n in ai_news], "links": [n["link"] for n in ai_news]})

    cal = []
    for m in D.get("calendar", {}).get("meetings", [])[:3]:
        cal.append(f"{m['date'][5:].replace('-', '/')} {m['name']}（台灣時間 {m['tpe']}）")
    for e in D.get("calendar", {}).get("earnings", [])[:6]:
        cal.append(f"{e['date'][5:].replace('-', '/')} {e['name']} 財報")
    if cal:
        secs.append({"title": "接下來要注意", "bullets": cal})
    return secs


# ---------------------------------------------------------------- 主流程

def load_prev(path: Path) -> dict | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def quote_entry(sym: str, name: str, h: dict, extra: dict | None = None) -> dict:
    m = metrics(h)
    m.update({"sym": sym, "name": name, "posLabel": pos_label(m["pos"])})
    if extra:
        m.update(extra)
    return m


def collect(cfg: dict, prev: dict | None, demo: bool) -> dict:
    hist = demo_history if demo else get_history
    D: dict = {"series": {}, "quotes": {}}

    def load(item: dict, period="1y", keep=True):
        h = hist(item["sym"], period)
        if not h:
            return None
        if keep:
            D["series"][item["sym"]] = {"d": h["d"], "c": [round(x, 2) for x in h["c"]]}
        q = quote_entry(item["sym"], item["name"], h, {"unit": item.get("unit", "")})
        D["quotes"][item["sym"]] = q
        return q

    print("行情…")
    D["taiex"] = load(cfg["taiex"])
    D["indices"] = [q for q in (load(i) for i in cfg["indices"]) if q]
    D["macro"] = [q for q in (load(i) for i in cfg["macro"]) if q]
    sox = load(cfg["ai_index"])
    ai_stocks = [q for q in (load(i) for i in cfg["ai_stocks"]) if q]
    for i in cfg.get("watchlist", []):
        if i["sym"] not in D["quotes"]:
            load(i, keep=False)

    # AI 九強等權指數（起點 = 100）
    ai_idx = None
    if ai_stocks:
        base_dates = D["series"].get(cfg["ai_index"]["sym"], {}).get("d") or D["series"][ai_stocks[0]["sym"]]["d"]
        lookups = []
        for s in ai_stocks:
            ser = D["series"][s["sym"]]
            lookups.append(dict(zip(ser["d"], ser["c"])))
        vals, lastv = [], [None] * len(lookups)
        for d in base_dates:
            for k, lk in enumerate(lookups):
                if d in lk:
                    lastv[k] = lk[d]
            vals.append(lastv[:])
        firsts = [next((v[k] for v in vals if v[k] is not None), None) for k in range(len(lookups))]
        idx = []
        for v in vals:
            rs = [v[k] / firsts[k] for k in range(len(lookups)) if v[k] is not None and firsts[k]]
            idx.append(round(100 * sum(rs) / len(rs), 2) if rs else None)
        ai_idx = {"d": base_dates, "c": idx}
    D["ai"] = {"index": cfg["ai_index"]["name"], "indexSym": cfg["ai_index"]["sym"],
               "stocks": ai_stocks, "basket": ai_idx}
    if ai_idx and len([x for x in ai_idx["c"] if x]) > 6:
        c = [x for x in ai_idx["c"] if x]
        D["ai"]["basketD5Pct"] = pct(c[-1], c[-6])
        D["ai"]["basketPos"] = round((c[-1] - min(c)) / (max(c) - min(c)), 3) if max(c) > min(c) else 0.5

    print("新聞…")
    nc = cfg["news"]
    age = int(nc.get("max_age_hours", 48))
    if demo:
        world_news, ai_market, ai_models, ai_money, tw_news, macro_news = demo_news()
    else:
        world_news = gather_news(nc["world_queries"], age, 120)
        ai_market = gather_news(nc["ai_market_queries"], age, 10)
        ai_models = gather_news(nc["ai_model_queries"], age, 8)
        ai_money = gather_news(nc["ai_money_queries"], age, 8)
        tw_news = gather_news(nc["tw_queries"], age, 8)
        macro_news = gather_news(nc["macro_queries"], age, 10)
    hotspots = build_hotspots(world_news, prev)
    D["world"] = {"hotspots": hotspots, "latest": world_news[:20]}
    D["ai"].update({"market": ai_market, "models": ai_models, "money": ai_money})
    D["news"] = {"tw": tw_news, "macro": macro_news}

    print("行事曆與法人…")
    D["calendar"] = {"meetings": upcoming_meetings(cfg),
                     "earnings": demo_earnings(cfg) if demo else earnings_dates(cfg["ai_stocks"])}
    D["twse_foreign"] = {"date": D["taiex"]["date"] if D.get("taiex") else "", "netYi": -235.6} if demo else twse_foreign()

    D["finance"] = build_finance_points(D, cfg)
    D["oneliner"] = build_oneliner(D)
    D["brief"] = build_brief(D, cfg)
    return D


def main() -> int:
    for st in (sys.stdout, sys.stderr):
        try:
            st.reconfigure(encoding="utf-8", errors="replace")  # Windows 終端機中文
        except Exception:  # noqa: BLE001
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="產生示範資料，不連網")
    ap.add_argument("--out", default=str(ROOT / "data" / "data.json"))
    ap.add_argument("--prev", default=None, help="上一次的 data.json（用來判斷局勢升溫/新出現）")
    ap.add_argument("--config", default=str(ROOT / "config.json"))
    a = ap.parse_args()

    cfg = json.loads(Path(a.config).read_text(encoding="utf-8"))
    out = Path(a.out)
    prev = load_prev(Path(a.prev)) if a.prev else load_prev(out)
    if prev and prev.get("meta", {}).get("demo") and not a.demo:
        prev = None

    try:
        D = collect(cfg, prev, a.demo)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return 1

    if not a.demo and not D.get("taiex") and not D.get("indices"):
        print("主要行情全部抓取失敗，保留舊的 data.json 不覆蓋。", file=sys.stderr)
        return 2

    now = datetime.now(TPE)
    D["meta"] = {
        "fetchedAt": now.isoformat(timespec="minutes"),
        "fetchedAtText": now.strftime("%m/%d %H:%M"),
        "marketDate": (D.get("taiex") or {}).get("date", ""),
        "demo": bool(a.demo),
        "repo_url": cfg.get("repo_url", ""),
        "warnings": WARNINGS[:40],
        "watchlist": cfg.get("watchlist", []),
        "sources": [
            {"name": "Yahoo Finance", "use": "股價、指數、原物料、匯率（約延遲 15–20 分鐘，收盤後為收盤價）", "url": "https://finance.yahoo.com"},
            {"name": "Google 新聞 RSS", "use": "國際局勢、AI、台股與總經新聞標題", "url": "https://news.google.com"},
            {"name": "臺灣證券交易所", "use": "三大法人（外資）買賣超", "url": "https://www.twse.com.tw"},
            {"name": "美國聯準會 Fed", "use": "FOMC 會議日期（寫在 config.json）", "url": "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"},
        ],
    }
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(D, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"完成：{out}（{out.stat().st_size // 1024} KB，警告 {len(WARNINGS)} 則）")
    return 0


# ---------------------------------------------------------------- 示範資料

_DEMO_BASE = {
    "^TWII": 26000, "^GSPC": 5600, "^IXIC": 17800, "^N225": 38000, "^KS11": 2600, "^HSI": 19000,
    "000001.SS": 3000, "^GDAXI": 19000, "^FTSE": 8200, "^NSEI": 24000, "BZ=F": 75, "CL=F": 71,
    "GC=F": 2600, "TWD=X": 32.1, "DX-Y.NYB": 103, "^TNX": 4.1, "^VIX": 16, "BTC-USD": 60000,
    "^SOX": 4800, "NVDA": 120, "2330.TW": 950, "MSFT": 420, "GOOGL": 165, "META": 560, "AMZN": 185,
    "AAPL": 225, "AVGO": 160, "AMD": 150, "0050.TW": 180, "0056.TW": 38, "00878.TW": 22,
    "2317.TW": 190, "2454.TW": 1250, "VOO": 520, "QQQ": 480,
}


def demo_history(sym: str, period: str = "1y") -> dict:
    rnd = random.Random(sum(map(ord, sym)) * 31 + 7)
    base = _DEMO_BASE.get(sym, 100)
    flat = sym in ("TWD=X", "DX-Y.NYB", "^TNX", "^VIX", "BZ=F", "CL=F")
    drift = 0.0 if flat else rnd.uniform(-0.0003, 0.0011)
    vol = 0.006 if sym in ("TWD=X", "DX-Y.NYB") else 0.03 if sym in ("^VIX", "BTC-USD") else 0.013
    d = date.today() - timedelta(days=365)
    dates, cl, v = [], [], base
    while d <= date.today():
        if d.weekday() < 5:
            v *= 1 + drift + rnd.gauss(0, vol) + 0.004 * math.sin(len(cl) / 18) + (0.02 * (1 - v / base) if flat else 0)
            dates.append(d.isoformat())
            cl.append(v)
        d += timedelta(days=1)
    return {"d": dates, "c": cl, "currency": "TWD" if sym.endswith(".TW") or sym == "^TWII" else "USD"}


def demo_news():
    now = int(time.time())

    def n(t, h, src="示範新聞"):
        return {"title": "【示範】" + t, "source": src, "link": "https://news.google.com", "ts": now - h * 3600}

    world = [
        n("伊朗與美國緊張升級 雙方互相警告將發動攻擊", 1), n("美軍增兵中東 伊朗稱若遭攻擊將全面報復", 3),
        n("伊朗外長：飛彈部隊進入最高戒備", 6), n("胡塞組織再以飛彈襲擊紅海商船", 2),
        n("胡塞無人機攻擊沙烏地機場 造成數人傷亡", 4), n("沙國攔截胡塞飛彈 機場一度關閉", 9),
        n("法國退休改革抗議擴大 上千人被捕", 2), n("巴黎示威衝突升級 警方動用催淚瓦斯", 5),
        n("歐洲多國農民抗議 封鎖高速公路", 8), n("剛果伊波拉疫情擴散 新增病例", 3),
        n("世衛：伊波拉疫情蔓延至鄰國", 7), n("烏克蘭與俄羅斯同意局部停火談判", 4),
        n("俄烏談判下週續行 停火範圍仍有分歧", 10), n("以色列空襲加薩南部 至少數十人死亡", 3),
        n("加薩停火協議破局 以色列恢復攻擊", 6), n("共艦繞台 國防部嚴密監控", 5),
        n("解放軍台海周邊軍演 共機逾三十架次", 8), n("蘇丹內戰持續 難民湧入查德", 9),
        n("蘇丹叛軍攻擊城市 平民傷亡慘重", 12), n("塞爾維亞提前大選 執政黨面臨挑戰", 7),
        n("菲律賓南海補給船遭水砲 雙方再起衝突", 11), n("日本東北外海地震 規模6.8", 6),
    ]
    ai_market = [n("Meta 財報後重挫 市場質疑 AI 支出回報", 2)]
    models = [n("某 AI 公司發表新一代旗艦模型 程式能力大幅提升", 5),
              n("設計平台推出自家生圖模型 支援 2K 解析度", 20)]
    money = [n("AI 新創完成大型融資 估值創新高", 3), n("雲端巨頭上調 AI 資本支出預估", 8)]
    tw = [n("台股終場下跌 權值股走弱", 4)]
    macro = [n("Fed 本週利率決議 市場預期按兵不動", 3)]
    return world, ai_market, models, money, tw, macro


def demo_earnings(cfg):
    today = datetime.now(TPE).date()
    picks = [("GOOGL", "Alphabet", 3), ("MSFT", "微軟", 3), ("AMZN", "亞馬遜", 4), ("AAPL", "蘋果", 8), ("AMD", "AMD", 9)]
    return [{"date": (today + timedelta(days=dd)).isoformat(), "sym": s, "name": nm} for s, nm, dd in picks]


if __name__ == "__main__":
    sys.exit(main())
