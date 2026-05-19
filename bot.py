import os
import time
import json
import hashlib
import requests
import sqlite3
from datetime import datetime, timezone, timedelta
from xml.etree import ElementTree

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
REPORT_TOKEN = os.environ.get("REPORT_TOKEN", "")
REPORT_CHAT_ID = os.environ.get("REPORT_CHAT_ID", "")
ANTHROPIC_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
INTERVAL = int(os.environ.get("INTERVAL_MINUTES", "3"))
MIN_SCORE = int(os.environ.get("MIN_SCORE", "7"))

TG_URL = f"https://api.telegram.org/bot{TOKEN}"
REPORT_URL = f"https://api.telegram.org/bot{REPORT_TOKEN}"
seen_signals = set()
prob_history = {}

# Historique des signaux pour les rapports
signals_history = []  # [{id, title, action, prob_entry, prob_current, sent_at}]

def send_report(text):
    """Envoie sur le bot de rapport séparé"""
    if not REPORT_TOKEN or not REPORT_CHAT_ID:
        return False
    try:
        r = requests.post(f"{REPORT_URL}/sendMessage", json={
            "chat_id": REPORT_CHAT_ID,
            "text": text[:4000],
            "parse_mode": "HTML",
            "disable_web_page_preview": True
        }, timeout=10)
        return r.json().get("ok", False)
    except:
        return False

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

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

def uid(text):
    return hashlib.md5(text.encode()).hexdigest()[:12]

def fmt_vol(n):
    if n >= 1_000_000: return f"${n/1_000_000:.1f}M"
    if n >= 1_000: return f"${round(n/1_000)}K"
    return f"${round(n)}"

# ─── RÉCUPÉRATION MAXIMALE DES MARCHÉS ───────────────────────────────────────

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
                })
        return result
    except Exception as e:
        log(f"Fetch error offset={offset}: {e}")
        return []

def fetch_all_markets():
    """Récupère un maximum de marchés en paginant"""
    all_markets = []
    offsets = [0, 100, 200, 300, 400]
    for offset in offsets:
        page = fetch_markets_page(offset=offset, limit=100)
        if not page:
            break
        all_markets.extend(page)
        time.sleep(0.5)
    log(f"Total marchés récupérés : {len(all_markets)}")
    return all_markets

def build_link(m):
    event_slug = m.get("event_slug", "")
    slug = m.get("slug", "")
    if event_slug and not event_slug.isdigit() and len(event_slug) > 5:
        return f"https://polymarket.com/event/{event_slug}"
    if slug and not slug.isdigit() and len(slug) > 5:
        return f"https://polymarket.com/event/{slug}"
    query = m["title"][:60].replace(" ", "%20")
    return f"https://polymarket.com/search?q={query}"

def is_expiring_soon(m, max_days=30):
    end = m.get("endDate", "")
    if not end:
        return True  # Si pas de date, on garde
    try:
        end_dt = datetime.fromisoformat(end.replace("Z", "+00:00"))
        days_left = (end_dt - datetime.now(timezone.utc)).days
        return 0 < days_left <= max_days
    except:
        return True

# ─── DÉTECTION DES OPPORTUNITÉS ──────────────────────────────────────────────

def update_prob_history(markets):
    now = time.time()
    for m in markets:
        mid = m["id"]
        if mid not in prob_history:
            prob_history[mid] = []
        prob_history[mid].append((now, m["prob"], m["vol"]))
        # Garder 6 heures d'historique
        prob_history[mid] = [(t, p, v) for t, p, v in prob_history[mid] if now - t < 21600]

def get_prob_change(market_id, minutes_ago=60):
    now = time.time()
    history = prob_history.get(market_id, [])
    target_time = now - (minutes_ago * 60)
    old_points = [(t, p, v) for t, p, v in history if abs(t - target_time) < 900]
    if not old_points:
        return None, None
    old_prob = old_points[-1][1]
    old_vol = old_points[-1][2]
    return old_prob, old_vol

