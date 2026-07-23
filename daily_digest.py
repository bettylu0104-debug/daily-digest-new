# -*- coding: utf-8 -*-
"""
每日晨報機器人 (Daily Digest Bot)
----------------------------------
每天由 GitHub Actions 自動執行一次，會：
  1. 抓取台股/美股/總經/AI/新創新聞 RSS
  2. 抓取台股成交量排行、類股漲跌、美股大盤、台積電ADR (免費資料源，不需金鑰)
  3. 把原始資料丟給 Google Gemini，整理成人性化中文晨報
  4. 產生一個好看的網頁 (docs/index.html)，並透過 GitHub Pages 免費發佈
  5. 用 Bark 推播一則通知到你的 iPhone，點開就會連到那個網頁

完全不需要付費（Gemini 在免費額度內、GitHub Actions/Pages 免費、Bark 免費）。
如果某個新聞來源當天抓不到，程式會自動略過，不會讓整支程式當掉。
"""

import os
import sys
import json
import datetime
import traceback
import itertools
import html as html_lib
import re
from urllib.parse import quote
from email.utils import parsedate_to_datetime

import requests
import feedparser

NEWS_ID_COUNTER = itertools.count(1)


def google_news_rss(query: str) -> str:
    """透過 Google 新聞的 RSS 搜尋，取得指定網站/關鍵字的新聞。
    這是 Google 公開提供的功能，不需要申請金鑰，也不受個別網站自己
    有沒有做 RSS 的限制，可以用來抓鉅亨網、數位時代這類沒有穩定 RSS 的網站。
    語法：site:網域 可以限定只抓某個網站；when:2d 限定只抓最近兩天的新聞。
    """
    return f"https://news.google.com/rss/search?q={quote(query)}&hl=zh-TW&gl=TW&ceid=TW:zh-Hant"

# ============================================================
# 1. 設定區：新聞來源清單
#    如果之後想增加/替換來源，只要改這裡的網址即可，不用動其他程式碼
# ============================================================

# 新聞策略：
# - Yahoo / 鉅亨：透過 Google News RSS 的 site: 限定，避免依賴不穩定的站內 RSS。
# - 官方 AI / VC 媒體：有穩定 RSS 的直接用 RSS；其餘透過 Google News RSS 限定官方網域。
# - when:1d 用來鎖定近一天；程式端還會再做時間過濾與去重。
RSS_SOURCES = {
    "台股": [
        ("Yahoo奇摩股市-台股個股", google_news_rss("site:tw.stock.yahoo.com 台股 個股 when:1d")),
        ("Yahoo奇摩股市-台灣科技", google_news_rss("site:tw.stock.yahoo.com (科技 OR 半導體 OR 電子 OR AI OR 伺服器 OR 晶片) when:1d")),
        ("鉅亨網-台股個股", google_news_rss("site:cnyes.com 台股 個股 when:1d")),
        ("鉅亨網-台灣科技", google_news_rss("site:cnyes.com (台股 OR 台灣) (科技 OR 半導體 OR 電子 OR AI OR 伺服器 OR 晶片) when:1d")),
    ],
    "美股": [
        ("Yahoo Finance-美股個股", google_news_rss("site:finance.yahoo.com stocks company shares when:1d")),
        ("Yahoo Finance-美國科技", google_news_rss("site:finance.yahoo.com (technology OR semiconductor OR AI OR chip OR cloud) stocks when:1d")),
        ("鉅亨網-美股個股", google_news_rss("site:cnyes.com 美股 個股 when:1d")),
        ("鉅亨網-美國科技", google_news_rss("site:cnyes.com 美股 (科技 OR 半導體 OR AI OR 晶片 OR 雲端 OR 伺服器) when:1d")),
    ],
    "AI": [
        ("TechCrunch-AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("VentureBeat-AI", "https://venturebeat.com/category/ai/feed/"),
        ("Reuters-AI", google_news_rss("site:reuters.com artificial intelligence AI when:1d")),
        ("OpenAI-News", google_news_rss("site:openai.com/news when:1d")),
        ("Anthropic-News", google_news_rss("site:anthropic.com/news when:1d")),
        ("Google-DeepMind", google_news_rss("site:deepmind.google/blog when:1d")),
        ("Meta-AI", google_news_rss("site:ai.meta.com/blog when:1d")),
        ("NVIDIA-AI", google_news_rss("site:blogs.nvidia.com AI when:1d")),
        ("NVIDIA-Newsroom-AI", google_news_rss("site:nvidianews.nvidia.com AI when:1d")),
        ("SemiAnalysis", google_news_rss("site:semianalysis.com when:1d")),
        ("Y Combinator-AI", google_news_rss("site:ycombinator.com/blog (AI OR artificial intelligence OR LLM OR model) when:1d")),
    ],
    "新創": [
        ("Y Combinator", google_news_rss("site:ycombinator.com/blog when:1d")),
        ("創業小聚", google_news_rss("site:meet.bnext.com.tw when:1d")),
        ("數位時代", google_news_rss("site:bnext.com.tw (新創 OR 創業 OR startup) when:1d")),
        ("a16z", google_news_rss("site:a16z.com when:1d")),
        ("Sequoia Capital", google_news_rss("site:sequoiacap.com when:1d")),
    ],
}

# 總經只取鉅亨網。拆成多個 query，增加涵蓋率，再由程式去重。
MACRO_KEYWORDS_SOURCES = [
    ("鉅亨網-總經", google_news_rss("site:cnyes.com (聯準會 OR Fed OR 央行 OR 利率 OR 降息 OR 升息) when:1d")),
    ("鉅亨網-總經", google_news_rss("site:cnyes.com (CPI OR PCE OR 非農 OR GDP OR PMI OR 通膨 OR 就業) when:1d")),
    ("鉅亨網-總經", google_news_rss("site:cnyes.com (美元 OR 美債 OR 殖利率 OR 關稅 OR 匯率 OR 原油 OR 黃金) when:1d")),
]


TIMEZONE_OFFSET_HOURS = 8  # 台北時間 UTC+8
NEWS_LOOKBACK_HOURS = int(os.environ.get("NEWS_LOOKBACK_HOURS", "30"))  # 晨報預設抓近30小時，涵蓋美股前一交易日

# 模擬真實瀏覽器的請求標頭，避免部分網站把伺服器的請求誤判為機器人而拒絕回應
BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "application/rss+xml, application/xml, text/xml, */*",
}


