import os
import time
import json
import hashlib
import requests
import sqlite3
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree
from calendar_events import CALENDAR

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INTERVAL = int(os.environ.get("INTERVAL_MINUTES", "3"))
POLY_INTERVAL = int(os.environ.get("POLY_INTERVAL_MINUTES", "5"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "7"))

TG_URL = f"https://api.telegram.org/bot{TOKEN}"
POLYMARKET_URL = "https://gamma-api.polymarket.com/markets?limit=50&active=true&closed=false&order=volume&ascending=false"

seen_news = set()
seen_signals = set()
markets_cache = []
last_poly_check = 0

# ─── BASE DE DONNÉES HISTORIQUE ──────────────────────────────────────────────

def init_db():
    conn = sqlite3.connect("/tmp/alerts.db")
    conn.execute("""
        CREATE TABLE IF NOT EXISTS alerts (
            id TEXT PRIMARY KEY,
            timestamp TEXT,
            source TEXT,
            title TEXT,
            market_title TEXT,
            market_prob INTEGER,
            market_delta INTEGER,
            score INTEGER,
            sent INTEGER DEFAULT 1
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS market_snapshots (
            alert_id TEXT,
            market_id TEXT,
            prob_before INTEGER,
            prob_after INTEGER,
            checked_at TEXT
        )
    """)
    conn.commit()
    return conn

def save_alert(conn, alert_id, source, title, markets, score):
    try:
        market_title = markets[0]["title"] if markets else ""
        market_prob = markets[0]["prob"] if markets else 0
        market_delta = markets[0]["delta"] if markets else 0
        conn.execute("""
            INSERT OR IGNORE INTO alerts
            (id, timestamp, source, title, market_title, market_prob, market_delta, score, sent)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)
        """, (alert_id, datetime.now(timezone.utc).isoformat(), source, title, market_title, market_prob, market_delta, score))
        conn.commit()
    except Exception as e:
        log(f"DB save error: {e}")

def get_daily_stats(conn):
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
        cur = conn.execute("SELECT COUNT(*), AVG(score) FROM alerts WHERE timestamp > ? AND sent=1", (since,))
        row = cur.fetchone()
        count = row[0] or 0
        avg_score = round(row[1] or 0, 1)
        cur2 = conn.execute("SELECT source, COUNT(*) as c FROM alerts WHERE timestamp > ? GROUP BY source ORDER BY c DESC LIMIT 5", (since,))
        top_sources = cur2.fetchall()
        return count, avg_score, top_sources
    except:
        return 0, 0, []

# ─── SOURCES ─────────────────────────────────────────────────────────────────