def detect_opportunities(markets):
    """Détecte toutes les opportunités exploitables"""
    opportunities = []
    now = time.time()

    for m in markets:
        mid = m["id"]
        prob = m["prob"]
        vol = m["vol"]
        delta = m["delta"]

        # Ignorer les marchés résolus ou sans volume
        if prob <= 2 or prob >= 98:
            continue
        if vol < 500:
            continue

        # Calculer le changement de prob sur 1h
        old_prob_1h, old_vol_1h = get_prob_change(mid, 60)
        prob_change_1h = (prob - old_prob_1h) if old_prob_1h is not None else 0
        vol_change_1h = ((vol - old_vol_1h) / old_vol_1h * 100) if old_vol_1h and old_vol_1h > 0 else 0

        # Score d'opportunité
        score = 0
        reasons = []

        # 1. Mouvement de prob significatif sur 24h
        if abs(delta) >= 8 and abs(delta) < 50:
            score += 2
            reasons.append(f"variation {'+' if delta>0 else ''}{delta}pts/24h")

        # 2. Surge en 1h (prob + volume)
        if abs(prob_change_1h) >= 5 and vol_change_1h >= 30:
            score += 3
            reasons.append(f"surge +{prob_change_1h:.0f}pts en 1h (+{vol_change_1h:.0f}% vol)")

        # 3. Volume élevé = marché liquide
        if vol >= 50000:
            score += 2
            reasons.append("volume élevé")
        elif vol >= 10000:
            score += 1
            reasons.append("volume correct")

        # 4. Probabilité entre 30-70% = encore incertain
        if 30 <= prob <= 70:
            score += 2
            reasons.append("prob incertaine exploitable")

        # 5. Expiration proche = urgence
        if is_expiring_soon(m, 30):
            score += 1
            reasons.append("expire bientôt")

        # Seuil minimum pour considérer comme opportunité
        if score >= 4 and mid + "-opp" not in seen_signals:
            opportunities.append({
                "market": m,
                "score": score,
                "reasons": reasons,
                "prob_change_1h": prob_change_1h,
                "vol_change_1h": vol_change_1h,
            })

    # Trier par score puis volume
    opportunities.sort(key=lambda x: (x["score"], x["market"]["vol"]), reverse=True)
    return opportunities[:10]  # Top 10 opportunités

# ─── ANALYSE IA ──────────────────────────────────────────────────────────────

def analyze_opportunity(opp):
    """Claude analyse si c'est vraiment exploitable et donne une action claire"""
    if not ANTHROPIC_KEY:
        return None
    m = opp["market"]
    reasons_str = ", ".join(opp["reasons"])
    prob_change = opp["prob_change_1h"]
    vol_change = opp["vol_change_1h"]

    prompt = (
        "Tu es un trader expert en marchés de prédiction Polymarket.\n\n"
        f"MARCHE : {m['title']}\n"
        f"Probabilite actuelle : {m['prob']}%\n"
        f"Variation 24h : {'+' if m['delta']>0 else ''}{m['delta']}pts\n"
        f"Variation 1h : {'+' if prob_change>0 else ''}{prob_change:.0f}pts\n"
        f"Volume 24h : {fmt_vol(m['vol'])}\n"
        f"Signaux detectes : {reasons_str}\n\n"
        "Reponds UNIQUEMENT en JSON strict :\n"
        '{"exploitable": <true/false>, "action": "<ACHETER OUI/ACHETER NON/PASSER>", '
        '"conviction": "<FORTE/MOYENNE/FAIBLE>", '
        '"analyse": "<2 phrases max en francais expliquant pourquoi et comment profiter>", '
        '"risque": "<ce qui pourrait invalider ce trade en 1 phrase>"}'
        "\n\nexploitable=true seulement si tu vois une vraie opportunite avec edge clair."
    )

    try:
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": ANTHROPIC_KEY, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": "claude-haiku-4-5-20251001", "max_tokens": 250, "messages": [{"role": "user", "content": prompt}]},
            timeout=20
        )
        text = r.json()["content"][0]["text"].strip().replace("```json", "").replace("```", "").strip()
        return json.loads(text)
    except Exception as e:
        log(f"Claude error: {e}")
        return None

def build_opportunity_alert(opp, analysis):
    m = opp["market"]
    action = analysis.get("action", "")
    conviction = analysis.get("conviction", "")
    analyse = analysis.get("analyse", "")
    risque = analysis.get("risque", "")
    prob_change = opp["prob_change_1h"]
    vol_change = opp["vol_change_1h"]

    if action == "ACHETER OUI":
        icon = "🟢"
        action_label = "ACHETER OUI"
    elif action == "ACHETER NON":
        icon = "🔴"
        action_label = "ACHETER NON"
    else:
        icon = "⚪"
        action_label = "PASSER"

    conv_icon = "🔥" if conviction == "FORTE" else "🟡" if conviction == "MOYENNE" else "⚪"

    msg = (
        f"{icon} <b>OPPORTUNITE POLYMARKET</b>\n\n"
        f"📊 {m['title']}\n\n"
        f"Prob actuelle : <b>{m['prob']}%</b>\n"
        f"Variation 24h : {'+' if m['delta']>0 else ''}{m['delta']}pts\n"
    )
    if abs(prob_change) >= 3:
        msg += f"Variation 1h : {'+' if prob_change>0 else ''}{prob_change:.0f}pts\n"
    if vol_change >= 20:
        msg += f"Volume : +{vol_change:.0f}% en 1h\n"
    msg += f"Vol 24h : {fmt_vol(m['vol'])}\n\n"
    msg += f"{conv_icon} <b>{action_label}</b> — Conviction : {conviction}\n\n"
    msg += f"💡 {analyse}\n"
    if risque:
        msg += f"⚠️ Risque : {risque}\n"
    msg += f"\n🔗 {build_link(m)}"
    return msg