# ============================================================
# 2. 抓 RSS 新聞（任何一個來源失敗都不會讓程式中斷）
# ============================================================

def _entry_datetime(entry):
    """盡量解析 RSS/Atom 的發布時間，回傳 aware UTC datetime；解析不到則回 None。"""
    for key in ("published", "updated", "created"):
        value = entry.get(key)
        if not value:
            continue
        try:
            dt = parsedate_to_datetime(value)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=datetime.timezone.utc)
            return dt.astimezone(datetime.timezone.utc)
        except Exception:
            pass

    # feedparser 有時提供 *_parsed time_struct
    for key in ("published_parsed", "updated_parsed", "created_parsed"):
        value = entry.get(key)
        if value:
            try:
                return datetime.datetime(*value[:6], tzinfo=datetime.timezone.utc)
            except Exception:
                pass
    return None


def fetch_rss_category(sources, max_per_source=50, lookback_hours=NEWS_LOOKBACK_HOURS):
    """抓分類新聞、去重，並優先保留近 lookback_hours 小時的項目。

    注意：Google News 的 site: 搜尋與 RSS 無法保證枚舉網站「絕對全部」文章，
    但此設計會把每個指定來源能抓到的近一天結果盡量完整保留，而不是先讓 LLM 刪減。
    """
    items = []
    seen = set()
    now_utc = datetime.datetime.now(datetime.timezone.utc)
    cutoff = now_utc - datetime.timedelta(hours=lookback_hours)

    for source_name, url in sources:
        try:
            resp = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
            resp.raise_for_status()
            feed = feedparser.parse(resp.content)
            if not feed.entries:
                print(f"[警告] 來源可能失效或格式不符，略過：{source_name} ({url})")
                continue

            source_added = 0
            for entry in feed.entries[:max_per_source]:
                title = entry.get("title", "").strip()
                link = entry.get("link", "").strip()
                if not title or not link:
                    continue

                dt = _entry_datetime(entry)
                if dt is not None and dt < cutoff:
                    continue

                # 同一篇被不同 query 抓到時避免重複
                dedupe_key = (re.sub(r"\s+", " ", title).lower(), link)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                items.append({
                    "id": next(NEWS_ID_COUNTER),
                    "source": source_name,
                    "title": title,
                    "link": link,
                    "published": entry.get("published", entry.get("updated", "")),
                    "summary": (entry.get("summary", "") or "")[:500],
                })
                source_added += 1

            print(f"[新聞抓取] {source_name}: 新增 {source_added} 則")
        except Exception as e:
            print(f"[警告] 抓取失敗，略過：{source_name} -> {e}")
            continue

    return items


# ============================================================
# 3. 抓台股大盤數據（成交量排行、類股漲跌）－ 台灣證券交易所 OpenAPI，免金鑰
# ============================================================

def fetch_twse_top_volume(top_n=5):
    """抓昨天的成交量排行前 N 名個股"""
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/STOCK_DAY_ALL"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        # 依成交股數排序
        cleaned = []
        for row in data:
            try:
                volume = int(str(row.get("TradeVolume", "0")).replace(",", ""))
                cleaned.append({
                    "code": row.get("Code"),
                    "name": row.get("Name"),
                    "volume": volume,
                    "close": row.get("ClosingPrice"),
                    "change": row.get("Change"),
                })
            except Exception:
                continue
        cleaned.sort(key=lambda x: x["volume"], reverse=True)
        return cleaned[:top_n]
    except Exception as e:
        print(f"[警告] 台股成交量資料抓取失敗：{e}")
        return []


def fetch_twse_hot_sector(top_n=3):
    """抓昨天漲幅最高的類股指數"""
    try:
        url = "https://openapi.twse.com.tw/v1/exchangeReport/MI_INDEX"
        resp = requests.get(url, timeout=15)
        resp.raise_for_status()
        data = resp.json()
        cleaned = []
        for row in data:
            name = row.get("指數") or row.get("Index")
            change_pct = row.get("漲跌百分比") or row.get("ChangePercent")
            if name and change_pct:
                try:
                    cleaned.append({"sector": name, "change_pct": float(change_pct)})
                except Exception:
                    continue
        cleaned.sort(key=lambda x: x["change_pct"], reverse=True)
        return cleaned[:top_n]
    except Exception as e:
        print(f"[警告] 類股資料抓取失敗：{e}")
        return []


# ============================================================
# 4. 抓美股大盤 / 台積電ADR － 用 yfinance，免金鑰
# ============================================================

def fetch_market_quotes():
    """抓台股大盤、美股三大指數、台積電ADR的漲跌幅"""
    import yfinance as yf

    tickers = {
        "台股加權指數": "^TWII",
        "台積電ADR (TSM)": "TSM",
        "道瓊工業指數": "^DJI",
        "S&P 500": "^GSPC",
        "那斯達克指數": "^IXIC",
        "費城半導體指數": "^SOX",
    }
    result = {}
    for label, symbol in tickers.items():
        try:
            t = yf.Ticker(symbol)
            hist = t.history(period="5d")
            if len(hist) >= 2:
                prev_close = hist["Close"].iloc[-2]
                last_close = hist["Close"].iloc[-1]
                pct = (last_close - prev_close) / prev_close * 100
                result[label] = {
                    "close": round(float(last_close), 2),
                    "change_pct": round(float(pct), 2),
                }
        except Exception as e:
            print(f"[警告] 行情抓取失敗 {label}: {e}")
    return result


