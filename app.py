"""
app.py
------
Flask web app version of the News Sentiment Panel. Unlike the desktop
version, this fetches and analyzes news LIVE on each request (with a short
cache), so it doesn't need a separate always-running script or a shared
CSV file. Deploy this to a free host (see README_hosting.md) and you can
open it from your phone anywhere with mobile data, no WiFi/PC needed.

Also serves as a PWA (Progressive Web App) -- open it once in your phone's
browser, tap "Add to Home Screen" (Chrome) or "Add to Home Screen" (Safari
share menu), and it behaves like an installed app icon.
"""

import re
import time
from datetime import datetime, timezone

import requests
from bs4 import BeautifulSoup
from flask import Flask, jsonify, render_template_string

app = Flask(__name__, static_folder="static")

# --------------------------------------------------------------------------
# Same analysis logic as news_analyzer.py (kept self-contained here so this
# single file can be deployed on its own)
# --------------------------------------------------------------------------

NEWS_SOURCES = [
    "https://www.newsnow.co.uk/h/Business+&+Finance/Currencies?type=ln",
    "https://www.newsnow.co.uk/h/Business+&+Finance/Stock+Markets?type=ln",
    "https://www.newsnow.co.uk/h/Business+&+Finance/Commodities?type=ln",
]
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}

INSTRUMENTS = [
    "USA30", "USA100", "GER40", "FRA40", "EU50", "UK100",
    "SUI20", "AUS200", "JPN225", "BTC", "XAG/USD", "CRUDE OIL",
]

KEYWORDS = {
    "USA30":     ["dow jones", "dow ", "wall street", "us stocks", "s&p", "nasdaq"],
    "USA100":    ["nasdaq", "tech stocks", "big tech", "us tech"],
    "GER40":     ["dax", "german stocks", "germany", "frankfurt"],
    "FRA40":     ["cac 40", "cac", "french stocks", "france", "paris stocks"],
    "EU50":      ["euro stoxx", "eurostoxx", "eurozone stocks", "ecb", "european central bank"],
    "UK100":     ["ftse", "london stocks", "bank of england", "boe ", "sterling", "pound"],
    "SUI20":     ["smi", "swiss stocks", "snb", "swiss national bank", "swiss franc"],
    "AUS200":    ["asx", "australian stocks", "rba", "reserve bank of australia", "aussie", "australian dollar"],
    "JPN225":    ["nikkei", "japanese stocks", "boj", "bank of japan", "yen"],
    "BTC":       ["bitcoin", "btc", "crypto"],
    "XAG/USD":   ["silver"],
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

CACHE = {"data": None, "ts": 0}
CACHE_SECONDS = 300  # re-fetch news at most every 5 minutes


def fetch_headlines():
    headlines = []
    for url in NEWS_SOURCES:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=15)
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


def analyze(headlines):
    results = {inst: {"score": 0, "hits": 0, "examples": []} for inst in INSTRUMENTS}
    risk_off, risk_on = 0, 0
    for h in headlines:
        hl = h.lower()
        if "risk-off" in hl or "risk off" in hl:
            risk_off += 1
        if "risk-on" in hl or "risk on" in hl:
            risk_on += 1
        for inst, kws in KEYWORDS.items():
            if any(kw in hl for kw in kws):
                s = score_headline(hl)
                if s != 0:
                    results[inst]["score"] += s
                    results[inst]["hits"] += 1
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


def overall_sentiment(results, risk_on, risk_off):
    equity_syms = ["USA30", "USA100", "GER40", "FRA40", "EU50", "UK100", "SUI20", "AUS200", "JPN225"]
    equity_score = sum(results[s]["score"] for s in equity_syms)
    safe_haven_score = results["XAG/USD"]["score"]
    btc_score = results["BTC"]["score"]
    tilt = equity_score - safe_haven_score * 0.5 + btc_score * 0.5
    tilt += (risk_on - risk_off) * 2
    if tilt >= 2:
        return "RISK-ON", tilt
    if tilt <= -2:
        return "RISK-OFF", tilt
    return "NEUTRAL", tilt


def get_signals(force=False):
    now = time.time()
    if not force and CACHE["data"] and (now - CACHE["ts"] < CACHE_SECONDS):
        return CACHE["data"]

    headlines = fetch_headlines()
    if not headlines:
        # keep serving stale cache if fetch fails
        if CACHE["data"]:
            return CACHE["data"]
        results = {inst: {"score": 0, "hits": 0, "examples": []} for inst in INSTRUMENTS}
        sentiment, tilt = "NEUTRAL", 0
    else:
        results, risk_on, risk_off = analyze(headlines)
        sentiment, tilt = overall_sentiment(results, risk_on, risk_off)

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
        "updated": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
        "headline_count": len(headlines),
    }
    CACHE["data"] = data
    CACHE["ts"] = now
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
<link rel="manifest" href="/static/manifest.json">
<meta name="theme-color" content="#0b0f19">
<link rel="apple-touch-icon" href="/static/icon-192.png">
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
  .sentiment .value { font-size:26px; font-weight:700; color:{{ sent_color }}; }
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
    <div class="value">{{ sentiment }} (tilt {{ tilt }})</div>
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
  navigator.serviceWorker.register('/static/service-worker.js');
}
</script>
</body>
</html>"""


@app.route("/")
def dashboard():
    data = get_signals()
    sent_color = SIGNAL_COLORS.get(
        "STRONG" if data["sentiment"] == "RISK-ON" else "WEAK" if data["sentiment"] == "RISK-OFF" else "NEUTRAL",
        "#9ca3af",
    )
    return render_template_string(
        PAGE_TEMPLATE,
        instruments=data["instruments"],
        sentiment=data["sentiment"],
        tilt=data["tilt"],
        updated=data["updated"],
        headline_count=data["headline_count"],
        colors=SIGNAL_COLORS,
        labels=SIGNAL_LABELS,
        sent_color=sent_color,
    )


@app.route("/api/signals")
def api_signals():
    return jsonify(get_signals())


@app.route("/refresh")
def refresh():
    get_signals(force=True)
    return dashboard()


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