# ─── SOURCES NEWS ─────────────────────────────────────────────────────────────

NEWS_SOURCES = [
    {"name": "Reuters",         "url": "https://feeds.reuters.com/reuters/topNews"},
    {"name": "AP News",         "url": "https://rsshub.app/apnews/topics/apf-topnews"},
    {"name": "BBC World",       "url": "https://feeds.bbci.co.uk/news/world/rss.xml"},
    {"name": "Al Jazeera",      "url": "https://www.aljazeera.com/xml/rss/all.xml"},
    {"name": "Politico",        "url": "https://rss.politico.com/politics-news.xml"},
    {"name": "CoinDesk",        "url": "https://www.coindesk.com/arc/outboundfeeds/rss/"},
    {"name": "MarketWatch",     "url": "https://feeds.content.dowjones.io/public/rss/mw_topstories"},
    {"name": "Fed Reserve",     "url": "https://www.federalreserve.gov/feeds/press_all.xml"},
    {"name": "White House",     "url": "https://www.whitehouse.gov/feed/"},
    {"name": "r/Polymarket",    "url": "https://www.reddit.com/r/Polymarket/hot.json?limit=15", "type": "reddit"},
    {"name": "r/PredictionMarkets", "url": "https://www.reddit.com/r/PredictionMarkets/hot.json?limit=10", "type": "reddit"},
    {"name": "@disclosetv",     "url": "https://rsshub.app/telegram/channel/disclosetv"},
    {"name": "@sentdefender",   "url": "https://rsshub.app/telegram/channel/sentdefender"},
    {"name": "@BreakingNews",   "url": "https://rsshub.app/telegram/channel/BreakingNews"},
]

seen_news = set()

def fetch_rss(src):
    try:
        r = requests.get(
            "https://api.allorigins.win/get?url=" + requests.utils.quote(src["url"]),
            timeout=8
        )
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
            if d.get("stickied"):
                continue
            title = d.get("title", "")
            if title and len(title) > 10:
                news.append({
                    "title": title,
                    "link": "https://reddit.com" + d.get("permalink", ""),
                    "source": src["name"]
                })
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
    """Trouve les news liées à un marché"""
    from xml.etree import ElementTree
    stop = {"the","a","an","is","are","to","of","in","on","at","by","for","and","or","be","it","will","has","have","was","were","not","with","from","that","this","be","do","can","if","but"}
    mkt_words = set(market["title"].lower().replace("?","").replace(",","").split()) - stop
    mkt_words = {w for w in mkt_words if len(w) > 3}
    matched = []
    for n in news_list:
        news_words = set(n["title"].lower().replace("?","").replace(",","").split()) - stop
        common = mkt_words & news_words
        if len(common) >= 2:
            matched.append((n, len(common)))
    matched.sort(key=lambda x: x[1], reverse=True)
    return [n for n, _ in matched[:2]]


# ─── SYSTÈME DE RAPPORT ──────────────────────────────────────────────────────

last_report_time = time.time()

def track_signal(market, action):
    """Enregistre un signal envoyé pour le suivi"""
    signals_history.append({
        "id": market["id"],
        "title": market["title"],
        "action": action,
        "prob_entry": market["prob"],
        "prob_current": market["prob"],
        "sent_at": time.time(),
        "link": build_link(market),
        "end_date": market.get("endDate", ""),
        "result": None,
    })
    # Garder seulement les 50 derniers signaux
    if len(signals_history) > 50:
        signals_history.pop(0)