# ============================================================
# 4.5 抓 Google 日曆今天的行程（跟語音記行程共用同一組 OAuth 憑證）
# ============================================================

CAL_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID", "")
CAL_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET", "")
CAL_REFRESH_TOKEN = os.environ.get("GOOGLE_REFRESH_TOKEN", "")
CAL_CALENDAR_ID = os.environ.get("GOOGLE_CALENDAR_ID", "primary")


def fetch_todays_events(now_tw: datetime.datetime):
    """抓今天（台北時間）的 Google Calendar 行程。

    會先用 refresh token 換 access token，再讀取使用者 Calendar List，
    並合併所有「已選取/可見」且可讀取的日曆，而不是只讀 primary。
    這比較接近 Google Calendar 網頁上你實際看到的「我的行程」。
    """
    missing = []
    if not CAL_CLIENT_ID:
        missing.append("GOOGLE_CLIENT_ID")
    if not CAL_CLIENT_SECRET:
        missing.append("GOOGLE_CLIENT_SECRET")
    if not CAL_REFRESH_TOKEN:
        missing.append("GOOGLE_REFRESH_TOKEN")

    if missing:
        print(f"[日曆錯誤] 缺少 GitHub Actions 環境變數：{', '.join(missing)}")
        print("[日曆錯誤] Python 本身無法讀取 GitHub Secret；workflow 必須把 Secrets 放進 env。")
        return []

    try:
        print("[日曆] 已讀取 OAuth 環境變數，開始交換 Access Token")

        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": CAL_CLIENT_ID,
                "client_secret": CAL_CLIENT_SECRET,
                "refresh_token": CAL_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=20,
        )
        if token_resp.status_code != 200:
            raise RuntimeError(
                f"Refresh Token 換取 Access Token 失敗，HTTP {token_resp.status_code}: "
                f"{token_resp.text[:800]}"
            )

        access_token = token_resp.json().get("access_token")
        if not access_token:
            raise RuntimeError("Google Token API 沒有回傳 access_token")

        headers = {"Authorization": f"Bearer {access_token}"}
        tz_tw = datetime.timezone(datetime.timedelta(hours=TIMEZONE_OFFSET_HOURS))

        if now_tw.tzinfo is None:
            now_tw_aware = now_tw.replace(tzinfo=tz_tw)
        else:
            now_tw_aware = now_tw.astimezone(tz_tw)

        day_start = now_tw_aware.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + datetime.timedelta(days=1)

        # 先確認 primary calendar，方便 GitHub Actions log 判斷 OAuth 到底綁哪個帳號
        primary_resp = requests.get(
            "https://www.googleapis.com/calendar/v3/calendars/primary",
            headers=headers,
            timeout=20,
        )
        if primary_resp.status_code == 200:
            primary_meta = primary_resp.json()
            print(
                "[日曆] OAuth 已成功連線。Primary Calendar："
                f"id={primary_meta.get('id', '(unknown)')} / "
                f"summary={primary_meta.get('summary', '(unknown)')} / "
                f"timeZone={primary_meta.get('timeZone', '(unknown)')}"
            )
        else:
            raise RuntimeError(
                f"無法讀取 primary calendar，HTTP {primary_resp.status_code}: "
                f"{primary_resp.text[:800]}"
            )

        # 讀取帳號中的 calendar list。Google Calendar UI 可能同時顯示多個日曆，
        # 所以只抓 primary 會漏掉「其他日曆」中的行程。
        calendars = []
        page_token = None
        while True:
            params = {"maxResults": 250}
            if page_token:
                params["pageToken"] = page_token

            list_resp = requests.get(
                "https://www.googleapis.com/calendar/v3/users/me/calendarList",
                headers=headers,
                params=params,
                timeout=20,
            )
            if list_resp.status_code != 200:
                raise RuntimeError(
                    f"Calendar List API 失敗，HTTP {list_resp.status_code}: "
                    f"{list_resp.text[:800]}"
                )

            payload = list_resp.json()
            calendars.extend(payload.get("items", []))
            page_token = payload.get("nextPageToken")
            if not page_token:
                break

        # 優先讀取使用者在 Google Calendar UI 中「已選取」的日曆。
        selected_calendars = [
            cal for cal in calendars
            if cal.get("selected", False)
            and cal.get("accessRole") in ("reader", "writer", "owner", "freeBusyReader")
        ]

        # 如果 API 沒有任何 selected=true，至少抓 primary，避免整段空掉。
        if not selected_calendars:
            selected_calendars = [
                cal for cal in calendars
                if cal.get("primary", False)
            ]

        print(
            "[日曆] 將讀取以下日曆："
            + ", ".join(
                f"{cal.get('summary', '(無名稱)')}<{cal.get('id', '')}>"
                for cal in selected_calendars
            )
        )

        merged_events = []
        seen = set()

        for cal in selected_calendars:
            cal_id = cal.get("id")
            if not cal_id:
                continue

            events_resp = requests.get(
                f"https://www.googleapis.com/calendar/v3/calendars/{quote(cal_id, safe='')}/events",
                headers=headers,
                params={
                    "timeMin": day_start.isoformat(),
                    "timeMax": day_end.isoformat(),
                    "singleEvents": "true",
                    "orderBy": "startTime",
                    "timeZone": "Asia/Taipei",
                    "maxResults": 250,
                },
                timeout=20,
            )

            if events_resp.status_code != 200:
                print(
                    f"[日曆警告] 無法讀取 {cal.get('summary', cal_id)}："
                    f"HTTP {events_resp.status_code} {events_resp.text[:400]}"
                )
                continue

            for item in events_resp.json().get("items", []):
                # 略過已取消事件
                if item.get("status") == "cancelled":
                    continue

                start_obj = item.get("start", {})
                start_str = start_obj.get("dateTime", start_obj.get("date", ""))
                time_label = "整天"

                if "dateTime" in start_obj:
                    dt = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                    time_label = dt.astimezone(tz_tw).strftime("%H:%M")

                dedupe_key = (
                    item.get("iCalUID") or item.get("id"),
                    start_str,
                )
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)

                merged_events.append({
                    "time": time_label,
                    "title": item.get("summary", "(無標題)"),
                    "location": item.get("location", ""),
                    "calendar": cal.get("summary", ""),
                    "_sort": start_str,
                })

        merged_events.sort(key=lambda x: x.get("_sort", ""))
        for event in merged_events:
            event.pop("_sort", None)

        print(f"[日曆] 今日共抓到 {len(merged_events)} 筆行程")
        return merged_events

    except Exception as e:
        print(f"[日曆錯誤] 日曆行程抓取失敗：{e}")
        traceback.print_exc()
        return []


