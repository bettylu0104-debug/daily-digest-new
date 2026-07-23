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

import requests
import feedparser

# ============================================================
# 1. 設定區：新聞來源清單
#    如果之後想增加/替換來源，只要改這裡的網址即可，不用動其他程式碼
# ============================================================

RSS_SOURCES = {
    "台股": [
        ("Yahoo奇摩股市-台股動態", "https://tw.stock.yahoo.com/rss?category=tw-market"),
        ("鉅亨網-台股新聞", "https://news.cnyes.com/rss/cat/tw_stock"),
    ],
    "美股": [
        ("Yahoo奇摩股市-國際財經", "https://tw.stock.yahoo.com/rss?category=intl-markets"),
        ("鉅亨網-美股新聞", "https://news.cnyes.com/rss/cat/us_stock"),
    ],
    "AI": [
        ("TechCrunch-AI", "https://techcrunch.com/category/artificial-intelligence/feed/"),
        ("VentureBeat-AI", "https://venturebeat.com/category/ai/feed/"),
        ("數位時代-AI與大數據", "https://www.bnext.com.tw/rss/category/AI"),
    ],
    "新創": [
        ("Y Combinator Blog", "https://www.ycombinator.com/blog/rss"),
        ("a16z", "https://a16z.com/feed/"),
        ("創業小聚 Meet", "https://meet.bnext.com.tw/rss"),
    ],
}

# 總經新聞：直接用關鍵字去 Yahoo/鉅亨網搜尋 Fed、CPI、非農等關鍵字
MACRO_KEYWORDS_SOURCES = [
    ("鉅亨網-總經", "https://news.cnyes.com/rss/cat/macro"),
]

TIMEZONE_OFFSET_HOURS = 8  # 台北時間 UTC+8


# ============================================================
# 2. 抓 RSS 新聞（任何一個來源失敗都不會讓程式中斷）
# ============================================================

def fetch_rss_category(sources, max_per_source=8):
    """抓一個分類底下所有來源的新聞，回傳 list of dict"""
    items = []
    for source_name, url in sources:
        try:
            feed = feedparser.parse(url)
            if feed.bozo and not feed.entries:
                print(f"[警告] 來源可能失效，略過：{source_name} ({url})")
                continue
            for entry in feed.entries[:max_per_source]:
                items.append({
                    "source": source_name,
                    "title": entry.get("title", "").strip(),
                    "link": entry.get("link", ""),
                    "published": entry.get("published", entry.get("updated", "")),
                    "summary": (entry.get("summary", "") or "")[:300],
                })
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
    """抓今天 (台北時間) 的 Google 日曆行程，依開始時間排序"""
    if not (CAL_CLIENT_ID and CAL_CLIENT_SECRET and CAL_REFRESH_TOKEN):
        print("[提示] 尚未設定 Google 日曆憑證，略過今日任務區塊")
        return []
    try:
        token_resp = requests.post(
            "https://oauth2.googleapis.com/token",
            data={
                "client_id": CAL_CLIENT_ID,
                "client_secret": CAL_CLIENT_SECRET,
                "refresh_token": CAL_REFRESH_TOKEN,
                "grant_type": "refresh_token",
            },
            timeout=15,
        )
        token_resp.raise_for_status()
        access_token = token_resp.json()["access_token"]

        day_start = now_tw.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + datetime.timedelta(days=1)
        time_min = (day_start - datetime.timedelta(hours=TIMEZONE_OFFSET_HOURS)).isoformat() + "Z"
        time_max = (day_end - datetime.timedelta(hours=TIMEZONE_OFFSET_HOURS)).isoformat() + "Z"

        resp = requests.get(
            f"https://www.googleapis.com/calendar/v3/calendars/{CAL_CALENDAR_ID}/events",
            headers={"Authorization": f"Bearer {access_token}"},
            params={
                "timeMin": time_min,
                "timeMax": time_max,
                "singleEvents": "true",
                "orderBy": "startTime",
            },
            timeout=15,
        )
        resp.raise_for_status()
        events = []
        for item in resp.json().get("items", []):
            start = item.get("start", {})
            start_str = start.get("dateTime", start.get("date", ""))
            time_label = "整天"
            if "dateTime" in start:
                dt = datetime.datetime.fromisoformat(start_str.replace("Z", "+00:00"))
                time_label = dt.astimezone(datetime.timezone(datetime.timedelta(hours=8))).strftime("%H:%M")
            events.append({
                "time": time_label,
                "title": item.get("summary", "(無標題)"),
                "location": item.get("location", ""),
            })
        return events
    except Exception as e:
        print(f"[警告] 日曆行程抓取失敗：{e}")
        return []


# ============================================================
# 5. 呼叫 Google Gemini，把原始資料整理成人性化晨報
#    Gemini API 有免費額度，一天一次遠遠用不完
#    免費申請金鑰：https://aistudio.google.com/apikey
# ============================================================

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
# 如果之後 Google 更新了免費模型名稱，去 https://ai.google.dev/gemini-api/docs/models 查最新的 flash 免費模型名稱替換掉下面這行
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "gemini-2.5-flash")


def call_gemini(raw_data: dict) -> dict:
    """把原始資料交給 Gemini，請它輸出結構化 JSON 晨報"""
    if not GEMINI_API_KEY:
        raise RuntimeError("找不到 GEMINI_API_KEY，請確認 GitHub Secrets 有設定")

    system_instruction = """
你是使用者的私人財經與科技新聞助理，語氣親切、像信任的朋友在跟他簡報，但內容要專業精準，不浮誇。
請根據我提供的原始資料，整理成「今日晨報」，並且【只能輸出 JSON，不要有任何其他文字、不要markdown code fence】，格式如下：

{
  "greeting": "一句今天的簡短問候語，可以提到天氣感或市場氣氛，20字內",
  "taiwan_stocks": {
    "night_futures_note": "根據原始資料整理台指期夜盤漲跌重點，若無資料請寫'今日無夜盤資料，建議至台灣期貨交易所官網查看'",
    "top_volume_note": "用一句話總結成交量最大的個股是誰、量有多大",
    "hot_sector_note": "用一句話總結最熱的類股是誰、漲了多少",
    "news": [ {"title": "...", "summary": "20-40字重點摘要", "source": "...", "link": "..."} ... 十條 ]
  },
  "us_stocks": {
    "market_summary": "用1-2句話總結美股大盤走勢",
    "tsm_adr_note": "台積電ADR漲跌幅一句話重點",
    "news": [ {"title": "...", "summary": "...", "source": "...", "link": "..."} ... 十條 ]
  },
  "macro": [ {"title": "...", "summary": "...", "source": "...", "link": "..."} ... 三條，聚焦美國經濟數據或Fed動態 ],
  "ai": [ {"title": "...", "summary": "...", "source": "...", "link": "..."} ... 十條，涵蓋LLM、垂直AI應用、AI工具 ],
  "startup": [ {"title": "...", "summary": "...", "source": "...", "link": "..."} ... 五到八條，來自YC/a16z/創業小聚 ],
  "closing_note": "一句鼓勵/提醒的結語，15字內"
}

規則：
- 新聞條數不夠就盡量整理現有的，不要編造不存在的新聞。
- summary 一定要是你自己整理的重點，不要整段照抄原文。
- 台股/美股新聞挑當天最重要、對投資人最有意義的。
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
