import os
import time
import json
import hashlib
import requests
from datetime import datetime, timezone
from xml.etree import ElementTree

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INTERVAL = int(os.environ.get("INTERVAL_MINUTES", "3"))
POLY_INTERVAL = int(os.environ.get("POLY_INTERVAL_MINUTES", "5"))

TG_URL = f"https://api.telegram.org/bot{TOKEN}"
POLYMARKET_URL = "https://gamma-api.polymarket.com/markets?limit=50&active=true&closed=false&order=volume&ascending=false"

seen_news = set()
seen_signals = set()
markets_cache = []
last_poly_check = 0

# ─── SOURCES D'INFORMATION BRUTES ───────────────────────────────────────────

SOURCES = [
    # GÉOPOLITIQUE
    {"name": "Reuters Top News",     "cat": "geo",      "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP World",             "cat": "geo",      "url": "https://rsshub.app/apnews/topics/apf-topnews"},
    {"name": "Al Jazeera",           "cat": "geo",      "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "BBC World",            "cat": "geo",      "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "DW News",              "cat": "geo",      "url": "https://rss.dw.com/rdf/rss-en-all"},
    {"name": "UN News",              "cat": "geo",      "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml"},
    {"name": "Foreign Affairs",      "cat": "geo",      "url": "https://www.foreignaffairs.com/rss.xml"},
    {"name": "The Guardian World",   "cat": "geo",      "url": "https://www.theguardian.com/world/rss"},
    # POLITIQUE
    {"name": "Politico",             "cat": "politics", "url": "https://rss.politico.com/politics-news.xml"},
    {"name": "The Hill",             "cat": "politics", "url": "https://thehill.com/news/feed/"},
    {"name": "RFI",                  "cat": "politics", "url": "https://www.rfi.fr/fr/rss-podcasts/rss_actualites.xml"},
    {"name": "Le Monde",             "cat": "politics", "url": "https://www.lemonde.fr/rss/une.xml"},
    # CRYPTO
    {"name": "CoinDesk",             "cat": "crypto",   "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph",        "cat": "crypto",   "url": "https://cointelegraph.com/rss"},
    {"name": "The Block",            "cat": "crypto",   "url": "https://www.theblock.co/rss.xml"},
    {"name": "Decrypt",              "cat": "crypto",   "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin Magazine",     "cat": "crypto",   "url": "https://bitcoinmagazine.com/feed"},
    # ÉCONOMIE
    {"name": "MarketWatch",          "cat": "economics","url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "Les Echos",            "cat": "economics","url": "https://services.lesechos.fr/rss/les-echos-finance.xml"},
    {"name": "Bloomberg Markets",    "cat": "economics","url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Financial Times",      "cat": "economics","url": "https://www.ft.com/rss/home"},
    {"name": "The Economist",        "cat": "economics","url": "https://www.economist.com/latest/rss.xml"},
    # REDDIT
    {"name": "r/worldnews",          "cat": "geo",      "url": "https://www.reddit.com/r/worldnews/hot.json?limit=15", "type": "reddit"},
    {"name": "r/geopolitics",        "cat": "geo",      "url": "https://www.reddit.com/r/geopolitics/hot.json?limit=10", "type": "reddit"},
    {"name": "r/CryptoCurrency",     "cat": "crypto",   "url": "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=10", "type": "reddit"},
    {"name": "r/investing",          "cat": "economics","url": "https://www.reddit.com/r/investing/hot.json?limit=10", "type": "reddit"},
    {"name": "r/politics",           "cat": "politics", "url": "https://www.reddit.com/r/politics/hot.json?limit=10", "type": "reddit"},
    {"name": "r/Polymarket",         "cat": "all",      "url": "https://www.reddit.com/r/Polymarket/hot.json?limit=10", "type": "reddit"},
    # TELEGRAM CHANNELS (via RSS Bridge)
    {"name": "@BreakingNews",    "cat": "geo",      "url": "https://rsshub.app/telegram/channel/BreakingNews"},
    {"name": "@disclosetv",      "cat": "geo",      "url": "https://rsshub.app/telegram/channel/disclosetv"},
    {"name": "@sentdefender",    "cat": "geo",      "url": "https://rsshub.app/telegram/channel/sentdefender"},
    {"name": "@IntelSlava",      "cat": "geo",      "url": "https://rsshub.app/telegram/channel/IntelSlava"},
    {"name": "@warnewsua",       "cat": "geo",      "url": "https://rsshub.app/telegram/channel/warnewsua"},
    {"name": "@Reuters",         "cat": "geo",      "url": "https://rsshub.app/telegram/channel/Reuters"},
    {"name": "@BBCBreaking",     "cat": "geo",      "url": "https://rsshub.app/telegram/channel/BBCBreaking"},
    {"name": "@CoinDeskNews",    "cat": "crypto",   "url": "https://rsshub.app/telegram/channel/CoinDeskNews"},
    {"name": "@Cointelegraph",   "cat": "crypto",   "url": "https://rsshub.app/telegram/channel/cointelegraph"},
    {"name": "@MarketWatch",     "cat": "economics","url": "https://rsshub.app/telegram/channel/MarketWatch"},
]

# Mots-clés à haute valeur qui signalent une info exploitable
HIGH_VALUE_KEYWORDS = [
    # Géopolitique
    "attack","strike","missile","troops","invasion","ceasefire","war","nuclear",
    "sanction","embargo","coup","assassination","explosion","airspace","closed",
    "troops","deployed","escalat","conflict","bomb","rocket","drone",
    # Politique
    "resign","impeach","elect","vote","poll","president","prime minister",
    "emergency","arrest","indicted","guilty","verdict","trial","ban",
    "executive order","law passed","veto","senate","congress",
    # Crypto
    "hack","exploit","crash","surge","etf approved","sec","regulation",
    "ban","listing","delisting","bankruptcy","seized","halted",
    # Économie
    "rate hike","rate cut","inflation","recession","gdp","unemployment",
    "fed","ecb","interest rate","default","crisis","bailout","tariff",
    "sanctions","trade war","deficit","surplus",
    # Signaux urgents
    "breaking","urgent","alert","flash","developing","just in","confirmed",
    "official","announces","declares","signs","agrees","rejects",
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

def is_high_value(title):
    t = title.lower()
    return any(k in t for k in HIGH_VALUE_KEYWORDS)

def fetch_rss(src):
    try:
        r = requests.get(
            f"https://api.allorigins.win/get?url={requests.utils.quote(src['url'])}",
            timeout=8
        )
        j = r.json()
        xml = ElementTree.fromstring(j["contents"])
        items = xml.findall(".//item")
        news = []
        for i in items[:10]:
            title = (i.findtext("title") or "").replace("<![CDATA[","").replace("]]>","").strip()
            link = (i.findtext("link") or "").strip()
            date = i.findtext("pubDate") or datetime.now(timezone.utc).isoformat()
            if title and len(title) > 10:
                news.append({"title": title, "link": link, "date": date, "source": src["name"], "cat": src["cat"]})
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
            if title and len(title) > 10:
                news.append({
                    "title": title,
                    "link": "https://reddit.com" + d.get("permalink",""),
                    "date": datetime.now(timezone.utc).isoformat(),
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

def analyze_opportunity(news_item, markets):
    if not ANTHROPIC_KEY or not markets:
        return None
    try:
        markets_str = "\n".join([
            f"- {m['title']} → prob actuelle {m['prob']}% (variation {'+' if m['delta']>0 else ''}{m['delta']}pts, vol {fmt_vol(m['vol'])})"
            for m in markets
        ])
        prompt = (
            f"Tu es un trader expert en marchés de prédiction Polymarket. Une info vient de sortir :\n\n"
            f"SOURCE : {news_item['source']}\n"
            f"INFO : {news_item['title']}\n\n"
            f"MARCHÉS POLYMARKET LIÉS :\n{markets_str}\n\n"
            f"Réponds en français, de façon ultra-concise et directe (3-4 phrases max) :\n"
            f"1. Quel est l'impact probable de cette info sur ces marchés ?\n"
            f"2. Quel marché est le plus intéressant et dans quel sens (OUI ou NON) ?\n"
            f"3. Est-ce urgent d'agir ou peut-on attendre ?"
        )
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json"
            },
            json={
                "model": "claude-haiku-4-5-20251001",
                "max_tokens": 250,
                "messages": [{"role": "user", "content": prompt}]
            },
            timeout=20
        )
        data = r.json()
        if data.get("content"):
            return data["content"][0]["text"].strip()
        return None
    except Exception as e:
        log(f"Claude API error: {e}")
        return None

def build_news_alert(news_item, matched_markets, analysis):
    cat_icons = {"geo": "🌍", "politics": "🏛️", "crypto": "₿", "economics": "📈", "all": "🔔"}
    icon = cat_icons.get(news_item["cat"], "📰")
    msg = f"{icon} <b>INFO DÉTECTÉE — {news_item['source'].upper()}</b>\n\n"
    msg += f"📰 {news_item['title']}\n"
    if news_item.get("link"):
        msg += f"🔗 {news_item['link']}\n"
    if matched_markets:
        msg += f"\n📊 <b>MARCHÉS POLYMARKET LIÉS</b>\n"
        for m in matched_markets:
            d = m["delta"]
            ds = f"+{d}" if d > 0 else str(d)
            msg += f"• {m['title'][:60]}\n  → {m['prob']}% · {ds}pts · {fmt_vol(m['vol'])}\n  {build_link(m)}\n"
    if analysis:
        msg += f"\n🤖 <b>Analyse</b>\n{analysis}"
    return msg

def run_news_check():
    global seen_news
    log("Scan des sources d'information...")
    news = fetch_all_news()
    new_items = []

    for item in news:
        nid = news_id(item["title"], item["source"])
        if nid in seen_news:
            continue
        seen_news.add(nid)
        if is_high_value(item["title"]):
            new_items.append(item)

    if not new_items:
        log("Aucune nouvelle info à haute valeur")
        return

    log(f"{len(new_items)} nouvelles infos à haute valeur détectées")

    # Limite à 3 alertes par cycle pour ne pas spammer
    for item in new_items[:3]:
        matched = match_news_to_markets(item, markets_cache)
        analysis = analyze_opportunity(item, matched) if matched else None
        msg = build_news_alert(item, matched, analysis)
        if send_telegram(msg):
            log(f"Alerte info envoyée : {item['title'][:60]}")
        time.sleep(2)

def run_market_check():
    global markets_cache, last_poly_check, seen_signals
    log("Scan Polymarket...")
    markets = fetch_markets()
    if not markets:
        return
    markets_cache = markets
    last_poly_check = time.time()
    log(f"{len(markets)} marchés chargés")

    signals = [
        m for m in markets
        if abs(m["delta"]) >= 15
        and abs(m["delta"]) < 95
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

def main():
    log("=== Bot Polymarket Intelligence démarré ===")
    if not TOKEN or not CHAT_ID:
        log("ERREUR : TOKEN ou CHAT_ID manquant")
        return

    send_telegram(
        f"🟢 <b>Bot Polymarket Intelligence démarré</b>\n\n"
        f"✅ Surveillance de {len(SOURCES)} sources actives\n"
        f"✅ Scan news toutes les {INTERVAL} min\n"
        f"✅ Scan Polymarket toutes les {POLY_INTERVAL} min\n"
        f"✅ Analyse IA : {'activée' if ANTHROPIC_KEY else 'désactivée'}\n\n"
        f"Tu recevras une alerte dès qu'une info exploitable est détectée."
    )

    # Charger les marchés au démarrage
    log("Chargement initial des marchés...")
    markets_cache.extend(fetch_markets())
    log(f"{len(markets_cache)} marchés chargés")

    cycle = 0
    while True:
        cycle += 1
        # Scan des news à chaque cycle
        run_news_check()

        # Scan Polymarket tous les N cycles
        if cycle % max(1, POLY_INTERVAL // INTERVAL) == 0:
            run_market_check()

        log(f"Prochain scan dans {INTERVAL} min...")
        time.sleep(INTERVAL * 60)

if __name__ == "__main__":
    main()
# Ce bloc est juste un test — on vérifie que le fichier existe
