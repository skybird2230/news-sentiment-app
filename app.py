"""
app.py (combined news + calendar version)
-------------------------------------------
Single Flask web app / PWA that shows BOTH:
  1. News headline sentiment (NewsNow scraping + keyword scoring)
  2. Today's ForexFactory economic calendar sentiment (Actual vs Forecast)

Each source keeps its OWN sentiment verdict and OWN "last updated" time --
they are shown side by side (or stacked on narrow phone screens), never
merged into a single combined score, matching the MT5 CombinedSentimentPanel.

All files (this app.py, manifest.json, service-worker.js, icons,
requirements.txt) live together in one folder -- no "static" subfolder
needed, so it can be uploaded straight to GitHub root and deployed on
Render exactly like the previous single-source app.
"""

import re
import time
from datetime import datetime, timedelta, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string, send_from_directory

app = Flask(__name__)

BD_TZ = timezone(timedelta(hours=6))  # Bangladesh Standard Time, UTC+6

INSTRUMENTS = [
    "USA30", "USA100", "USA500", "GER40", "FRA40", "EU50", "UK100",
    "SUI20", "AUS200", "JPN225", "BTC", "XAG/USD", "XAU/USD", "CRUDE OIL",
]

# --------------------------------------------------------------------------
# NEWS source (NewsNow headlines + keyword scoring)
# --------------------------------------------------------------------------

NEWS_SOURCES = [
    "https://www.newsnow.co.uk/h/Business+&+Finance/Currencies?type=ln",
    "https://www.newsnow.co.uk/h/Business+&+Finance/Stock+Markets?type=ln",
    "https://www.newsnow.co.uk/h/Business+&+Finance/Commodities?type=ln",
]
NEWS_HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

NEWS_KEYWORDS = {
    "USA30":     ["dow jones", "dow ", "wall street"],
    "USA100":    ["nasdaq", "tech stocks", "big tech", "us tech"],
    "USA500":    ["s&p 500", "s&p500", "sp 500", "sp500", "s&p "],
    "GER40":     ["dax", "german stocks", "germany", "frankfurt"],
    "FRA40":     ["cac 40", "cac", "french stocks", "france", "paris stocks"],
    "EU50":      ["euro stoxx", "eurostoxx", "eurozone stocks", "ecb", "european central bank"],
    "UK100":     ["ftse", "london stocks", "bank of england", "boe ", "sterling", "pound"],
    "SUI20":     ["smi", "swiss stocks", "snb", "swiss national bank", "swiss franc"],
    "AUS200":    ["asx", "australian stocks", "rba", "reserve bank of australia", "aussie", "australian dollar"],
    "JPN225":    ["nikkei", "japanese stocks", "boj", "bank of japan", "yen"],
    "BTC":       ["bitcoin", "btc", "crypto"],
    "XAG/USD":   ["silver"],
    "XAU/USD":   ["gold", "bullion"],
    "CRUDE OIL": ["crude", "wti", "brent", "oil price", "oil prices"],
}

POSITIVE_WORDS = [
    "rally", "rallies", "surge", "surges", "jump", "jumps", "gain", "gains",
    "gained", "rise", "rises", "rising", "climb", "climbs", "boost", "boosts",
    "strong", "strength", "resilien", "up ", "higher", "advance", "advances",
    "recover", "recovers", "record high", "outperform",
]
NEGATIVE_WORDS = [
    "fall", "falls", "falling", "drop", "drops", "slump", "slumps", "plunge",
    "plunges", "weigh", "weighs", "pressure", "weak", "weakens", "weakened",
    "down ", "lower", "decline", "declines", "slide", "slides", "tumble",
    "tumbles", "sell-off", "selloff", "risk-off", "underperform",
]

NEWS_CACHE = {"data": None, "ts": 0}
CACHE_SECONDS = 300


