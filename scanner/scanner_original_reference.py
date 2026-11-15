import csv
import getpass
import html
import json
import os
import time
import urllib.parse
import urllib.request
from datetime import datetime

API = "https://api.dexscreener.com"

CONFIG = "scanner_config.json"
ALERTED = "scanner_alerted.json"
TRACKING_FILE = "scanner_suivi.json"
HISTORY_FILE = "scanner_historique.csv"

INTERVAL = 60

MIN_LIQUIDITY = 5000
MIN_VOLUME = 10000
MIN_TXNS = 30
MIN_AGE_MINUTES = 30

MAX_CHANGE_1H = 80
MIN_CHANGE_1H = -15
MAX_CHANGE_5M = 20

CHECKPOINTS = {
    "5 min": 300,
    "15 min": 900,
    "1 heure": 3600,
    "24 heures": 86400
}

HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json"
}


def get_json(url):
    request = urllib.request.Request(url, headers=HEADERS)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def load_config():
    if os.path.exists(CONFIG):
        with open(CONFIG, "r", encoding="utf-8") as f:
            config = json.load(f)
        if config.get("token") and config.get("chat_id"):
            return config

    print("Configuration Telegram")
    token = getpass.getpass("Token du bot Telegram : ").strip()
    chat_id = input("Identifiant du chat Telegram : ").strip()

    config = {"token": token, "chat_id": chat_id}

    with open(CONFIG, "w", encoding="utf-8") as f:
        json.dump(config, f)

    return config


def send_telegram(config, message):
    url = "https://api.telegram.org/bot" + config["token"] + "/sendMessage"

    data = urllib.parse.urlencode({
        "chat_id": config["chat_id"],
        "text": message,
        "parse_mode": "HTML",
        "disable_web_page_preview": "true"
    }).encode()

    request = urllib.request.Request(url, data=data)

    with urllib.request.urlopen(request, timeout=20) as response:
        result = json.loads(response.read().decode())

    if not result.get("ok"):
        raise RuntimeError("Erreur Telegram : message refusé")