# ============================================================
# 5. 呼叫 Google Gemini，把原始資料整理成人性化晨報
#    Gemini API 有免費額度，一天一次遠遠用不完
#    免費申請金鑰：https://aistudio.google.com/apikey
# ============================================================

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 如果之後 Google 又更新了免費模型名稱，去 https://ai.google.dev/gemini-api/docs/models 查最新的免費模型名稱替換掉下面這行
# 2026/7 更新：gemini-2.5-flash 已對新用戶關閉，改用 Google 這次(7/21)發布的新模型
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite")


def call_gemini(raw_data: dict) -> dict:
    """把原始資料交給 Gemini，請它輸出結構化 JSON 晨報

    設計重點：新聞的「標題/連結/來源」一律使用原始資料裡的 id 對應回去，
    不讓 Gemini 自己重新輸出網址文字，避免 AI 抄錯連結導致點進去打不開。
    Gemini 只需要負責寫「摘要」跟少數需要它綜合判斷的整理句子。
    """
    if not GEMINI_API_KEY:
        raise RuntimeError("找不到 GEMINI_API_KEY，請確認 GitHub Secrets 有設定")

    system_instruction = """
你是使用者的私人財經與科技新聞助理，語氣親切、像信任的朋友在跟他簡報，但內容要專業精準，不浮誇。
我提供的原始資料裡，每一則新聞都有一個數字 "id"。
請根據原始資料，整理成「今日晨報」，並且【只能輸出 JSON，不要有任何其他文字、不要markdown code fence】，格式如下：

{
  "greeting": "一句今天的簡短問候語，可以提到天氣感或市場氣氛，20字內",
  "taiwan_stocks": {
    "night_futures_note": "根據原始資料整理台指期夜盤漲跌重點，若無資料請寫'今日無夜盤資料，建議至台灣期貨交易所官網查看'",
    "top_volume_note": "用一句話總結成交量最大的個股是誰、量有多大",
    "hot_sector_note": "用一句話總結最熱的類股是誰、漲了多少",
    "news": [ {"id": 原始資料的id數字, "summary": "20-40字重點摘要"} ... 從 taiwan_stock_news 中保留所有與個股或科技產業直接相關的當日新聞 ]
  },
  "us_stocks": {
    "market_summary": "用1-2句話總結美股大盤走勢",
    "tsm_adr_note": "台積電ADR漲跌幅一句話重點",
    "news": [ {"id": ..., "summary": "..."} ... 從 us_stock_news 中保留所有與個股或科技產業直接相關的當日新聞 ]
  },
  "macro": [ {"id": ..., "summary": "..."} ... 只從 macro_news 整理所有當日重要總經新聞，不要使用 us_stock_news ],
  "ai": [ {"id": ..., "summary": "..."} ... 從 ai_news 整理所有當日重要更新，優先保留官方模型/產品/研究發布與 Reuters 重大報導 ],
  "startup": [ {"id": ..., "summary": "..."} ... 從 startup_news 整理所有當日官方新創/VC更新 ],
  "closing_note": "一句鼓勵/提醒的結語，15字內"
}

規則：
- id 一定要是原始資料裡真實存在的數字，不可以自己編號。
- 新聞條數不夠就盡量整理現有的，不用湊數，不要編造不存在的新聞。
- 台股與美股：不要只挑少數新聞；只要與上市個股或科技產業直接相關，就應保留。
- 總經：只能使用 macro_news，macro_news 本身只包含鉅亨網來源。
- AI：來源要多元，包含 TechCrunch、VentureBeat、Reuters、OpenAI、Anthropic、Google DeepMind、Meta AI、NVIDIA、SemiAnalysis、Y Combinator。
- 新創：若 YC、創業小聚、數位時代、a16z、Sequoia 當日有資料，至少保留該來源一則。
- 台股/美股新聞挑當天最重要、對投資人最有意義的。
- 若 taiwan_stock_news 中存在 source 以「鉅亨網」開頭的新聞，台股 news 至少選 1 則鉅亨網。
- 若 us_stock_news 中存在 source 以「鉅亨網」開頭的新聞，美股 news 至少選 1 則鉅亨網。
- macro_news 若有資料，macro 優先選 macro_news，且至少選 1 則鉅亨網總經新聞。
"""

    user_content = json.dumps(raw_data, ensure_ascii=False)

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "system_instruction": {"parts": [{"text": system_instruction}]},
        "contents": [{"parts": [{"text": f"以下是今天的原始資料：\n{user_content}"}]}],
        "generationConfig": {
            "temperature": 0.4,
            "response_mime_type": "application/json",
        },
    }
    resp = requests.post(url, json=payload, timeout=90)
    if resp.status_code != 200:
        # 印出 Google 實際回傳的錯誤原因，方便除錯（金鑰問題/格式問題等都會在這裡看到）
        raise RuntimeError(f"Gemini API 回應 {resp.status_code}：{resp.text[:1000]}")
    data = resp.json()
    text = data["candidates"][0]["content"]["parts"][0]["text"]
    return json.loads(text)