def update_signal_results(markets):
    """Met à jour les probabilités actuelles des signaux suivis"""
    market_probs = {m["id"]: m["prob"] for m in markets}
    for sig in signals_history:
        if sig["id"] in market_probs:
            sig["prob_current"] = market_probs[sig["id"]]
            # Déterminer le résultat
            prob_change = sig["prob_current"] - sig["prob_entry"]
            if sig["action"] == "ACHETER OUI":
                if prob_change >= 5:
                    sig["result"] = "WIN"
                elif prob_change <= -5:
                    sig["result"] = "LOSE"
            elif sig["action"] == "ACHETER NON":
                if prob_change <= -5:
                    sig["result"] = "WIN"
                elif prob_change >= 5:
                    sig["result"] = "LOSE"

def check_signal_results():
    """Envoie le résultat uniquement quand le marché est résolu (prob >= 95% ou <= 5%)"""
    now = time.time()
    for sig in signals_history:
        # Ignorer si déjà rapporté
        if sig.get("reported"):
            continue

        # Attendre au minimum 30 min
        if now - sig["sent_at"] < 1800:
            continue

        current_prob = sig["prob_current"]

        # Marché résolu = prob >= 95% / <= 5% OU date d expiration atteinte
        resolved_yes = current_prob >= 95
        resolved_no = current_prob <= 5

        # Vérifier si la date d expiration est passée
        expired = False
        end_date = sig.get("end_date", "")
        if end_date:
            try:
                end_dt = datetime.fromisoformat(end_date.replace("Z", "+00:00"))
                expired = datetime.now(timezone.utc) >= end_dt
            except:
                pass

        if not resolved_yes and not resolved_no and not expired:
            continue  # Pas encore résolu — on attend

        # Si expiré sans résolution claire, utiliser la prob actuelle
        if expired and not resolved_yes and not resolved_no:
            resolved_yes = current_prob >= 50
            resolved_no = current_prob < 50

        # Déterminer le résultat
        if resolved_yes:
            resolution = "OUI"
        else:
            resolution = "NON"

        if sig["action"] == "ACHETER OUI" and resolved_yes:
            result = "WIN"
        elif sig["action"] == "ACHETER NON" and resolved_no:
            result = "WIN"
        else:
            result = "LOSE"

        prob_change = current_prob - sig["prob_entry"]
        ds = f"+{prob_change:.0f}" if prob_change > 0 else f"{prob_change:.0f}"

        prob_change = sig["prob_current"] - sig["prob_entry"]
        ds = f"+{prob_change:.0f}" if prob_change > 0 else f"{prob_change:.0f}"

        # Déterminer le résultat
        if sig["action"] == "ACHETER OUI":
            if prob_change >= 5:
                result = "WIN"
                result_icon = "✅"
                result_label = "GAGNANT"
            elif prob_change <= -5:
                result = "LOSE"
                result_icon = "❌"
                result_label = "PERDANT"
            else:
                result = "NEUTRAL"
                result_icon = "➡️"
                result_label = "NEUTRE"
        elif sig["action"] == "ACHETER NON":
            if prob_change <= -5:
                result = "WIN"
                result_icon = "✅"
                result_label = "GAGNANT"
            elif prob_change >= 5:
                result = "LOSE"
                result_icon = "❌"
                result_label = "PERDANT"
            else:
                result = "NEUTRAL"
                result_icon = "➡️"
                result_label = "NEUTRE"
        else:
            result = "NEUTRAL"
            result_icon = "➡️"
            result_label = "NEUTRE"

        sig["result"] = result
        sig["reported"] = True

        sent_at_str = datetime.fromtimestamp(sig["sent_at"], tz=timezone.utc).strftime("%d/%m %H:%M")
        resolved_at_str = datetime.now(timezone.utc).strftime("%d/%m %H:%M")

        if sig["action"] == "ACHETER OUI":
            action_icon = "🟢"
        elif sig["action"] == "ACHETER NON":
            action_icon = "🔴"
        else:
            action_icon = "⚪"

        result_icon = "✅" if result == "WIN" else "❌"
        result_label = "GAGNANT" if result == "WIN" else "PERDANT"

        msg = (
            f"{result_icon} <b>MARCHÉ RÉSOLU — {result_label}</b>\n\n"
            f"📊 {sig['title']}\n\n"
            f"━━━━━━━━━━━━━\n"
            f"{action_icon} Action prise : <b>{sig['action']}</b>\n"
            f"Signal le : {sent_at_str} UTC\n"
            f"Prob au signal : {sig['prob_entry']}%\n"
            f"━━━━━━━━━━━━━\n"
            f"Résultat : <b>{resolution}</b>\n"
            f"Prob finale : {current_prob}% ({ds}pts)\n"
            f"Résolu le : {resolved_at_str} UTC\n"
            f"━━━━━━━━━━━━━\n\n"
        )

        if expired and not (current_prob >= 95 or current_prob <= 5):
            msg += "⏰ Marché clôturé à la date d expiration.\n"
        if result == "WIN":
            msg += "✅ Tu avais le bon côté — signal validé !\n"
        else:
            msg += "❌ Le marché a résolu dans l autre sens.\n"

        msg += f"\n🔗 {sig['link']}"

        send_report(msg)
        log(f"Résultat signal envoyé : {sig['title'][:50]} → {result_label}")
        time.sleep(2)

