import os
import time
import json
import math
import requests
from datetime import datetime

TOKEN = os.environ.get("TELEGRAM_TOKEN", "")
CHAT_ID = os.environ.get("CHAT_ID", "")
THRESHOLD = int(os.environ.get("THRESHOLD", "10"))
INTERVAL = int(os.environ.get("INTERVAL_MINUTES", "5"))

TG_URL = f"https://api.telegram.org/bot{TOKEN}"
POLYMARKET_URL = "https://gamma-api.polymarket.com/markets?limit=50&active=true&closed=false&order=volume&ascending=false"

alerted = set()
markets_cache = []

def log(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)

def guess_cat(title):
    t = (title or "").lower()
    if any(k in t for k in ["bitcoin","btc","eth","crypto","solana","token","defi","blockchain","nft"]):
        return "Crypto"
    if any(k in t for k in ["election","president","trump","biden","democrat","republican","vote","minister"]):
        return "Politique"
    if any(k in t for k in ["nba","nfl","cup","champion","league","football","tennis","sport","match","olympics"]):
        return "Sport"
    if any(k in t for k in ["iran","russia","ukraine","china","war","peace","nuclear","military","nato","conflict","missile","israel","gaza"]):
        return "Géopolitique"
    if any(k in t for k in ["fed","rate","inflation","gdp","recession","stock","economy","dollar","oil","gold","tariff"]):
        return "Économie"
    return "Autre"

def fmt_vol(n):
    if n >= 1_000_000:
        return f"${n/1_000_000:.1f}M"
    if n >= 1_000:
        return f"${round(n/1_000)}K"
    return f"${round(n)}"

def send_telegram(text):
    try:
        r = requests.post(f"{TG_URL}/sendMessage", json={
            "chat_id": CHAT_ID,
            "text": text,
            "parse_mode": "HTML"
        }, timeout=10)
        data = r.json()
        if not data.get("ok"):
            log(f"Telegram error: {data.get('description')}")
            return False
        return True
    except Exception as e:
        log(f"Telegram exception: {e}")
        return False

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
            if vol > 0 and len(title) > 5:
                result.append({
                    "id": m.get("id",""),
                    "title": title,
                    "prob": prob,
                    "vol": vol,
                    "delta": delta,
                    "cat": guess_cat(title),
                    "slug": m.get("slug") or m.get("id",""),
                    "endDate": m.get("endDateIso") or m.get("endDate","")
                })
        return result
    except Exception as e:
        log(f"Fetch markets error: {e}")
        return []

def build_signal_msg(m):
    icon = "🚨" if abs(m["delta"]) >= 20 else "⚠️"
    direction = "+" if m["delta"] > 0 else ""
    side = "OUI" if m["prob"] >= 50 else "NON"
    link = f"https://polymarket.com/event/{m['slug']}"
    return (
        f"{icon} <b>SIGNAL POLYMARKET</b>\n\n"
        f"📊 {m['title']}\n"
        f"Probabilité : {m['prob']}% ({side})\n"
        f"Variation : {direction}{m['delta']}pts / 24h\n"
        f"Volume 24h : {fmt_vol(m['vol'])}\n"
        f"Catégorie : {m['cat']}\n\n"
        f"💡 Mouvement fort détecté — vérifiez les news liées.\n\n"
        f"🔗 {link}"
    )

def build_digest_msg(markets):
    top = sorted(markets, key=lambda m: m["vol"], reverse=True)[:3]
    lines = []
    for i, m in enumerate(top, 1):
        d = m["delta"]
        ds = f"+{d}" if d > 0 else str(d)
        lines.append(f"{i}. {m['title'][:55]}\n   {m['prob']}% · {fmt_vol(m['vol'])} · {ds}pts")
    body = "\n\n".join(lines)
    return f"📋 <b>RÉSUMÉ POLYMARKET</b>\n\nTop 3 marchés par volume :\n\n{body}\n\n🔗 https://polymarket.com"

def run_check():
    global markets_cache
    log("Scan Polymarket en cours...")
    markets = fetch_markets()
    if not markets:
        log("Aucun marché récupéré")
        return
    markets_cache = markets
    log(f"{len(markets)} marchés chargés")
    sent = 0
    signals = [m for m in markets if abs(m["delta"]) >= THRESHOLD and m["id"]+"-sig" not in alerted]
    signals = sorted(signals, key=lambda m: abs(m["delta"]), reverse=True)[:3]
    for m in signals:
        msg = build_signal_msg(m)
        if send_telegram(msg):
            alerted.add(m["id"]+"-sig")
            sent += 1
            log(f"Alerte envoyée : {m['title'][:50]} ({'+' if m['delta']>0 else ''}{m['delta']}pts)")
    if sent == 0:
        log(f"Aucun signal au-dessus de {THRESHOLD}pts")
    return markets

def main():
    log("=== Bot Polymarket démarré ===")
    log(f"Seuil : {THRESHOLD}pts | Intervalle : {INTERVAL} min")

    if not TOKEN or not CHAT_ID:
        log("ERREUR : TELEGRAM_TOKEN ou CHAT_ID manquant")
        return

    send_telegram(
        f"🟢 <b>Bot Polymarket démarré</b>\n\n"
        f"Surveillance active 🎯\n"
        f"Seuil : {THRESHOLD}pts\n"
        f"Scan : toutes les {INTERVAL} min"
    )

    check_count = 0
    while True:
        markets = run_check()
        check_count += 1

        # Résumé toutes les 12 vérifications (1h si interval=5min)
        if check_count % 12 == 0 and markets:
            send_telegram(build_digest_msg(markets))
            log("Résumé horaire envoyé")

        log(f"Prochain scan dans {INTERVAL} min...")
        time.sleep(INTERVAL * 60)

if __name__ == "__main__":
    main()