def resolve_news_items(gemini_items, *source_lists):
    """把 Gemini 回傳的 {id, summary} 對應回原始資料，補上真正的 title/link/source，
    這樣連結一定是原始資料裡真實存在的網址，不會被 AI 抄錯。"""
    by_id = {}
    for source_list in source_lists:
        for item in source_list:
            by_id[item["id"]] = item

    resolved = []
    for g_item in gemini_items or []:
        original = by_id.get(g_item.get("id"))
        if not original:
            continue  # Gemini 給了不存在的 id，直接跳過，不要顯示壞掉的項目
        resolved.append({
            "title": original["title"],
            "summary": g_item.get("summary", ""),
            "source": original["source"],
            "link": original["link"],
        })
    return resolved


def _plain_text(value: str) -> str:
    """把 RSS 原始摘要清成可直接顯示的短文字。"""
    value = value or ""
    value = re.sub(r"<[^>]+>", " ", value)
    value = html_lib.unescape(value)
    value = re.sub(r"\s+", " ", value).strip()
    return value[:120]


def ensure_source_coverage(resolved_items, raw_items, required_source_prefixes, max_items=10):
    """如果 Gemini 沒挑到指定來源，就從原始 RSS 補一則，避免整個來源被 AI 選文邏輯淘汰。"""
    result = list(resolved_items or [])
    existing_sources = {item.get("source", "") for item in result}

    for prefix in required_source_prefixes:
        if any(src.startswith(prefix) for src in existing_sources):
            continue

        candidate = next(
            (item for item in raw_items if item.get("source", "").startswith(prefix)),
            None,
        )
        if candidate:
            fallback_summary = _plain_text(candidate.get("summary", ""))
            if not fallback_summary:
                fallback_summary = candidate.get("title", "")
            result.append({
                "title": candidate.get("title", ""),
                "summary": fallback_summary,
                "source": candidate.get("source", ""),
                "link": candidate.get("link", ""),
            })
            existing_sources.add(candidate.get("source", ""))

    return result[:max_items]


def ensure_minimum_per_source(resolved_items, raw_items, min_per_source=5, max_total=200):
    """確保每個實際抓到新聞的來源，最終頁面至少保留 min_per_source 篇。

    若某來源在指定時間範圍內本來就少於 min_per_source 篇，則全部保留，
    不會為了湊數而捏造不存在的新聞。
    """
    result = list(resolved_items or [])

    # 已經顯示的標題，避免重複
    shown_keys = {
        (item.get("source", ""), re.sub(r"\s+", " ", item.get("title", "")).strip().lower())
        for item in result
    }

    # 依 raw_items 中實際存在的來源分組
    source_groups = {}
    for item in raw_items or []:
        source_groups.setdefault(item.get("source", ""), []).append(item)

    for source_name, candidates in source_groups.items():
        current_count = sum(1 for item in result if item.get("source") == source_name)
        needed = max(0, min(min_per_source, len(candidates)) - current_count)

        if needed <= 0:
            continue

        for candidate in candidates:
            key = (
                candidate.get("source", ""),
                re.sub(r"\s+", " ", candidate.get("title", "")).strip().lower(),
            )
            if key in shown_keys:
                continue

            fallback_summary = _plain_text(candidate.get("summary", ""))
            if not fallback_summary:
                fallback_summary = candidate.get("title", "")

            result.append({
                "title": candidate.get("title", ""),
                "summary": fallback_summary,
                "source": candidate.get("source", ""),
                "link": candidate.get("link", ""),
            })
            shown_keys.add(key)
            needed -= 1

            if needed <= 0 or len(result) >= max_total:
                break

    return result[:max_total]