# ─── BOUCLE PRINCIPALE ───────────────────────────────────────────────────────

cycle = 0
all_markets = []

def run():
    global cycle, all_markets, seen_news

    cycle += 1
    log(f"=== Cycle #{cycle} ===")

    # Recharger tous les marchés toutes les 3 minutes
    log("Chargement des marchés...")
    all_markets = fetch_all_markets()
    if not all_markets:
        log("Aucun marché chargé")
        return

    # Mettre à jour l'historique des probabilités
    update_prob_history(all_markets)

    # Charger les news
    log("Chargement des news...")
    news = fetch_all_news()
    new_news = []
    for n in news:
        nid = uid(n["title"] + n["source"])
        if nid not in seen_news:
            seen_news.add(nid)
            new_news.append(n)

    log(f"{len(all_markets)} marchés, {len(new_news)} nouvelles news")

    # Mettre à jour les résultats des signaux passés
    update_signal_results(all_markets)

    # Vérifier les résultats des signaux après 24h
    check_signal_results()

    # Détecter les opportunités
    opportunities = detect_opportunities(all_markets)
    log(f"{len(opportunities)} opportunités détectées")

    sent = 0
    for opp in opportunities:
        m = opp["market"]
        mid = m["id"]

        # Trouver les news liées
        related_news = match_news_to_market(new_news + news[:50], m)

        # Analyser avec Claude
        log(f"Analyse : {m['title'][:60]}")
        analysis = analyze_opportunity(opp)

        if not analysis:
            continue

        if not analysis.get("exploitable", False):
            log(f"Non exploitable : {m['title'][:50]}")
            continue

        if analysis.get("action") == "PASSER":
            log(f"Action PASSER : {m['title'][:50]}")
            continue

        # Construire et envoyer l'alerte
        msg = build_opportunity_alert(opp, analysis)

        # Ajouter les news liées si disponibles
        if related_news:
            msg += "\n\n📰 <b>News liées :</b>"
            for n in related_news:
                msg += f"\n• {n['source']}: {n['title'][:60]}"
                if n.get("link"):
                    msg += f"\n  🔗 {n['link']}"

        # Enregistrer le signal silencieusement — résultat envoyé 24h après
        seen_signals.add(mid + "-opp")
        track_signal(m, analysis.get("action", ""))
        sent += 1
        log(f"Signal enregistré : {m['title'][:50]} — résultat dans 24h")
        time.sleep(1)

        if sent >= 3:
            break

    if sent == 0:
        log("Aucune opportunité exploitable ce cycle")

def main():
    log("=== Bot Polymarket Intelligence v4 démarré ===")
    if not TOKEN or not CHAT_ID:
        log("ERREUR : TOKEN ou CHAT_ID manquant")
        return

    send_telegram(
        "🟢 <b>Bot Polymarket Intelligence v4</b>\n\n"
        "✅ 500+ marchés scannés en continu\n"
        "✅ Détection opportunités en temps réel\n"
        "✅ Surges prob + volume simultanés\n"
        "✅ Analyse IA — action claire OUI/NON\n"
        "✅ News liées à chaque opportunité\n"
        f"✅ Scan toutes les {INTERVAL} min\n\n"
        "Tu recevras une alerte uniquement quand une vraie opportunité est détectée."
    )

    # Message de démarrage sur le bot rapport
    send_report(
        "📊 <b>Bot Rapport Polymarket démarré</b>\n\n"
        "Tu recevras ici le compte rendu de chaque signal dès que 5 signaux ont été collectés.\n\n"
        "Format du rapport :\n"
        "✅ Signaux gagnants\n"
        "❌ Signaux perdants\n"
        "⏳ Signaux en cours\n"
        "📈 Taux de réussite global"
    )

    # Premier chargement
    log("Chargement initial...")
    all_markets.extend(fetch_all_markets())
    update_prob_history(all_markets)
    log(f"{len(all_markets)} marchés chargés au démarrage")

    while True:
        run()
        log(f"Prochain scan dans {INTERVAL} min...")
        time.sleep(INTERVAL * 60)

if __name__ == "__main__":
    main()
