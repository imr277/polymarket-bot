import os
import time
import json
import hashlib
import requests
from datetime import datetime, timezone
from xml.etree import ElementTree

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
REPORT_TOKEN = os.environ.get("REPORT_TOKEN", "")
REPORT_CHAT_ID = os.environ.get("REPORT_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INTERVAL = int(os.environ.get("INTERVAL_MINUTES", "3"))

TG_URL = f"https://api.telegram.org/bot{TOKEN}"
REPORT_URL = f"https://api.telegram.org/bot{REPORT_TOKEN}"
seen_signals = set()
prob_history = {}
signals_history = []
seen_news = set()

CAT_QUOTA = {"geopolitics": 2, "crypto": 2, "economics": 2, "politics": 2, "sports": 1, "other": 1}
CAT_LABELS = {"sports": "Sport", "crypto": "Crypto", "politics": "Politique", "geopolitics": "Geopolitique", "economics": "Economie", "other": "Autre"}

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def send_telegram(text):
    try:
        r = requests.post(f"{TG_URL}/sendMessage", json={
            "chat_id": CHAT_ID, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": True
        }, timeout=10)
        data = r.json()
        if not data.get("ok"):
            log(f"Telegram error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

def send_report(text):
    if not REPORT_TOKEN or not REPORT_CHAT_ID:
        return False
    try:
        r = requests.post(f"{REPORT_URL}/sendMessage", json={
            "chat_id": REPORT_CHAT_ID, "text": text[:4000],
            "parse_mode": "HTML", "disable_web_page_preview": True
        }, timeout=10)
        return r.json().get("ok", False)
    except:
        return False

def uid(text):
    return hashlib.md5(text.encode()).hexdigest()[:12]

def fmt_vol(n):
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000: return f"${round(n/1_000)}K"
    return f"${round(n)}"

def guess_cat(title):
    t = (title or "").lower()
    if any(k in t for k in ["nba","nfl","nhl","mlb","cup","champion","league","football","soccer","tennis","sport","match","game","season","olympics","rugby","formula","f1","ufc","boxing","golf","playoff","tournament","pitcher","quarterback","coach","championship","semifinal","qualifier"]):
        return "sports"
    if any(k in t for k in ["bitcoin","btc","eth","crypto","solana","token","defi","blockchain","nft","coinbase","binance","ethereum"]):
        return "crypto"
    if any(k in t for k in ["election","president","trump","biden","democrat","republican","vote","minister","parliament","congress","senate","governor","poll","ballot","primary"]):
        return "politics"
    if any(k in t for k in ["iran","russia","ukraine","china","war","peace","nuclear","military","nato","conflict","missile","troops","sanction","taiwan","israel","gaza","hamas","ceasefire","treaty"]):
        return "geopolitics"
    if any(k in t for k in ["fed","rate","inflation","gdp","recession","stock","economy","dollar","oil","gold","tariff","trade","interest","bond","unemployment","cpi","nfp","ecb"]):
        return "economics"
    return "other"

# ─── FILTRES STRICTS ──────────────────────────────────────────────────────────

def days_until_expiry(m):
    end = m.get("endDate", "")
    if not end:
        return 999
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        return (end_dt - datetime.now(timezone.utc)).days
    except:
        return 999

def passes_strict_filters(m):
    """Filtres obligatoires — si un seul échoue, le marché est ignoré"""
    prob = m["prob"]
    vol = m["vol"]
    delta = m["delta"]
    days = days_until_expiry(m)

    # 1. Volume minimum — marché liquide
    if vol < 5000:
        return False, f"volume trop bas ({fmt_vol(vol)})"

    # 2. Variation significative mais pas une résolution
    if abs(delta) < 8:
        return False, "variation insuffisante"
    if abs(delta) > 45:
        return False, "marche presque resolu"

    # 3. Expiration dans 3 à 30 jours
    if days < 3:
        return False, "expire dans moins de 3 jours"
    if days > 30:
        return False, f"expire dans {days} jours (trop loin)"

    # 4. Pas complètement résolu
    if prob <= 3 or prob >= 97:
        return False, "marche resolu"

    return True, "ok"

# ─── MARCHES ─────────────────────────────────────────────────────────────────

def fetch_markets_page(offset=0, limit=100):
    try:
        url = f"https://gamma-api.polymarket.com/markets?limit={limit}&offset={offset}&active=true&closed=false&order=volume&ascending=false"
        r = requests.get(url, timeout=15)
        if not r.ok:
            return []
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
            end_date = m.get("endDateIso") or m.get("endDate", "")
            if len(title) > 5:
                result.append({
                    "id": m.get("id", ""),
                    "title": title,
                    "prob": prob,
                    "vol": vol,
                    "delta": delta,
                    "slug": m.get("slug") or "",
                    "event_slug": event_slug,
                    "endDate": end_date,
                    "cat": guess_cat(title),
                })
        return result
    except Exception as e:
        log(f"Fetch error: {e}")
        return []

def fetch_all_markets():
    all_markets = []
    for offset in [0, 100, 200, 300, 400]:
        page = fetch_markets_page(offset=offset, limit=100)
        if not page:
            break
        all_markets.extend(page)
        time.sleep(0.5)
    log(f"Total marches : {len(all_markets)}")
    return all_markets

def build_link(m):
    event_slug = m.get("event_slug", "")
    slug = m.get("slug", "")
    if event_slug and not event_slug.isdigit() and len(event_slug) > 5:
        return f"https://polymarket.com/event/{event_slug}"
    if slug and not slug.isdigit() and len(slug) > 5:
        return f"https://polymarket.com/event/{slug}"
    return f"https://polymarket.com/search?q={m['title'][:60].replace(' ', '%20')}"

# ─── HISTORIQUE PROBABILITES ─────────────────────────────────────────────────

def update_prob_history(markets):
    now = time.time()
    for m in markets:
        mid = m["id"]
        if mid not in prob_history:
            prob_history[mid] = []
        prob_history[mid].append((now, m["prob"], m["vol"]))
        prob_history[mid] = [(t, p, v) for t, p, v in prob_history[mid] if now - t < 21600]

def get_prob_change(market_id, minutes_ago=60):
    now = time.time()
    history = prob_history.get(market_id, [])
    target_time = now - (minutes_ago * 60)
    old_points = [(t, p, v) for t, p, v in history if abs(t - target_time) < 900]
    if not old_points:
        return None, None
    return old_points[-1][1], old_points[-1][2]

# ─── DETECTION OPPORTUNITES ──────────────────────────────────────────────────

def detect_opportunities(markets):
    scored = []
    filtered_out = {"prob": 0, "volume": 0, "delta": 0, "expiry": 0}

    for m in markets:
        mid = m["id"]

        # Filtres stricts obligatoires
        passes, reason = passes_strict_filters(m)
        if not passes:
            if "prob" in reason: filtered_out["prob"] += 1
            elif "volume" in reason: filtered_out["volume"] += 1
            elif "resolu" in reason or "variation" in reason: filtered_out["delta"] += 1
            elif "jours" in reason: filtered_out["expiry"] += 1
            continue

        if mid + "-opp" in seen_signals:
            continue

        # Score de qualité
        prob = m["prob"]
        vol = m["vol"]
        delta = m["delta"]
        cat = m["cat"]
        days = days_until_expiry(m)

        old_prob_1h, old_vol_1h = get_prob_change(mid, 60)
        prob_change_1h = (prob - old_prob_1h) if old_prob_1h is not None else 0
        vol_change_1h = ((vol - old_vol_1h) / old_vol_1h * 100) if old_vol_1h and old_vol_1h > 0 else 0

        score = 0
        reasons = []

        # Variation 24h
        if abs(delta) >= 15:
            score += 3
            reasons.append(f"{'+' if delta>0 else ''}{delta}pts/24h")
        elif abs(delta) >= 8:
            score += 1
            reasons.append(f"{'+' if delta>0 else ''}{delta}pts/24h")

        # Surge 1h
        if abs(prob_change_1h) >= 5 and vol_change_1h >= 30:
            score += 3
            reasons.append(f"surge {'+' if prob_change_1h>0 else ''}{prob_change_1h:.0f}pts/1h")

        # Volume
        if vol >= 100000:
            score += 3
        elif vol >= 50000:
            score += 2
        elif vol >= 10000:
            score += 1

        # Expiration imminente (plus urgent = plus intéressant)
        if days <= 7:
            score += 2
        elif days <= 14:
            score += 1

        # Bonus catégories prioritaires
        if cat in ["geopolitics", "economics", "crypto", "politics"]:
            score += 1

        scored.append({
            "market": m,
            "score": score,
            "reasons": reasons,
            "prob_change_1h": prob_change_1h,
            "vol_change_1h": vol_change_1h,
            "cat": cat,
            "days": days,
        })

    log(f"Filtres: prob={filtered_out['prob']} vol={filtered_out['volume']} delta={filtered_out['delta']} expiry={filtered_out['expiry']}")

    # Trier par score
    scored.sort(key=lambda x: (x["score"], x["market"]["vol"]), reverse=True)

    # Appliquer quota par catégorie
    cat_counts = {cat: 0 for cat in CAT_QUOTA}
    selected = []
    for opp in scored:
        cat = opp["cat"]
        quota = CAT_QUOTA.get(cat, 1)
        if cat_counts.get(cat, 0) < quota:
            selected.append(opp)
            cat_counts[cat] = cat_counts.get(cat, 0) + 1
        if len(selected) >= 5:
            break

    return selected

# ─── ANALYSE ─────────────────────────────────────────────────────────────────

def build_default_analysis(opp):
    m = opp["market"]
    delta = m["delta"]
    prob_change = opp["prob_change_1h"]
    vol_change = opp["vol_change_1h"]

    if delta > 0 or prob_change > 0:
        action = "ACHETER OUI"
        conviction = "FORTE" if opp["score"] >= 5 else "MOYENNE"
        analyse = f"Prob en hausse ({'+' if delta>0 else ''}{delta}pts/24h"
        if abs(prob_change) >= 5:
            analyse += f", +{prob_change:.0f}pts/1h"
        analyse += f"). Vol : {fmt_vol(m['vol'])}."
        if vol_change >= 30:
            analyse += f" Surge volume +{vol_change:.0f}%."
    else:
        action = "ACHETER NON"
        conviction = "FORTE" if opp["score"] >= 5 else "MOYENNE"
        analyse = f"Prob en baisse ({delta}pts/24h"
        if abs(prob_change) >= 5:
            analyse += f", {prob_change:.0f}pts/1h"
        analyse += f"). Vol : {fmt_vol(m['vol'])}."
        if vol_change >= 30:
            analyse += f" Surge volume +{vol_change:.0f}%."

    return {"action": action, "conviction": conviction, "analyse": analyse, "risque": "Verifier les actualites avant d entrer."}

def analyze_with_claude(opp):
    if not ANTHROPIC_KEY:
        return None
    m = opp["market"]
    reasons_str = ", ".join(opp["reasons"])
    prob_change = opp["prob_change_1h"]
    prompt = (
        "Tu es un trader expert en marches de prediction Polymarket.\n\n"
        f"MARCHE : {m['title']}\n"
        f"Categorie : {CAT_LABELS.get(opp['cat'], 'Autre')}\n"
        f"Probabilite : {m['prob']}% | Variation 24h : {'+' if m['delta']>0 else ''}{m['delta']}pts\n"
        f"Variation 1h : {'+' if prob_change>0 else ''}{prob_change:.0f}pts | Volume : {fmt_vol(m['vol'])}\n"
        f"Expire dans : {opp['days']} jours\n"
        f"Signaux : {reasons_str}\n\n"
        'Reponds UNIQUEMENT en JSON : {"action": "<ACHETER OUI/ACHETER NON/PASSER>", "conviction": "<FORTE/MOYENNE/FAIBLE>", "analyse": "<2 phrases max en francais>", "risque": "<1 phrase>"}'
    )
    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 200, "messages": [{"role": "user", "content": prompt}]},
            timeout=20
        )
        text = r.json()["content"][0]["text"].strip().replace("```json","").replace("```","").strip()
        return json.loads(text)
    except Exception as e:
        log(f"Claude error: {e}")
        return None