# ============================================================
# 6. 產生晨報網頁 (Vogue / WSJ 風格：襯線標題字 + 極簡黑白配色)
# ============================================================

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>{date_str} 晨報</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Playfair+Display:ital,wght@0,500;0,700;1,500&family=Noto+Serif+TC:wght@400;600;700&family=Inter:wght@300;400;500;600&display=swap" rel="stylesheet">
<style>
  :root {{
    --ink: #1a1a1a;
    --paper: #faf9f6;
    --line: #d8d5cd;
    --accent: #a6772c;
    --muted: #6b6b66;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0;
    background: var(--paper);
    color: var(--ink);
    font-family: 'Inter', 'Noto Serif TC', sans-serif;
    line-height: 1.7;
  }}
  .masthead {{
    text-align: center;
    padding: 48px 24px 24px;
    border-bottom: 3px solid var(--ink);
  }}
  .masthead .date {{
    letter-spacing: 3px;
    font-size: 12px;
    color: var(--muted);
    text-transform: uppercase;
  }}
  .masthead h1 {{
    font-family: 'Playfair Display', 'Noto Serif TC', serif;
    font-size: 42px;
    margin: 8px 0 4px;
    letter-spacing: 1px;
  }}
  .greeting {{
    max-width: 700px;
    margin: 32px auto 8px;
    padding: 0 24px;
    font-style: italic;
    color: var(--muted);
    text-align: center;
    font-size: 16px;
  }}
  .filter-bar {{
    position: sticky;
    top: 0;
    z-index: 10;
    background: var(--paper);
    display: flex;
    justify-content: center;
    flex-wrap: wrap;
    gap: 10px;
    padding: 20px 16px 16px;
    border-bottom: 1px solid var(--line);
  }}
  .filter-btn {{
    font-family: 'Inter', sans-serif;
    font-size: 13px;
    letter-spacing: 1px;
    padding: 8px 18px;
    border: 1px solid var(--ink);
    border-radius: 20px;
    background: transparent;
    color: var(--ink);
    cursor: pointer;
    transition: all 0.15s ease;
  }}
  .filter-btn:hover {{ background: rgba(0,0,0,0.05); }}
  .filter-btn.active {{
    background: var(--ink);
    color: var(--paper);
  }}
  .container {{ max-width: 760px; margin: 0 auto; padding: 24px; }}
  section {{ margin-bottom: 48px; scroll-margin-top: 90px; }}
  section.hidden {{ display: none; }}
  section h2 {{
    font-family: 'Playfair Display', 'Noto Serif TC', serif;
    font-size: 24px;
    border-bottom: 1px solid var(--line);
    padding-bottom: 10px;
    margin-bottom: 18px;
    display: flex;
    align-items: baseline;
    gap: 10px;
  }}
  section h2 .en {{
    font-size: 12px;
    letter-spacing: 2px;
    color: var(--accent);
    text-transform: uppercase;
    font-family: 'Inter', sans-serif;
  }}
  .highlight-box {{
    background: #fff;
    border: 1px solid var(--line);
    padding: 16px 20px;
    margin-bottom: 20px;
    font-size: 14px;
  }}
  .highlight-box div {{ margin-bottom: 6px; }}
  .highlight-box b {{ color: var(--accent); }}
  ul.news-list {{ list-style: none; margin: 0; padding: 0; }}
  ul.news-list li {{
    padding: 14px 0;
    border-bottom: 1px dashed var(--line);
  }}
  ul.news-list li:last-child {{ border-bottom: none; }}
  .news-title {{ font-weight: 600; font-size: 16px; }}
  .news-title a {{ color: var(--ink); text-decoration: none; }}
  .news-title a:hover {{ color: var(--accent); }}
  .news-summary {{ color: var(--muted); font-size: 14px; margin-top: 4px; }}
  .news-source {{ font-size: 11px; color: var(--accent); text-transform: uppercase; letter-spacing: 1px; margin-top: 4px; display: inline-block; }}
  ul.task-list {{ list-style: none; margin: 0; padding: 0; }}
  ul.task-list li {{
    display: flex;
    gap: 18px;
    padding: 14px 0;
    border-bottom: 1px dashed var(--line);
    align-items: baseline;
    cursor: pointer;
    user-select: none;
    transition: opacity 0.2s ease;
  }}
  ul.task-list li:last-child {{ border-bottom: none; }}
  ul.task-list li.done {{ opacity: 0.4; }}
  ul.task-list li.done .task-title {{ text-decoration: line-through; }}
  .task-time {{
    font-family: 'Playfair Display', serif;
    font-weight: 700;
    color: var(--accent);
    min-width: 64px;
    font-size: 15px;
  }}
  .task-check {{
    width: 18px;
    height: 18px;
    border: 1.5px solid var(--ink);
    border-radius: 50%;
    flex-shrink: 0;
    margin-top: 3px;
    position: relative;
  }}
  ul.task-list li.done .task-check::after {{
    content: "";
    position: absolute;
    left: 4px;
    top: 1px;
    width: 5px;
    height: 9px;
    border-right: 2px solid var(--accent);
    border-bottom: 2px solid var(--accent);
    transform: rotate(45deg);
  }}
  .task-title {{ font-weight: 600; font-size: 16px; }}
  .task-location {{ color: var(--muted); font-size: 13px; margin-left: 8px; }}
  .no-task {{ color: var(--muted); font-size: 14px; font-style: italic; padding: 8px 0; }}
  .task-card {{
    background: #fff;
    border: 1px solid var(--line);
    border-left: 4px solid var(--accent);
    padding: 26px 28px;
    margin: 32px auto 0;
    max-width: 712px;
  }}
  .task-card-title {{
    font-family: 'Playfair Display', 'Noto Serif TC', serif;
    font-size: 22px;
    margin: 0 0 16px;
    display: flex;
    align-items: baseline;
    gap: 10px;
  }}
  .task-card-title .en {{
    font-size: 11px;
    letter-spacing: 2px;
    color: var(--accent);
    text-transform: uppercase;
    font-family: 'Inter', sans-serif;
  }}
  footer {{
    text-align: center;
    padding: 32px 24px 60px;
    color: var(--muted);
    font-size: 13px;
    border-top: 1px solid var(--line);
  }}