def fetch_headlines():
    headlines = []
    for url in NEWS_SOURCES:
        try:
            resp = requests.get(url, headers=NEWS_HEADERS, timeout=15)
            resp.raise_for_status()
        except Exception:
            continue
        soup = BeautifulSoup(resp.text, "html.parser")
        for a in soup.select("a"):
            text = a.get_text(strip=True)
            href = a.get("href", "")
            if text and len(text) > 15 and "/A/" in href:
                headlines.append(text)
    seen, unique = set(), []
    for h in headlines:
        if h not in seen:
            seen.add(h)
            unique.append(h)
    return unique


def score_headline(hl):
    pos = any(w in hl for w in POSITIVE_WORDS)
    neg = any(w in hl for w in NEGATIVE_WORDS)
    if pos and not neg:
        return 1
    if neg and not pos:
        return -1
    return 0


def analyze_news(headlines):
    results = {inst: {"score": 0, "examples": []} for inst in INSTRUMENTS}
    risk_off, risk_on = 0, 0
    for h in headlines:
        hl = h.lower()
        if "risk-off" in hl or "risk off" in hl:
            risk_off += 1
        if "risk-on" in hl or "risk on" in hl:
            risk_on += 1
        for inst, kws in NEWS_KEYWORDS.items():
            if any(kw in hl for kw in kws):
                s = score_headline(hl)
                if s != 0:
                    results[inst]["score"] += s
                    if len(results[inst]["examples"]) < 2:
                        results[inst]["examples"].append(h[:90])
    return results, risk_on, risk_off


def classify(score):
    if score >= 2:
        return "STRONG"
    if score <= -2:
        return "WEAK"
    if score == 1:
        return "MILD_STRONG"
    if score == -1:
        return "MILD_WEAK"
    return "NEUTRAL"


def news_overall_sentiment(results, risk_on, risk_off):
    equity_syms = ["USA30", "USA100", "USA500", "GER40", "FRA40", "EU50", "UK100", "SUI20", "AUS200", "JPN225"]
    equity_score = sum(results[s]["score"] for s in equity_syms)
    safe_haven_score = results["XAG/USD"]["score"] + results["XAU/USD"]["score"]
    btc_score = results["BTC"]["score"]
    tilt = equity_score - safe_haven_score * 0.5 + btc_score * 0.5
    tilt += (risk_on - risk_off) * 2
    if tilt >= 2:
        return "RISK-ON", tilt
    if tilt <= -2:
        return "RISK-OFF", tilt
    return "NEUTRAL", tilt


def get_news_signals(force=False):
    now = time.time()
    if not force and NEWS_CACHE["data"] and (now - NEWS_CACHE["ts"] < CACHE_SECONDS):
        return NEWS_CACHE["data"]

    headlines = fetch_headlines()
    if not headlines:
        if NEWS_CACHE["data"]:
            return NEWS_CACHE["data"]
        results = {inst: {"score": 0, "examples": []} for inst in INSTRUMENTS}
        sentiment, tilt = "NEUTRAL", 0
    else:
        results, risk_on, risk_off = analyze_news(headlines)
        sentiment, tilt = news_overall_sentiment(results, risk_on, risk_off)

    data = {
        "instruments": [
            {
                "name": inst,
                "signal": classify(results[inst]["score"]),
                "score": results[inst]["score"],
                "note": " | ".join(results[inst]["examples"]) or "no relevant news",
            }
            for inst in INSTRUMENTS
        ],
        "sentiment": sentiment,
        "tilt": round(tilt, 1),
        "updated": datetime.now(BD_TZ).strftime("%Y-%m-%d %I:%M:%S %p BDT"),
        "count": len(headlines),
        "count_label": "headlines scanned",
    }
    NEWS_CACHE["data"] = data
    NEWS_CACHE["ts"] = now
    return data



# --------------------------------------------------------------------------
# Web routes
# --------------------------------------------------------------------------