def build_alert(opp, analysis):
    m = opp["market"]
    action = analysis.get("action", "")
    conviction = analysis.get("conviction", "")
    analyse = analysis.get("analyse", "")
    risque = analysis.get("risque", "")
    prob_change = opp["prob_change_1h"]
    vol_change = opp["vol_change_1h"]
    score = opp["score"]
    cat = opp["cat"]
    days = opp["days"]

    icon = "🟢" if action == "ACHETER OUI" else "🔴"
    conv_icon = "🔥" if conviction == "FORTE" else "🟡"
    ai_label = "🤖 IA" if score >= 9 else "📊"

    msg = (
        f"{icon} <b>OPPORTUNITE [{score}pts] — {CAT_LABELS.get(cat, 'Autre').upper()}</b>\n\n"
        f"📊 {m['title']}\n\n"
        f"Prob : <b>{m['prob']}%</b> ({'+' if m['delta']>0 else ''}{m['delta']}pts/24h)\n"
    )
    if abs(prob_change) >= 3:
        msg += f"1h : {'+' if prob_change>0 else ''}{prob_change:.0f}pts\n"
    if vol_change >= 20:
        msg += f"Volume : +{vol_change:.0f}% en 1h\n"
    msg += f"Vol 24h : {fmt_vol(m['vol'])} | Expire : {days}j\n\n"
    msg += f"{conv_icon} <b>{action}</b> — {conviction} {ai_label}\n\n"
    msg += f"💡 {analyse}\n"
    if risque:
        msg += f"⚠️ {risque}\n"
    msg += f"\n🔗 {build_link(m)}"
    return msg