SOURCES = [
    # GÉOPOLITIQUE
    {"name": "Reuters",          "cat": "geo",       "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP News",          "cat": "geo",       "url": "https://rsshub.app/apnews/topics/apf-topnews"},
    {"name": "Al Jazeera",       "cat": "geo",       "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "BBC World",        "cat": "geo",       "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "DW News",          "cat": "geo",       "url": "https://rss.dw.com/rdf/rss-en-all"},
    {"name": "UN News",          "cat": "geo",       "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml"},
    {"name": "The Guardian",     "cat": "geo",       "url": "https://www.theguardian.com/world/rss"},
    {"name": "Foreign Affairs",  "cat": "geo",       "url": "https://www.foreignaffairs.com/rss.xml"},
    # POLITIQUE
    {"name": "Politico",         "cat": "politics",  "url": "https://rss.politico.com/politics-news.xml"},
    {"name": "The Hill",         "cat": "politics",  "url": "https://thehill.com/news/feed/"},
    {"name": "RFI",              "cat": "politics",  "url": "https://www.rfi.fr/fr/rss-podcasts/rss_actualites.xml"},
    {"name": "Le Monde",         "cat": "politics",  "url": "https://www.lemonde.fr/rss/une.xml"},
    # CRYPTO
    {"name": "CoinDesk",         "cat": "crypto",    "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph",    "cat": "crypto",    "url": "https://cointelegraph.com/rss"},
    {"name": "The Block",        "cat": "crypto",    "url": "https://www.theblock.co/rss.xml"},
    {"name": "Decrypt",          "cat": "crypto",    "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin Magazine", "cat": "crypto",    "url": "https://bitcoinmagazine.com/feed"},
    # ÉCONOMIE + SOURCES OFFICIELLES
    {"name": "MarketWatch",      "cat": "economics", "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "Les Echos",        "cat": "economics", "url": "https://services.lesechos.fr/rss/les-echos-finance.xml"},
    {"name": "Bloomberg",        "cat": "economics", "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Financial Times",  "cat": "economics", "url": "https://www.ft.com/rss/home"},
    {"name": "The Economist",    "cat": "economics", "url": "https://www.economist.com/latest/rss.xml"},
    # SOURCES OFFICIELLES ULTRA-RAPIDES
    {"name": "Fed Reserve",      "cat": "economics", "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "White House",      "cat": "politics",  "url": "https://www.whitehouse.gov/feed/"},
    {"name": "SEC",              "cat": "crypto",    "url": "https://www.sec.gov/cgi-bin/browse-edgar?action=getcurrent&type=&dateb=&owner=include&count=10&search_text=&output=atom"},
    {"name": "NATO",             "cat": "geo",       "url": "https://www.nato.int/cps/en/natohq/news.xml"},
    {"name": "IMF",              "cat": "economics", "url": "https://www.imf.org/en/News/rss?language=eng"},
    # TELEGRAM CHANNELS (via RSSHub)
    {"name": "@BreakingNews",    "cat": "geo",       "url": "https://rsshub.app/telegram/channel/BreakingNews"},
    {"name": "@disclosetv",      "cat": "geo",       "url": "https://rsshub.app/telegram/channel/disclosetv"},
    {"name": "@sentdefender",    "cat": "geo",       "url": "https://rsshub.app/telegram/channel/sentdefender"},
    {"name": "@IntelSlava",      "cat": "geo",       "url": "https://rsshub.app/telegram/channel/IntelSlava"},
    {"name": "@BBCBreaking",     "cat": "geo",       "url": "https://rsshub.app/telegram/channel/BBCBreaking"},
    {"name": "@CoinDeskNews",    "cat": "crypto",    "url": "https://rsshub.app/telegram/channel/CoinDeskNews"},
    {"name": "@Cointelegraph",   "cat": "crypto",    "url": "https://rsshub.app/telegram/channel/cointelegraph"},
    # REDDIT
    {"name": "r/worldnews",      "cat": "geo",       "url": "https://www.reddit.com/r/worldnews/hot.json?limit=15", "type": "reddit"},
    {"name": "r/geopolitics",    "cat": "geo",       "url": "https://www.reddit.com/r/geopolitics/hot.json?limit=10", "type": "reddit"},
    {"name": "r/CryptoCurrency", "cat": "crypto",    "url": "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=10", "type": "reddit"},
    {"name": "r/investing",      "cat": "economics", "url": "https://www.reddit.com/r/investing/hot.json?limit=10", "type": "reddit"},
    {"name": "r/Polymarket",     "cat": "all",       "url": "https://www.reddit.com/r/Polymarket/hot.json?limit=10", "type": "reddit"},
]

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def fmt_vol(n):
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000: return f"${round(n/1_000)}K"
    return f"${round(n)}"

def send_telegram(text):
    try:
        r = requests.post(f"{TG_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        data = r.json()
        if not data.get("ok"):
            log(f"Telegram error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

def news_id(title, source):
    return hashlib.md5(f"{source}:{title}".encode()).hexdigest()[:12]

def fetch_rss(src):
    try:
        r = requests.get(
            f"https://api.allorigins.win/get?url={requests.utils.quote(src['url'])}",
            timeout=8
        )
        j = r.json()
        xml = ElementTree.fromstring(j["contents"])
        items = xml.findall(".//item") or xml.findall(".//{http://www.w3.org/2005/Atom}entry")
        news = []
        for i in items[:10]:
            title = (i.findtext("title") or i.findtext("{http://www.w3.org/2005/Atom}title") or "").replace("<![CDATA[","").replace("]]>","").strip()
            link = (i.findtext("link") or i.findtext("{http://www.w3.org/2005/Atom}link") or "").strip()
            if title and len(title) > 10:
                news.append({"title": title, "link": link, "source": src["name"], "cat": src["cat"]})
        return news
    except:
        return []

def fetch_reddit(src):
    try:
        r = requests.get(src["url"], headers={"User-Agent": "Mozilla/5.0"}, timeout=8)
        posts = r.json().get("data", {}).get("children", [])
        news = []
        for p in posts:
            d = p.get("data", {})
            if d.get("stickied"): continue
            title = d.get("title", "")
            if title and len(title) > 10 and d.get("ups", 0) > 100:
                news.append({
                    "title": title,
                    "link": "https://reddit.com" + d.get("permalink",""),
                    "source": src["name"],
                    "cat": src["cat"],
                    "ups": d.get("ups", 0)
                })
        return news
    except:
        return []

def fetch_all_news():
    all_news = []
    for src in SOURCES:
        if src.get("type") == "reddit":
            all_news.extend(fetch_reddit(src))
        else:
            all_news.extend(fetch_rss(src))
    return all_news

def guess_cat(title):
    t = (title or "").lower()
    if any(k in t for k in ["bitcoin","btc","eth","crypto","solana","token","defi","nft"]): return "crypto"
    if any(k in t for k in ["election","president","trump","democrat","republican","vote","minister"]): return "politics"
    if any(k in t for k in ["nba","nfl","cup","champion","football","tennis","sport","match"]): return "sports"
    if any(k in t for k in ["iran","russia","ukraine","china","war","peace","nuclear","military","nato","conflict","israel","gaza"]): return "geopolitics"
    if any(k in t for k in ["fed","rate","inflation","gdp","recession","economy","dollar","oil","gold","tariff"]): return "economics"
    return "other"

def fetch_markets():
    try:
        r = requests.get(POLYMARKET_URL, timeout=15)
        data = r.json()
        raw = data if isinstance(data, list) else data.get("markets", [])
        result = []
        for m in raw:
            try:
                prices = json.loads(m.get("outcomePrices", "[]"))
                prices = [float(p) for p in prices]
            except:
                prices = []
            prob = round(prices[0] * 100) if prices else 50
            vol = float(m.get("volume24hr") or m.get("volumeNum") or m.get("volume") or 0)
            delta = round((prices[0] - 0.5) * 200) if len(prices) >= 2 else 0
            title = m.get("question") or m.get("title") or ""
            events = m.get("events") or []
            event_slug = events[0].get("slug", "") if events else m.get("eventSlug", "")
            if vol > 0 and len(title) > 5 and 1 < prob < 99:
                result.append({
                    "id": m.get("id", ""),
                    "title": title,
                    "prob": prob,
                    "vol": vol,
                    "delta": delta,
                    "cat": guess_cat(title),
                    "slug": m.get("slug") or "",
                    "event_slug": event_slug,
                    "endDate": m.get("endDateIso") or m.get("endDate", ""),
                })
        return result
    except Exception as e:
        log(f"Fetch markets error: {e}")
        return []

def build_link(m):
    event_slug = m.get("event_slug", "")
    slug = m.get("slug", "")
    if event_slug and not event_slug.isdigit() and len(event_slug) > 5:
        return f"https://polymarket.com/event/{event_slug}"
    if slug and not slug.isdigit() and len(slug) > 5:
        return f"https://polymarket.com/event/{slug}"
    query = m["title"][:60].replace(" ", "%20")
    return f"https://polymarket.com/search?q={query}"

def match_news_to_markets(news_item, markets):
    title_words = set(news_item["title"].lower().replace(",","").replace(".","").split())
    stop = {"the","a","an","is","are","to","of","in","on","at","by","for","and","or","be","it","its","this","that","will","has","have","was","were","been","not","but","with","from","as","do","did","can","could","would","should","may","might","who","what","when","where","how","why","he","she","they","we"}
    title_words -= stop
    title_words = {w for w in title_words if len(w) > 3}
    matched = []
    for m in markets:
        mkt_words = set(m["title"].lower().replace(",","").replace(".","").split()) - stop
        common = title_words & mkt_words
        if len(common) >= 2:
            matched.append((m, len(common)))
    matched.sort(key=lambda x: x[1], reverse=True)
    return [m for m, _ in matched[:3]]

# ─── ANALYSE IA AVEC SCORE ────────────────────────────────────────────────────


def fetch_url_content(url, max_chars=1500):
    """Lit le contenu d'une page pour analyse"""
    if not url:
        return ""
    try:
        r = requests.get(url, timeout=8, headers={"User-Agent": "Mozilla/5.0"})
        # Extraire le texte brut
        text = r.text
        # Supprimer les balises HTML basiquement
        import re
        text = re.sub(r'<[^>]+>', ' ', text)
        text = re.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except:
        return ""

def fetch_article_content(url, max_chars=1500):
    """Récupère le contenu brut d'un article"""
    if not url:
        return ""
    try:
        r = requests.get(
            f"https://api.allorigins.win/get?url={requests.utils.quote(url)}",
            timeout=8
        )
        html = r.json().get("contents", "")
        import re as re2
        text = re2.sub(r'<[^>]+>', ' ', html)
        text = re2.sub(r'\s+', ' ', text).strip()
        return text[:max_chars]
    except:
        return ""

def score_and_analyze(news_item, markets):
    """Claude lit le contenu, note 1-10 et donne action + lien Polymarket"""
    if not ANTHROPIC_KEY:
        return 5, None, None
    try:
        # Lire le contenu de l'article
        article = ""
        if news_item.get("link"):
            log(f"Lecture : {news_item['link'][:60]}")
            article = fetch_article_content(news_item["link"])

        markets_str = ""
        if markets:
            markets_str = "\n".join([
                f"{i}. {m['title']} → {m['prob']}% ({'+' if m['delta']>0 else ''}{m['delta']}pts, {fmt_vol(m['vol'])})"
                for i, m in enumerate(markets)
            ])

        prompt = (
            f"Tu es un trader expert Polymarket.\n\n"
            f"SOURCE : {news_item['source']}\n"
            f"TITRE : {news_item['title']}\n"
            f"{'CONTENU : ' + article[:1000] if article else '(contenu non disponible)'}\n\n"
            f"MARCHÉS POLYMARKET LIÉS :\n{markets_str if markets_str else 'Aucun.'}\n\n"
            f'Réponds UNIQUEMENT en JSON : {{"score": <1-10>, "action": "<ACHETER OUI/ACHETER NON/ATTENDRE/AUCUNE OPPORTUNITE>", "marche_index": <0-2 ou -1 si aucun>, "analyse": "<2-3 phrases directes en francais expliquant impact et pourquoi agir ou pas>"}}\n\n'
            f"Score: 1-4=aucun impact, 5-6=incertain, 7-8=opportunité, 9-10=urgent"
        )

        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 350, "messages": [{"role": "user", "content": prompt}]},
            timeout=25
        )
        text = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        data = json.loads(text)
        score = int(data.get("score", 5))
        analysis = None
        best_market = None

        if score >= MIN_SCORE:
            action = data.get("action", "ATTENDRE")
            analyse = data.get("analyse", "")
            midx = int(data.get("marche_index", -1))
            best_market = markets[midx] if 0 <= midx < len(markets) else (markets[0] if markets else None)

            action_icons = {
                "ACHETER OUI": "🟢 ACHETER OUI",
                "ACHETER NON": "🔴 ACHETER NON",
                "ATTENDRE": "⏳ ATTENDRE",
                "AUCUNE OPPORTUNITE": "⚪ AUCUNE OPPORTUNITÉ"
            }
            action_label = action_icons.get(action, f"🎯 {action}")

            analysis = f"{analyse}\n\n{action_label}"
            if best_market:
                analysis += f"\nMarché : {best_market['title'][:60]}"
                analysis += f"\n🔗 {build_link(best_market)}"

        return score, analysis, best_market
    except Exception as e:
        log(f"Claude error: {e}")
        return 5, None, None


def build_news_alert(news_item, matched_markets, score, analysis, best_market=None):
    cat_icons = {"geo": "🌍", "politics": "🏛️", "crypto": "₿", "economics": "📈", "all": "🔔"}
    icon = cat_icons.get(news_item["cat"], "📰")
    score_bar = "🟢" if score >= 9 else "🟡" if score >= 7 else "🟠"

    msg = f"{icon} <b>SIGNAL {score_bar} [{score}/10] — {news_item['source'].upper()}</b>\n\n"
    msg += f"📰 {news_item['title']}\n"
    if news_item.get("link"):
        msg += f"🔗 {news_item['link']}\n"

    if matched_markets:
        msg += f"\n📊 <b>MARCHÉS POLYMARKET</b>\n"
        for m in matched_markets:
            d = m["delta"]
            ds = f"+{d}" if d > 0 else str(d)
            msg += f"• {m['title'][:60]}\n  → {m['prob']}% · {ds}pts · {fmt_vol(m['vol'])}\n  {build_link(m)}\n"

    if analysis:
        msg += f"\n🤖 <b>Analyse IA</b>\n{analysis}"

    if best_market:
        msg += f"\n\n📌 <b>Marché recommandé :</b>\n{best_market['title'][:60]}\n→ {best_market['prob']}% actuel\n{build_link(best_market)}"

    return msg


# ─── CALENDRIER ÉCONOMIQUE ────────────────────────────────────────────────────

def check_calendar(conn, markets):
    """Envoie une alerte 60min et 15min avant chaque événement majeur"""
    now = datetime.now(timezone.utc)
    sent = 0
    for event in CALENDAR:
        try:
            event_dt = datetime.fromisoformat(f"{event['date']}T{event['time']}").replace(tzinfo=timezone.utc)
            diff_min = (event_dt - now).total_seconds() / 60

            # Alerte 60 min avant
            alert_id_60 = f"cal-60-{event['date']}-{event['name'][:20]}"
            if 58 <= diff_min <= 62 and alert_id_60 not in seen_signals:
                matched = [m for m in markets if any(k in m["title"].lower() for k in event["keywords"])]
                msg = build_calendar_alert(event, matched, diff_min=60)
                if send_telegram(msg):
                    seen_signals.add(alert_id_60)
                    sent += 1
                    log(f"Calendrier 60min : {event['name']}")

            # Alerte 15 min avant
            alert_id_15 = f"cal-15-{event['date']}-{event['name'][:20]}"
            if 13 <= diff_min <= 17 and alert_id_15 not in seen_signals:
                matched = [m for m in markets if any(k in m["title"].lower() for k in event["keywords"])]
                msg = build_calendar_alert(event, matched, diff_min=15)
                if send_telegram(msg):
                    seen_signals.add(alert_id_15)
                    sent += 1
                    log(f"Calendrier 15min : {event['name']}")

        except Exception as e:
            log(f"Calendar error: {e}")
    return sent

def build_calendar_alert(event, matched_markets, diff_min):
    impact_icon = "🔴" if event["impact"] >= 10 else "🟠" if event["impact"] >= 8 else "🟡"
    urgency = "DANS 15 MINUTES" if diff_min <= 15 else "DANS 1 HEURE"
    msg = (
        f"{impact_icon} <b>CALENDRIER ÉCONOMIQUE — {urgency}</b>\n\n"
        f"📅 {event['name']}\n"
        f"🕐 Impact : {event['impact']}/10\n"
    )
    if matched_markets:
        msg += f"\n📊 <b>MARCHÉS POLYMARKET LIÉS</b>\n"
        for m in matched_markets[:3]:
            d = m["delta"]
            ds = f"+{d}" if d > 0 else str(d)
            msg += f"• {m['title'][:60]}\n  → {m['prob']}% · {ds}pts · {fmt_vol(m['vol'])}\n  {build_link(m)}\n"
    msg += f"\n💡 <b>Prépare-toi — ce chiffre va faire bouger Polymarket.</b>"
    return msg


# ─── TRACKER DE PROBABILITES ─────────────────────────────────────────────────

prob_history = {}  # {market_id: [(timestamp, prob, volume), ...]}

def update_prob_history(markets):
    now = time.time()
    for m in markets:
        mid = m["id"]
        if mid not in prob_history:
            prob_history[mid] = []
        prob_history[mid].append((now, m["prob"], m["vol"]))
        # Garder seulement les 24 dernières heures
        prob_history[mid] = [(t, p, v) for t, p, v in prob_history[mid] if now - t < 86400]

def detect_prob_surges(markets):
    """Detecte les marches avec montee rapide de probabilite + volume"""
    now = time.time()
    surges = []
    for m in markets:
        mid = m["id"]
        history = prob_history.get(mid, [])
        if len(history) < 2:
            continue

        # Comparer avec il y a 1h et 3h
        current_prob = m["prob"]
        current_vol = m["vol"]

        # Chercher le point il y a ~1h
        one_hour_ago = [(t, p, v) for t, p, v in history if now - t >= 3000 and now - t <= 5400]
        three_hours_ago = [(t, p, v) for t, p, v in history if now - t >= 9000 and now - t <= 12600]

        if not one_hour_ago:
            continue

        old_prob = one_hour_ago[-1][1]
        old_vol = one_hour_ago[-1][2]

        prob_change_1h = current_prob - old_prob
        vol_change = ((current_vol - old_vol) / old_vol * 100) if old_vol > 0 else 0

        # Signal fort : prob monte de +8pts en 1h ET volume augmente de +50%
        if abs(prob_change_1h) >= 8 and vol_change >= 50 and 20 < current_prob < 80:
            surge_id = f"{mid}-surge"
            if surge_id not in seen_signals:
                surges.append({
                    "market": m,
                    "prob_change_1h": prob_change_1h,
                    "vol_change": vol_change,
                    "old_prob": old_prob,
                })
    return surges

def build_surge_alert(surge):
    m = surge["market"]
    prob_change = surge["prob_change_1h"]
    vol_change = surge["vol_change"]
    old_prob = surge["old_prob"]
    direction = "montee" if prob_change > 0 else "chute"
    icon = "🚀" if prob_change > 0 else "📉"
    d = m["delta"]
    ds = f"+{d}" if d > 0 else str(d)
    link = build_link(m)
    msg = (
        f"{icon} <b>MOUVEMENT RAPIDE POLYMARKET</b>\n\n"
        f"📊 {m['title']}\n\n"
        f"Probabilite : {old_prob}% → <b>{m['prob']}%</b> ({'+' if prob_change > 0 else ''}{prob_change:.0f}pts en 1h)\n"
        f"Volume : +{vol_change:.0f}% en 1h\n"
        f"Volume 24h : {fmt_vol(m['vol'])}\n\n"
        f"💡 <b>Quelqu un sait quelque chose</b> — prob et volume montent simultanement.\n"
        f"Entre sur <b>{'OUI' if prob_change > 0 else 'NON'}</b> avant que ca continue.\n\n"
        f"🔗 {link}"
    )
    return msg

def run_news_check(conn):
    global seen_news
    log("Scan des sources...")
    news = fetch_all_news()
    new_items = []

    for item in news:
        nid = news_id(item["title"], item["source"])
        if nid in seen_news:
            continue
        seen_news.add(nid)
        new_items.append((nid, item))

    if not new_items:
        log("Aucune nouvelle info")
        return

    log(f"{len(new_items)} nouvelles infos — scoring IA en cours...")
    sent = 0

    for nid, item in new_items[:10]:  # max 10 par cycle
        matched = match_news_to_markets(item, markets_cache)
        score, analysis, best_market = score_and_analyze(item, matched)
        log(f"Score {score}/10 : {item['title'][:50]}")

        if score >= MIN_SCORE:
            msg = build_news_alert(item, matched, score, analysis, best_market)
            if send_telegram(msg):
                save_alert(conn, nid, item["source"], item["title"], matched, score)
                sent += 1
                log(f"✅ Alerte envoyée (score {score}) : {item['title'][:50]}")
            time.sleep(2)
        else:
            log(f"⏭️ Ignoré (score {score} < {MIN_SCORE})")

    if sent == 0:
        log(f"Aucune info n'a atteint le seuil de {MIN_SCORE}/10")


def is_expiring_soon(m, max_days=30):
    """Retourne True si le marché expire dans les 30 prochains jours"""
    end = m.get("endDate", "")
    if not end:
        return False
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        days_left = (end_dt - datetime.now(timezone.utc)).days
        return 0 < days_left <= max_days
    except:
        return False

def run_market_check():
    global markets_cache, seen_signals
    log("Scan Polymarket...")
    markets = fetch_markets()
    if not markets:
        return
    markets_cache = markets
    log(f"{len(markets)} marchés chargés")

    signals = [
        m for m in markets
        if abs(m["delta"]) >= 10
        and abs(m["delta"]) < 40
        and m["vol"] >= 10000
        and 30 < m["prob"] < 70
        and is_expiring_soon(m)
        and m["id"] + "-sig" not in seen_signals
    ]
    signals = sorted(signals, key=lambda m: abs(m["delta"]), reverse=True)[:2]

    for m in signals:
        d = m["delta"]
        ds = f"+{d}" if d > 0 else str(d)
        msg = (
            f"⚡ <b>MOUVEMENT POLYMARKET</b>\n\n"
            f"📊 {m['title']}\n"
            f"Probabilité : {m['prob']}% ({ds}pts / 24h)\n"
            f"Volume 24h : {fmt_vol(m['vol'])}\n"
            f"🔗 {build_link(m)}"
        )
        if send_telegram(msg):
            seen_signals.add(m["id"] + "-sig")
            log(f"Signal marché : {m['title'][:50]} ({ds}pts)")

def send_daily_report(conn):
    count, avg_score, top_sources = get_daily_stats(conn)
    if count == 0:
        return
    sources_str = "\n".join([f"• {s[0]} : {s[1]} alertes" for s in top_sources])
    msg = (
        f"📊 <b>RAPPORT QUOTIDIEN</b>\n\n"
        f"Alertes envoyées (24h) : {count}\n"
        f"Score moyen : {avg_score}/10\n\n"
        f"<b>Top sources :</b>\n{sources_str}"
    )
    send_telegram(msg)
    log("Rapport quotidien envoyé")

def main():
    log("=== Bot Polymarket Intelligence v3 démarré ===")
    if not TOKEN or not CHAT_ID:
        log("ERREUR : TOKEN ou CHAT_ID manquant")
        return

    conn = init_db()

    send_telegram(
        f"🟢 <b>Bot Polymarket Intelligence v3</b>\n\n"
        f"✅ {len(SOURCES)} sources surveillées\n"
        f"✅ Scan toutes les {INTERVAL} min\n"
        f"✅ Score IA minimum : {MIN_SCORE}/10\n"
        f"✅ Historique : activé\n"
        f"✅ Rapport quotidien : activé\n\n"
        f"Seules les infos scoring ≥{MIN_SCORE}/10 te seront envoyées."
    )

    log("Chargement initial des marchés...")
    markets_cache.extend(fetch_markets())
    log(f"{len(markets_cache)} marchés chargés")

    cycle = 0
    last_report = datetime.now(timezone.utc)

    while True:
        cycle += 1
        run_news_check(conn)
        check_calendar(conn, markets_cache)

        if cycle % max(1, POLY_INTERVAL // INTERVAL) == 0:
            run_market_check()

        # Rapport quotidien
        now = datetime.now(timezone.utc)
        if (now - last_report).total_seconds() >= 86400:
            send_daily_report(conn)
            last_report = now

        log(f"Prochain scan dans {INTERVAL} min...")
        time.sleep(INTERVAL * 60)

if __name__ == "__main__":
    main()