def load_alerted():
    if os.path.exists(ALERTED):
        with open(ALERTED, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_alerted(alerted):
    with open(ALERTED, "w", encoding="utf-8") as f:
        json.dump(list(alerted), f)


def load_tracking():
    if os.path.exists(TRACKING_FILE):
        with open(TRACKING_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return []


def save_tracking(tracking):
    with open(TRACKING_FILE, "w", encoding="utf-8") as f:
        json.dump(tracking, f, indent=2)


def discover_tokens():
    sources = [
        "/token-profiles/latest/v1",
        "/token-boosts/latest/v1",
        "/token-boosts/top/v1",
        "/community-takeovers/latest/v1"
    ]

    tokens = {}

    for endpoint in sources:
        try:
            data = get_json(API + endpoint)

            if not isinstance(data, list):
                continue

            count = 0

            for item in data:
                chain = item.get("chainId")
                address = item.get("tokenAddress")

                if chain and address:
                    tokens[(chain, address)] = True
                    count += 1

            print(endpoint, ":", count, "tokens")

        except Exception as error:
            print("Source indisponible", endpoint, ":", error)

    return list(tokens.keys())


def get_pairs(tokens):
    grouped = {}

    for chain, address in tokens:
        grouped.setdefault(chain, []).append(address)

    pairs = []

    for chain, addresses in grouped.items():
        for start in range(0, len(addresses), 30):
            batch = addresses[start:start + 30]
            url = API + "/tokens/v1/" + chain + "/" + ",".join(batch)

            try:
                result = get_json(url)
                if isinstance(result, list):
                    pairs.extend(result)

            except Exception as error:
                print("Erreur API", chain, ":", error)

            time.sleep(0.3)

    return pairs


def get_metrics(pair):
    liquidity = float((pair.get("liquidity") or {}).get("usd") or 0)
    volume = float((pair.get("volume") or {}).get("h24") or 0)

    txns = (pair.get("txns") or {}).get("h1") or {}
    buys = int(txns.get("buys") or 0)
    sells = int(txns.get("sells") or 0)

    changes = pair.get("priceChange") or {}
    change_1h = float(changes.get("h1") or 0)
    change_5m = float(changes.get("m5") or 0)

    created_at = float(pair.get("pairCreatedAt") or 0)
    age = max(0, (time.time() * 1000 - created_at) / 60000) if created_at else 0

    total = buys + sells
    buy_ratio = buys / total * 100 if total else 0

    return {
        "liquidity": liquidity,
        "volume": volume,
        "buys": buys,
        "sells": sells,
        "total": total,
        "change_1h": change_1h,
        "change_5m": change_5m,
        "age": age,
        "buy_ratio": buy_ratio
    }


def analyze(pair):
    m = get_metrics(pair)

    if m["age"] < MIN_AGE_MINUTES:
        return False
    if m["liquidity"] < MIN_LIQUIDITY:
        return False
    if m["volume"] < MIN_VOLUME:
        return False
    if m["total"] < MIN_TXNS:
        return False
    if m["buys"] <= m["sells"]:
        return False
    if m["change_1h"] < MIN_CHANGE_1H:
        return False
    if m["change_1h"] > MAX_CHANGE_1H:
        return False
    if m["change_5m"] > MAX_CHANGE_5M:
        return False

    return True


def calculate_score(pair):
    m = get_metrics(pair)

    liquidity_score = min(m["liquidity"] / 20000, 1) * 25
    volume_score = min(m["volume"] / 100000, 1) * 25
    activity_score = min(m["total"] / 100, 1) * 20
    balance_score = m["buy_ratio"] / 100 * 15
    stability_score = max(0, 15 - abs(m["change_1h"]) / 8)

    total = (
        liquidity_score
        + volume_score
        + activity_score
        + balance_score
        + stability_score
    )

    return round(max(0, min(100, total)))


def analyze_simulation(pair):
    m = get_metrics(pair)
    score = calculate_score(pair)

    positives = []
    risks = []

    if m["liquidity"] >= 20000:
        positives.append("Liquidité supérieure à 20 000 $")
    elif m["liquidity"] >= 10000:
        positives.append("Liquidité correcte")

    if m["volume"] >= 100000:
        positives.append("Volume 24h important")
    elif m["volume"] >= 20000:
        positives.append("Volume 24h intéressant")

    if m["total"] >= 100:
        positives.append("Activité élevée sur 1h")
    elif m["total"] >= 50:
        positives.append("Activité présente sur 1h")

    if m["buy_ratio"] >= 60:
        positives.append("Achats nettement majoritaires")
    elif m["buy_ratio"] >= 55:
        positives.append("Achats légèrement majoritaires")

    if m["change_1h"] > 40:
        risks.append("Hausse déjà très rapide sur 1h")
    if m["change_1h"] < 0:
        risks.append("Tendance négative sur 1h")
    if m["change_5m"] > 8:
        risks.append("Accélération rapide sur 5 min")
    if m["change_5m"] < -5:
        risks.append("Recul marqué sur 5 min")
    if m["liquidity"] < 10000:
        risks.append("Liquidité faible")
    if m["volume"] < 20000:
        risks.append("Volume limité")
    if m["buy_ratio"] < 55:
        risks.append("Achats insuffisamment dominants")
    if m["age"] < 60:
        risks.append("Paire encore récente")

    reject = (
        m["liquidity"] < 7500
        or m["volume"] < 10000
        or m["total"] < 30
        or m["buy_ratio"] < 50
        or m["change_1h"] < -15
        or m["change_1h"] > 60
        or m["change_5m"] < -10
        or m["change_5m"] > 15
    )

    candidate = (
        score >= 70
        and m["liquidity"] >= 20000
        and m["volume"] >= 100000
        and m["total"] >= 100
        and m["buy_ratio"] >= 58
        and -5 <= m["change_1h"] <= 25
        and -3 <= m["change_5m"] <= 8
        and m["age"] >= 60
    )

    if reject:
        decision = "ÉCARTER DU SUIVI"
        reason = "Un ou plusieurs seuils de sécurité ne sont pas réunis."
    elif candidate:
        decision = "CANDIDAT POUR SIMULATION"
        reason = "Les principaux critères du scénario simulé sont réunis."
    elif risks:
        decision = "ATTENDRE UNE CONFIRMATION"
        reason = "Des indicateurs intéressants existent, mais certains risques restent présents."
    else:
        decision = "OBSERVER"
        reason = "Les données méritent un suivi, sans confirmation suffisante."

    entry_conditions = (
        "Attendre 2 contrôles consécutifs avec achats supérieurs à 55 %, "
        "variation 5 min entre 0 et +8 %, et liquidité supérieure à 20 000 $. "
        "Aucune entrée réelle n'est effectuée."
    )

    invalidation = (
        "Abandonner le scénario simulé si la liquidité passe sous 7 500 $, "
        "si les ventes dominent durablement ou si la variation 5 min passe sous -5 %."
    )

    return {
        "decision": decision,
        "reason": reason,
        "positives": positives,
        "risks": risks,
        "entry": entry_conditions,
        "invalidation": invalidation,
        "score": score
    }


def make_message(pair):
    token = pair.get("baseToken") or {}
    m = get_metrics(pair)
    analysis = analyze_simulation(pair)

    name = html.escape(str(token.get("name") or "Inconnu"))
    symbol = html.escape(str(token.get("symbol") or "?"))
    chain = html.escape(str(pair.get("chainId") or "?"))

    address = token.get("address") or ""
    price = pair.get("priceUsd") or "?"
    url = pair.get("url") or (
        "https://dexscreener.com/" + str(pair.get("chainId") or "")
        + "/" + address
    )

    positives = "\n".join(
        "  + " + html.escape(x) for x in analysis["positives"]
    ) or "  Aucun point positif majeur identifié"

    risks = "\n".join(
        "  - " + html.escape(x) for x in analysis["risks"]
    ) or "  Aucun risque particulier détecté par les seuils"

    return (
        "🔎 <b>ANALYSE DU SCANNER</b>\n\n"
        "<b>" + name + " (" + symbol + ")</b>\n"
        "Décision : <b>" + analysis["decision"] + "</b>\n"
        "Score indicatif : <b>" + str(analysis["score"]) + "/100</b>\n\n"
        "<b>POURQUOI ?</b>\n"
        + html.escape(analysis["reason"]) + "\n\n"
        "<b>POINTS POSITIFS</b>\n" + positives + "\n\n"
        "<b>POINTS DE VIGILANCE</b>\n" + risks + "\n\n"
        "<b>CONDITIONS DU SCÉNARIO SIMULÉ</b>\n"
        + html.escape(analysis["entry"]) + "\n\n"
        "<b>INVALIDATION</b>\n"
        + html.escape(analysis["invalidation"]) + "\n\n"
        "<b>DONNÉES DU TOKEN</b>\n"
        "Réseau : " + chain + "\n"
        "Prix : $" + html.escape(str(price)) + "\n"
        "Liquidité : $" + format(m["liquidity"], ",.0f") + "\n"
        "Volume 24h : $" + format(m["volume"], ",.0f") + "\n"
        "Achats 1h : " + str(m["buys"]) + "\n"
        "Ventes 1h : " + str(m["sells"]) + "\n"
        "Part des achats : " + format(m["buy_ratio"], ".1f") + "%\n"
        "Variation 5 min : " + format(m["change_5m"], "+.2f") + "%\n"
        "Variation 1h : " + format(m["change_1h"], "+.2f") + "%\n\n"
        '<a href="' + html.escape(str(url), quote=True)
        + '">Voir sur DEX Screener</a>\n\n'
        "📊 Suivi automatique : 5 min / 15 min / 1h / 24h\n"
        "⚠️ Analyse indicative pour observation et simulation uniquement. "
        "Aucun achat ou vente n'est effectué."
    )


def add_history_row(item, label, current_price):
    initial_price = float(item["initial_price"])
    current_price = float(current_price)

    variation = (
        (current_price - initial_price) / initial_price
    ) * 100

    new_file = not os.path.exists(HISTORY_FILE)

    with open(HISTORY_FILE, "a", newline="", encoding="utf-8-sig") as f:
        writer = csv.writer(f)

        if new_file:
            writer.writerow([
                "Date detection", "Token", "Réseau", "Étape",
                "Prix initial", "Prix actuel", "Variation %"
            ])

        writer.writerow([
            datetime.fromtimestamp(item["detected_at"]).strftime(
                "%Y-%m-%d %H:%M:%S"
            ),
            item["symbol"],
            item["chain"],
            label,
            initial_price,
            current_price,
            round(variation, 2)
        ])

    return variation


def track_token(pair, tracking):
    token = pair.get("baseToken") or {}

    address = token.get("address")
    chain = pair.get("chainId")
    price = pair.get("priceUsd")

    if not address or not chain or not price:
        return

    try:
        price = float(price)
    except (ValueError, TypeError):
        return

    if price <= 0:
        return

    identifier = chain + ":" + address.lower()

    if any(item["id"] == identifier for item in tracking):
        return

    item = {
        "id": identifier,
        "name": token.get("name", "?"),
        "symbol": token.get("symbol", "?"),
        "chain": chain,
        "address": address,
        "initial_price": price,
        "detected_at": time.time(),
        "score": calculate_score(pair),
        "checks": []
    }

    tracking.append(item)
    add_history_row(item, "Détection", price)

    print("Suivi créé :", item["symbol"])


def update_tracking(tracking, config):
    changed = False

    for item in tracking:
        elapsed = time.time() - item["detected_at"]

        for label, seconds in CHECKPOINTS.items():
            if label in item["checks"] or elapsed < seconds:
                continue

            url = (
                API + "/tokens/v1/"
                + item["chain"] + "/" + item["address"]
            )

            try:
                pairs = get_json(url)

                if not isinstance(pairs, list):
                    continue

                matching = [
                    p for p in pairs
                    if (p.get("baseToken") or {}).get(
                        "address", ""
                    ).lower() == item["address"].lower()
                ]

                matching.sort(
                    key=lambda p: float(
                        (p.get("liquidity") or {}).get("usd") or 0
                    ),
                    reverse=True
                )

                if not matching:
                    continue

                current_price = matching[0].get("priceUsd")
                if not current_price:
                    continue

                current_price = float(current_price)
                if current_price <= 0:
                    continue

                variation = add_history_row(
                    item, label, current_price
                )

                item["checks"].append(label)
                changed = True

                message = (
                    "📊 <b>POINT DE SUIVI : "
                    + html.escape(label) + "</b>\n\n"
                    "<b>" + html.escape(item["symbol"]) + "</b>\n"
                    "Prix initial : $" + str(item["initial_price"]) + "\n"
                    "Prix actuel : $" + str(current_price) + "\n"
                    "Variation depuis détection : <b>"
                    + format(variation, "+.2f") + "%</b>\n\n"
                    "Suivi statistique uniquement. "
                    "Aucune transaction effectuée."
                )

                send_telegram(config, message)

                print(
                    "Suivi", item["symbol"], label,
                    ":", round(variation, 2), "%"
                )

            except Exception as error:
                print("Erreur suivi", item["symbol"], ":", error)

            time.sleep(0.2)

    if changed:
        save_tracking(tracking)


def main():
    config = load_config()
    alerted = load_alerted()
    tracking = load_tracking()

    print("\nScanner DEX Screener démarré.")
    print("Contrôle toutes les", INTERVAL, "secondes.")
    print("Analyse : observation et simulation uniquement.")
    print("Historique :", HISTORY_FILE)
    print("Arrêt : Ctrl + C\n")

    while True:
        try:
            update_tracking(tracking, config)

            tokens = discover_tokens()
            print("Tokens repérés :", len(tokens))

            pairs = get_pairs(tokens)
            print("Paires analysées :", len(pairs))

            new_alerts = 0

            for pair in pairs:
                token = pair.get("baseToken") or {}
                address = token.get("address")
                chain = pair.get("chainId")

                if not address or not chain:
                    continue

                identifier = chain + ":" + address.lower()

                if identifier in alerted:
                    continue

                if not analyze(pair):
                    continue

                try:
                    send_telegram(config, make_message(pair))

                    alerted.add(identifier)
                    save_alerted(alerted)

                    track_token(pair, tracking)
                    save_tracking(tracking)

                    new_alerts += 1

                    print(
                        "Analyse envoyée :",
                        token.get("symbol"),
                        "| Décision :",
                        analyze_simulation(pair)["decision"]
                    )

                except Exception as error:
                    print("Erreur alerte :", error)

            print("Nouvelles alertes :", new_alerts)
            print("Prochain scan dans", INTERVAL, "secondes.\n")

        except KeyboardInterrupt:
            print("\nScanner arrêté.")
            break

        except Exception as error:
            print("Erreur générale :", error)

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