# ─── SUIVI ET RAPPORT ────────────────────────────────────────────────────────

def track_signal(market, action):
    signals_history.append({
        "id": market["id"],
        "title": market["title"],
        "action": action,
        "prob_entry": market["prob"],
        "prob_current": market["prob"],
        "sent_at": time.time(),
        "link": build_link(market),
        "end_date": market.get("endDate", ""),
        "cat": market.get("cat", "other"),
        "reported": False,
    })
    if len(signals_history) > 100:
        signals_history.pop(0)

def update_signal_results(markets):
    market_probs = {m["id"]: m["prob"] for m in markets}
    for sig in signals_history:
        if sig["id"] in market_probs:
            sig["prob_current"] = market_probs[sig["id"]]

def check_signal_results():
    now = time.time()
    for sig in signals_history:
        if sig.get("reported"):
            continue
        if now - sig["sent_at"] < 1800:
            continue

        current_prob = sig["prob_current"]
        resolved_yes = current_prob >= 95
        resolved_no = current_prob <= 5

        expired = False
        end_date = sig.get("end_date", "")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                expired = datetime.now(timezone.utc) >= end_dt
            except:
                pass

        if not resolved_yes and not resolved_no and not expired:
            continue

        if expired and not resolved_yes and not resolved_no:
            resolved_yes = current_prob >= 50
            resolved_no = current_prob < 50

        resolution = "OUI" if resolved_yes else "NON"
        result = "WIN" if (sig["action"] == "ACHETER OUI" and resolved_yes) or (sig["action"] == "ACHETER NON" and resolved_no) else "LOSE"
        sig["reported"] = True

        prob_change = current_prob - sig["prob_entry"]
        ds = f"+{prob_change:.0f}" if prob_change > 0 else f"{prob_change:.0f}"
        sent_at_str = datetime.fromtimestamp(sig["sent_at"], tz=timezone.utc).strftime("%d/%m %H:%M")
        resolved_at_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M")
        result_icon = "✅" if result == "WIN" else "❌"

        msg = (
            f"{result_icon} <b>MARCHE RESOLU — {'GAGNANT' if result == 'WIN' else 'PERDANT'}</b>\n"
            f"<i>{CAT_LABELS.get(sig.get('cat','other'), 'Autre')}</i>\n\n"
            f"📊 {sig['title']}\n\n"
            f"━━━━━━━━━━━━━\n"
            f"{'🟢' if sig['action'] == 'ACHETER OUI' else '🔴'} Action : <b>{sig['action']}</b>\n"
            f"Signal le : {sent_at_str} UTC\n"
            f"Prob au signal : {sig['prob_entry']}%\n"
            f"━━━━━━━━━━━━━\n"
            f"Resultat : <b>{resolution}</b>\n"
            f"Prob finale : {current_prob}% ({ds}pts)\n"
            f"Resolu le : {resolved_at_str} UTC\n"
            f"━━━━━━━━━━━━━\n\n"
        )
        if expired and not (current_prob >= 95 or current_prob <= 5):
            msg += "⏰ Cloture a la date d expiration.\n"
        msg += "✅ Tu avais le bon cote !\n" if result == "WIN" else "❌ Le marche a resolu dans l autre sens.\n"
        msg += f"\n🔗 {sig['link']}"

        send_report(msg)
        log(f"Resultat : {sig['title'][:50]} → {'WIN' if result == 'WIN' else 'LOSE'}")
        time.sleep(2)