</style>
</head>
<body>
  <div class="masthead">
    <div class="date">{weekday_str} · {date_str}</div>
    <h1>每日晨報</h1>
  </div>
  <div class="greeting">{greeting}</div>

  <div class="container">
    <div class="task-card" id="taskCard">
      <h2 class="task-card-title">今日任務 <span class="en">Today's Agenda</span></h2>
      {tasks_html}
    </div>
  </div>

  <div class="filter-bar" id="filterBar">
    <button class="filter-btn active" data-target="all">全部</button>
    <button class="filter-btn" data-target="tw">台股</button>
    <button class="filter-btn" data-target="us">美股</button>
    <button class="filter-btn" data-target="macro">總經</button>
    <button class="filter-btn" data-target="ai">AI</button>
    <button class="filter-btn" data-target="startup">新創</button>
  </div>

  <div class="container">

    <section data-category="tw">
      <h2>台股 <span class="en">Taiwan Market</span></h2>
      <div class="highlight-box">
        <div><b>台指期夜盤　</b>{night_futures_note}</div>
        <div><b>成交量王　</b>{top_volume_note}</div>
        <div><b>最熱類股　</b>{hot_sector_note}</div>
      </div>
      <ul class="news-list">{tw_news_html}</ul>
    </section>

    <section data-category="us">
      <h2>美股 <span class="en">US Market</span></h2>
      <div class="highlight-box">
        <div><b>大盤走勢　</b>{market_summary}</div>
        <div><b>台積電ADR　</b>{tsm_adr_note}</div>
      </div>
      <ul class="news-list">{us_news_html}</ul>
    </section>

    <section data-category="macro">
      <h2>總體經濟 <span class="en">Macro</span></h2>
      <ul class="news-list">{macro_news_html}</ul>
    </section>

    <section data-category="ai">
      <h2>AI <span class="en">Artificial Intelligence</span></h2>
      <ul class="news-list">{ai_news_html}</ul>
    </section>

    <section data-category="startup">
      <h2>新創 <span class="en">Startups</span></h2>
      <ul class="news-list">{startup_news_html}</ul>
    </section>

  </div>
  <footer>{closing_note}<br><br>由你的私人助理自動整理 · {date_str}</footer>

  <script>
    (function () {{
      var buttons = document.querySelectorAll('.filter-btn');
      var sections = document.querySelectorAll('section[data-category]');
      buttons.forEach(function (btn) {{
        btn.addEventListener('click', function () {{
          buttons.forEach(function (b) {{ b.classList.remove('active'); }});
          btn.classList.add('active');
          var target = btn.getAttribute('data-target');
          sections.forEach(function (sec) {{
            if (target === 'all' || sec.getAttribute('data-category') === target) {{
              sec.classList.remove('hidden');
            }} else {{
              sec.classList.add('hidden');
            }}
          }});
        }});
      }});

      // 任務打勾：點擊切換完成狀態，並記在這台裝置的瀏覽器裡（換一天產生新頁面會自動重置）
      var storageKey = 'daily-digest-tasks-{date_str}';
      var doneIds = [];
      try {{
        doneIds = JSON.parse(localStorage.getItem(storageKey) || '[]');
      }} catch (e) {{ doneIds = []; }}

      var taskItems = document.querySelectorAll('#taskList li');
      function applyDoneState() {{
        taskItems.forEach(function (li) {{
          var id = li.getAttribute('data-task-id');
          if (doneIds.indexOf(id) !== -1) {{
            li.classList.add('done');
          }} else {{
            li.classList.remove('done');
          }}
        }});
      }}
      applyDoneState();

      taskItems.forEach(function (li) {{
        li.addEventListener('click', function () {{
          var id = li.getAttribute('data-task-id');
          var idx = doneIds.indexOf(id);
          if (idx === -1) {{
            doneIds.push(id);
          }} else {{
            doneIds.splice(idx, 1);
          }}
          try {{ localStorage.setItem(storageKey, JSON.stringify(doneIds)); }} catch (e) {{}}
          applyDoneState();
        }});
      }});
    }})();
  </script>