SIGNAL_COLORS = {
    "STRONG": "#22c55e", "MILD_STRONG": "#86efac",
    "WEAK": "#ef4444", "MILD_WEAK": "#fca5a5",
    "NEUTRAL": "#9ca3af",
}
SIGNAL_LABELS = {
    "STRONG": "▲▲ STRONG", "MILD_STRONG": "▲ Mild+",
    "WEAK": "▼▼ WEAK", "MILD_WEAK": "▼ Mild-",
    "NEUTRAL": "— Neutral",
}

PAGE_TEMPLATE = """<!DOCTYPE html>
<html lang="bn">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
<title>News Sentiment Panel</title>
<link rel="manifest" href="/manifest.json">
<meta name="theme-color" content="#0b0f19">
<link rel="apple-touch-icon" href="/icon-192.png">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="black-translucent">
<style>
  * { box-sizing: border-box; }
  body {
    background:#0b0f19; color:#e5e7eb; font-family:-apple-system,Segoe UI,Roboto,sans-serif;
    margin:0; padding:16px; padding-bottom:40px;
  }
  h1 { font-size:19px; margin:4px 0; }
  .updated { font-size:12px; color:#6b7280; margin-bottom:16px; }
  .sentiment {
    text-align:center; padding:18px; border-radius:14px; margin-bottom:18px;
    background:#111827; border:1px solid #1f2937;
  }
  .sentiment .label { font-size:13px; color:#9ca3af; }
  .sentiment .value { font-size:26px; font-weight:700; }
  .row {
    display:flex; justify-content:space-between; align-items:center;
    padding:12px 14px; background:#111827; border-radius:10px; margin-top:10px;
    border:1px solid #1f2937;
  }
  .inst { font-weight:600; font-size:15px; }
  .sig { font-weight:700; font-size:14px; }
  .note { font-size:11px; color:#6b7280; padding:4px 14px 0 14px; }
  .refresh-btn {
    display:block; width:100%; margin-top:20px; padding:14px; border:none;
    border-radius:10px; background:#2563eb; color:white; font-size:15px;
    font-weight:600;
  }
</style>
</head>
<body>
  <h1>📰 News Sentiment Panel</h1>
  <div class="updated">{{ headline_count }} headlines scanned • Updated {{ updated }}</div>
  <div class="sentiment">
    <div class="label">MARKET SENTIMENT</div>
    <div class="value" style="color:{{ sent_color }}">{{ sentiment }} (tilt {{ tilt }})</div>
  </div>
  {% for i in instruments %}
  <div class="row">
    <div class="inst">{{ i.name }}</div>
    <div class="sig" style="color:{{ colors[i.signal] }}">{{ labels[i.signal] }}</div>
  </div>
  <div class="note">{{ i.note }}</div>
  {% endfor %}
  <button class="refresh-btn" onclick="location.reload()">🔄 Refresh Now</button>
<script>
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('/service-worker.js');
}
</script>
</body>
</html>"""


def sentiment_color(sentiment):
    if sentiment == "RISK-ON":
        return "#22c55e"
    if sentiment == "RISK-OFF":
        return "#ef4444"
    return "#9ca3af"


@app.route("/")
def dashboard():
    data = get_news_signals()
    return render_template_string(
        PAGE_TEMPLATE,
        instruments=data["instruments"],
        sentiment=data["sentiment"],
        tilt=data["tilt"],
        updated=data["updated"],
        headline_count=data["count"],
        colors=SIGNAL_COLORS,
        labels=SIGNAL_LABELS,
        sent_color=sentiment_color(data["sentiment"]),
    )


@app.route("/api/news")
def api_news():
    return jsonify(get_news_signals())


@app.route("/refresh")
def refresh():
    get_news_signals(force=True)
    return dashboard()


@app.route("/manifest.json")
def manifest():
    return send_from_directory(".", "manifest.json")


@app.route("/service-worker.js")
def service_worker():
    return send_from_directory(".", "service-worker.js")


@app.route("/icon-192.png")
def icon_192():
    return send_from_directory(".", "icon-192.png")


@app.route("/icon-512.png")
def icon_512():
    return send_from_directory(".", "icon-512.png")


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