# ─── SOURCES NEWS ────────────────────────────────────────────────────────────

NEWS_SOURCES = [
    # GEOPOLITIQUE & MONDE
    {"name": "Reuters",          "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP News",          "url": "https://rsshub.app/apnews/topics/apf-topnews"},
    {"name": "BBC World",        "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera",       "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "DW News",          "url": "https://rss.dw.com/rdf/rss-en-all"},
    {"name": "UN News",          "url": "https://news.un.org/feed/subscribe/en/news/all/rss.xml"},
    {"name": "The Guardian",     "url": "https://www.theguardian.com/world/rss"},
    {"name": "Foreign Affairs",  "url": "https://www.foreignaffairs.com/rss.xml"},
    {"name": "Euronews",         "url": "https://www.euronews.com/rss?level=theme&name=news"},
    {"name": "RFI",              "url": "https://www.rfi.fr/fr/rss-podcasts/rss_actualites.xml"},
    {"name": "Le Monde",         "url": "https://www.lemonde.fr/rss/une.xml"},
    # POLITIQUE
    {"name": "Politico",         "url": "https://rss.politico.com/politics-news.xml"},
    {"name": "The Hill",         "url": "https://thehill.com/news/feed/"},
    {"name": "White House",      "url": "https://www.whitehouse.gov/feed/"},
    {"name": "NATO",             "url": "https://www.nato.int/cps/en/natohq/news.xml"},
    # ECONOMIE & FINANCE
    {"name": "MarketWatch",      "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "Bloomberg",        "url": "https://feeds.bloomberg.com/markets/news.rss"},
    {"name": "Financial Times",  "url": "https://www.ft.com/rss/home"},
    {"name": "The Economist",    "url": "https://www.economist.com/latest/rss.xml"},
    {"name": "Les Echos",        "url": "https://services.lesechos.fr/rss/les-echos-finance.xml"},
    {"name": "Fed Reserve",      "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "IMF",              "url": "https://www.imf.org/en/News/rss?language=eng"},
    # CRYPTO
    {"name": "CoinDesk",         "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "Cointelegraph",    "url": "https://cointelegraph.com/rss"},
    {"name": "The Block",        "url": "https://www.theblock.co/rss.xml"},
    {"name": "Decrypt",          "url": "https://decrypt.co/feed"},
    {"name": "Bitcoin Magazine", "url": "https://bitcoinmagazine.com/feed"},
    # SPORT
    {"name": "BBC Sport",        "url": "https://feeds.bbci.co.uk/sport/rss.xml"},
    {"name": "ESPN",             "url": "https://www.espn.com/espn/rss/news"},
    # REDDIT
    {"name": "r/Polymarket",     "url": "https://www.reddit.com/r/Polymarket/hot.json?limit=15", "type": "reddit"},
    {"name": "r/PredictionMarkets","url": "https://www.reddit.com/r/PredictionMarkets/hot.json?limit=10", "type": "reddit"},
    {"name": "r/worldnews",      "url": "https://www.reddit.com/r/worldnews/hot.json?limit=15", "type": "reddit"},
    {"name": "r/geopolitics",    "url": "https://www.reddit.com/r/geopolitics/hot.json?limit=10", "type": "reddit"},
    {"name": "r/CryptoCurrency", "url": "https://www.reddit.com/r/CryptoCurrency/hot.json?limit=10", "type": "reddit"},
    {"name": "r/investing",      "url": "https://www.reddit.com/r/investing/hot.json?limit=10", "type": "reddit"},
    {"name": "r/politics",       "url": "https://www.reddit.com/r/politics/hot.json?limit=10", "type": "reddit"},
    {"name": "r/europe",         "url": "https://www.reddit.com/r/europe/hot.json?limit=10", "type": "reddit"},
    # TELEGRAM CHANNELS
    {"name": "@BreakingNews",    "url": "https://rsshub.app/telegram/channel/BreakingNews"},
    {"name": "@disclosetv",      "url": "https://rsshub.app/telegram/channel/disclosetv"},
    {"name": "@sentdefender",    "url": "https://rsshub.app/telegram/channel/sentdefender"},
    {"name": "@IntelSlava",      "url": "https://rsshub.app/telegram/channel/IntelSlava"},
    {"name": "@CoinDeskNews",    "url": "https://rsshub.app/telegram/channel/CoinDeskNews"},
    {"name": "@Cointelegraph",   "url": "https://rsshub.app/telegram/channel/cointelegraph"},
    {"name": "@warnewsua",       "url": "https://rsshub.app/telegram/channel/warnewsua"},
    {"name": "@BBCBreaking",     "url": "https://rsshub.app/telegram/channel/BBCBreaking"},
]

def fetch_rss(src):
    try:
        r = requests.get("https://api.allorigins.win/get?url=" + requests.utils.quote(src["url"]), timeout=8)
        j = r.json()
        xml = ElementTree.fromstring(j["contents"])
        items = xml.findall(".//item") or xml.findall(".//{http://www.w3.org/2005/Atom}entry")
        news = []
        for i in items[:8]:
            title = (i.findtext("title") or i.findtext("{http://www.w3.org/2005/Atom}title") or "")
            title = title.replace("<![CDATA[", "").replace("]]>", "").strip()
            link = (i.findtext("link") or "").strip()
            if title and len(title) > 10:
                news.append({"title": title, "link": link, "source": src["name"]})
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
                news.append({"title": title, "link": "https://reddit.com" + d.get("permalink", ""), "source": src["name"]})
        return news
    except:
        return []

def fetch_all_news():
    all_news = []
    for src in NEWS_SOURCES:
        if src.get("type") == "reddit":
            all_news.extend(fetch_reddit(src))
        else:
            all_news.extend(fetch_rss(src))
    return all_news

def match_news_to_market(news_list, market):
    stop = {"the","a","an","is","are","to","of","in","on","at","by","for","and","or","be","it","will","has","have","was","were","not","with","from","that","this","do","can","if","but"}
    mkt_words = {w for w in market["title"].lower().replace("?","").replace(",","").split() if len(w) > 3} - stop
    matched = []
    for n in news_list:
        news_words = {w for w in n["title"].lower().replace("?","").replace(",","").split() if len(w) > 3} - stop
        common = mkt_words & news_words
        if len(common) >= 2:
            matched.append((n, len(common)))
    matched.sort(key=lambda x: x[1], reverse=True)
    return [n for n, _ in matched[:2]]

# ─── BOUCLE PRINCIPALE ───────────────────────────────────────────────────────

cycle = 0
all_markets_cache = []

def run():
    global cycle, all_markets_cache, seen_news
    cycle += 1
    log(f"=== Cycle #{cycle} ===")

    all_markets_cache = fetch_all_markets()
    if not all_markets_cache:
        log("Aucun marche charge")
        return

    update_prob_history(all_markets_cache)
    update_signal_results(all_markets_cache)
    check_signal_results()

    news = fetch_all_news()
    new_news = []
    for n in news:
        nid = uid(n["title"] + n["source"])
        if nid not in seen_news:
            seen_news.add(nid)
            new_news.append(n)

    opportunities = detect_opportunities(all_markets_cache)
    log(f"{len(opportunities)} opportunites selectionnees")

    sent = 0
    for opp in opportunities:
        m = opp["market"]
        mid = m["id"]

        if opp["score"] >= 9:
            log(f"Claude (score {opp['score']}) : {m['title'][:50]}")
            analysis = analyze_with_claude(opp)
            if not analysis:
                analysis = build_default_analysis(opp)
            if analysis.get("action") == "PASSER":
                continue
        else:
            analysis = build_default_analysis(opp)

        msg = build_alert(opp, analysis)

        related = match_news_to_market(new_news + news[:30], m)
        if related:
            msg += "\n\n📰 <b>News liees :</b>"
            for n in related:
                msg += f"\n• {n['source']}: {n['title'][:60]}"

        if send_telegram(msg):
            seen_signals.add(mid + "-opp")
            track_signal(m, analysis.get("action", ""))
            sent += 1
            log(f"Signal [{opp['cat']}] score={opp['score']} : {m['title'][:50]}")
        time.sleep(2)

        if sent >= 5:
            break

    if sent == 0:
        log("Aucune opportunite ce cycle")

def main():
    log("=== Bot Polymarket Intelligence v4 ===")
    if not TOKEN or not CHAT_ID:
        log("ERREUR : TOKEN ou CHAT_ID manquant")
        return

    send_telegram(
        "🟢 <b>Bot Polymarket Intelligence v4</b>\n\n"
        "✅ 500+ marches scannes\n"
        "✅ Filtres stricts : prob 25-75%, vol $5K+, expiration 3-30 jours\n"
        "✅ Max 1 signal sportif par cycle\n"
        "✅ Priorite : Geopolitique, Crypto, Economie, Politique\n"
        "✅ Resultats sur bot rapport quand marche resolu\n"
        f"✅ Scan toutes les {INTERVAL} min"
    )

    send_report(
        "📊 <b>Bot Rapport Polymarket demarre</b>\n\n"
        "Tu recevras ici le resultat de chaque signal\n"
        "des que le marche est resolu.\n\n"
        "✅ GAGNANT — signal valide\n"
        "❌ PERDANT — mauvais sens"
    )

    log("Chargement initial...")
    initial = fetch_all_markets()
    all_markets_cache.extend(initial)
    update_prob_history(all_markets_cache)
    log(f"{len(all_markets_cache)} marches charges")

    while True:
        run()
        log(f"Prochain scan dans {INTERVAL} min...")
        time.sleep(INTERVAL * 60)

if __name__ == "__main__":
    main()