</body>
</html>
"""


def render_task_list(events):
    if not events:
        return '<div class="no-task">今天沒有排定的行程，好好安排自己的時間吧。</div>'
    html = '<ul class="task-list" id="taskList">'
    for idx, ev in enumerate(events):
        loc = f'<span class="task-location">📍 {ev["location"]}</span>' if ev.get("location") else ""
        html += f"""<li data-task-id="{idx}">
          <div class="task-time">{ev.get('time','')}</div>
          <div class="task-check"></div>
          <div><div class="task-title">{ev.get('title','')}</div>{loc}</div>
        </li>"""
    html += "</ul>"
    return html


def render_news_list(items):
    html = ""
    for item in items:
        html += f"""<li>
          <div class="news-title"><a href="{item.get('link','#')}" target="_blank">{item.get('title','')}</a></div>
          <div class="news-summary">{item.get('summary','')}</div>
          <div class="news-source">{item.get('source','')}</div>
        </li>"""
    return html


def render_html(digest: dict, now: datetime.datetime, calendar_events=None) -> str:
    weekday_map = ["星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日"]
    return HTML_TEMPLATE.format(
        date_str=now.strftime("%Y/%m/%d"),
        weekday_str=weekday_map[now.weekday()],
        greeting=digest.get("greeting", ""),
        tasks_html=render_task_list(calendar_events or []),
        night_futures_note=digest["taiwan_stocks"].get("night_futures_note", ""),
        top_volume_note=digest["taiwan_stocks"].get("top_volume_note", ""),
        hot_sector_note=digest["taiwan_stocks"].get("hot_sector_note", ""),
        tw_news_html=render_news_list(digest["taiwan_stocks"].get("news", [])),
        market_summary=digest["us_stocks"].get("market_summary", ""),
        tsm_adr_note=digest["us_stocks"].get("tsm_adr_note", ""),
        us_news_html=render_news_list(digest["us_stocks"].get("news", [])),
        macro_news_html=render_news_list(digest.get("macro", [])),
        ai_news_html=render_news_list(digest.get("ai", [])),
        startup_news_html=render_news_list(digest.get("startup", [])),
        closing_note=digest.get("closing_note", ""),
    )


# ============================================================
# 7. 推播通知到 iPhone － 使用 Bark (免費 App + 免費伺服器)
#    下載 Bark App： https://apps.apple.com/app/id1403753865
# ============================================================

BARK_KEY = os.environ.get("BARK_KEY", "")
BARK_SERVER = os.environ.get("BARK_SERVER", "https://api.day.app")
PAGE_URL = os.environ.get("PAGE_URL", "")  # 你的 GitHub Pages 網址


def send_bark_notification(title: str, body: str, url: str = ""):
    if not BARK_KEY:
        print("[警告] 未設定 BARK_KEY，略過推播（僅產生網頁）")
        return
    try:
        payload = {
            "title": title,
            "body": body,
            "group": "每日晨報",
            "level": "timeSensitive",
        }
        if url:
            payload["url"] = url
        requests.post(f"{BARK_SERVER}/{BARK_KEY}", json=payload, timeout=15)
    except Exception as e:
        print(f"[警告] 推播失敗：{e}")


# ============================================================
# 主流程
# ============================================================

def main():
    now_utc = datetime.datetime.utcnow()
    now_tw = now_utc + datetime.timedelta(hours=TIMEZONE_OFFSET_HOURS)

    print("== 步驟 1/4：抓取新聞 RSS ==")
    raw_data = {
        "taiwan_stock_news": fetch_rss_category(RSS_SOURCES["台股"]),
        "us_stock_news": fetch_rss_category(RSS_SOURCES["美股"]),
        "ai_news": fetch_rss_category(RSS_SOURCES["AI"]),
        "startup_news": fetch_rss_category(RSS_SOURCES["新創"]),
        "macro_news": fetch_rss_category(MACRO_KEYWORDS_SOURCES),
    }

    # GitHub Actions 日誌直接顯示每個分類實際抓到哪些來源，避免「有設定但沒抓到」看不出原因。
    for category_key in ["taiwan_stock_news", "us_stock_news", "macro_news", "ai_news", "startup_news"]:
        source_counts = {}
        for item in raw_data[category_key]:
            source_counts[item["source"]] = source_counts.get(item["source"], 0) + 1
        print(f"[新聞來源] {category_key}: {source_counts}")

    print("== 步驟 2/4：抓取行情數據 ==")
    raw_data["twse_top_volume"] = fetch_twse_top_volume()
    raw_data["twse_hot_sector"] = fetch_twse_hot_sector()
    try:
        raw_data["market_quotes"] = fetch_market_quotes()
    except Exception as e:
        print(f"[警告] yfinance 抓取整體失敗：{e}")
        raw_data["market_quotes"] = {}

    print("== 步驟 3/4：呼叫 Gemini 整理晨報 ==")
    try:
        digest = call_gemini(raw_data)
    except Exception as e:
        print(f"[錯誤] Gemini 整理失敗：{e}")
        traceback.print_exc()
        sys.exit(1)

    # 把 Gemini 回傳的 id 對應回原始資料，補上真正可以打開的連結/標題/來源
    digest["taiwan_stocks"]["news"] = resolve_news_items(
        digest["taiwan_stocks"].get("news", []), raw_data["taiwan_stock_news"]
    )
    digest["us_stocks"]["news"] = resolve_news_items(
        digest["us_stocks"].get("news", []), raw_data["us_stock_news"]
    )
    digest["macro"] = resolve_news_items(
        digest.get("macro", []), raw_data["macro_news"]
    )
    digest["ai"] = resolve_news_items(digest.get("ai", []), raw_data["ai_news"])
    digest["startup"] = resolve_news_items(digest.get("startup", []), raw_data["startup_news"])

    # Gemini 可能因篇數或排序漏掉來源；只要原始抓取有資料，就至少保留該來源一則。
    digest["taiwan_stocks"]["news"] = ensure_source_coverage(
        digest["taiwan_stocks"]["news"],
        raw_data["taiwan_stock_news"],
        ["Yahoo", "鉅亨網"],
        max_items=100,
    )
    digest["us_stocks"]["news"] = ensure_source_coverage(
        digest["us_stocks"]["news"],
        raw_data["us_stock_news"],
        ["Yahoo", "鉅亨網"],
        max_items=100,
    )
    digest["macro"] = ensure_source_coverage(
        digest["macro"],
        raw_data["macro_news"],
        ["鉅亨網"],
        max_items=50,
    )
    digest["ai"] = ensure_source_coverage(
        digest["ai"],
        raw_data["ai_news"],
        ["TechCrunch", "VentureBeat", "Reuters", "OpenAI", "Anthropic", "Google-DeepMind",
         "Meta-AI", "NVIDIA", "SemiAnalysis", "Y Combinator-AI"],
        max_items=100,
    )
    digest["startup"] = ensure_source_coverage(
        digest["startup"],
        raw_data["startup_news"],
        ["Y Combinator", "創業小聚", "數位時代", "a16z", "Sequoia"],
        max_items=100,
    )

    # 每個來源至少顯示 5 篇；若該來源當日實際少於 5 篇，就全部顯示，不捏造新聞。
    digest["taiwan_stocks"]["news"] = ensure_minimum_per_source(
        digest["taiwan_stocks"]["news"], raw_data["taiwan_stock_news"], min_per_source=5
    )
    digest["us_stocks"]["news"] = ensure_minimum_per_source(
        digest["us_stocks"]["news"], raw_data["us_stock_news"], min_per_source=5
    )
    digest["macro"] = ensure_minimum_per_source(
        digest["macro"], raw_data["macro_news"], min_per_source=5
    )
    digest["ai"] = ensure_minimum_per_source(
        digest["ai"], raw_data["ai_news"], min_per_source=5
    )
    digest["startup"] = ensure_minimum_per_source(
        digest["startup"], raw_data["startup_news"], min_per_source=5
    )

    print("== 步驟 3.5/4：抓取今日 Google 日曆行程 ==")
    calendar_events = fetch_todays_events(now_tw)

    print("== 步驟 4/4：產生網頁並推播 ==")
    html = render_html(digest, now_tw, calendar_events)
    os.makedirs("docs", exist_ok=True)
    with open("docs/index.html", "w", encoding="utf-8") as f:
        f.write(html)
    with open("docs/latest.json", "w", encoding="utf-8") as f:
        json.dump(digest, f, ensure_ascii=False, indent=2)

    short_body = digest.get("greeting", "") + " 點開看今天的完整晨報"
    send_bark_notification("早安，你的晨報到了 ☕️", short_body, url=PAGE_URL)

    print("完成！")


if __name__ == "__main__":
    main()
