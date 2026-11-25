import os
import re
import json
import requests
import xml.etree.ElementTree as ET

REBRICKABLE_KEY = os.environ["REBRICKABLE_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
HISTORY_FILE = "seen_deals.json"

def load_seen():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r") as f:
            return set(json.load(f))
    return set()

def save_seen(seen):
    with open(HISTORY_FILE, "w") as f:
        json.dump(list(seen), f)

def send_telegram(msg):
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"})

def get_deals():
    deals = []
    # 1. mydealz RSS-Feed abrufen
    try:
        r = requests.get("https://mydealz.de", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        root = ET.fromstring(r.text)
        for item in root.findall(".//item")[:15]:
            title = item.find("title").text or ""
            link = item.find("link").text or ""
            set_m = re.search(r"\b(\d{4,5})\b", title)
            price_m = re.search(r"(\d+[.,]\d{2})\s*€", title) or re.search(r"(\d+)\s*€", title)
            if set_m and price_m:
                deals.append({
                    "source": "mydealz",
                    "title": title,
                    "set_num": f"{set_m.group(1)}-1",
                    "price": float(price_m.group(1).replace(",", ".")),
                    "bm_pov": None,
                    "link": link
                })
    except Exception as e:
        print("mydealz Fehler:", e)

    # 2. brickmerge Telegram-Webfeed abrufen
    try:
        r = requests.get("https://t.me", headers={"User-Agent": "Mozilla/5.0"}, timeout=15)
        messages = r.text.split("tgme_widget_message_text")[-10:]
        for msg in messages:
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", msg))
            set_m = re.search(r"\b(\d{4,5})\b", text)
            price_m = re.search(r"(?:ab|für)\s+(\d+[.,]\d{2})\s*€", text)
            pov_m = re.search(r"Part-Out-Value:\s*ca\.\s*(\d+[.,]\d{2})\s*€", text)
            link_m = re.search(r"(https://brckmrg\.de/[a-zA-Z0-9\-_/]+)", text)
            if set_m and price_m:
                deals.append({
                    "source": "brickmerge",
                    "title": text[:100].strip(),
                    "set_num": f"{set_m.group(1)}-1",
                    "price": float(price_m.group(1).replace(",", ".")),
                    "bm_pov": float(pov_m.group(1).replace(",", ".")) if pov_m else None,
                    "link": link_m.group(1) if link_m else f"https://brickmerge.de{set_m.group(1)}-1"
                })
    except Exception as e:
        print("brickmerge Fehler:", e)

    return deals

def analyze_and_alert():
    seen = load_seen()
    deals = get_deals()
    headers = {"Authorization": f"key {REBRICKABLE_KEY}"}
    bad_words = ["duplo", "dots", "art", "brickheadz", "keychain", "schlüsselanhänger", "buch", "book", "plush"]

    for d in deals:
        deal_id = f"{d['set_num']}_{d['price']}"
        if deal_id in seen:
            continue
        seen.add(deal_id)

        # Rebrickable Set-Infos abrufen
        res_set = requests.get(f"https://rebrickable.com{d['set_num']}/", headers=headers)
        if res_set.status_code != 200:
            continue
        set_info = res_set.json()
        set_name = set_info.get("name", d["title"])
        num_parts = set_info.get("num_parts", 0)

        # Ladenhüter-Filter
        full_text = f"{set_name} {d['title']}".lower()
        if any(w in full_text for w in bad_words):
            continue

        # Minifiguren abrufen
        res_figs = requests.get(f"https://rebrickable.com{d['set_num']}/minifigs/", headers=headers)
        total_figs = 0
        fig_names = []
        if res_figs.status_code == 200:
            for f in res_figs.json().get("results", []):
                qty = f.get("quantity", 1)
                total_figs += qty
                fig_names.append(f"{qty}x {f.get('set_name')}")

        # Themen-Bonus & POV-Berechnung
        is_hot_theme = bool(re.search(r"star wars|harry potter|marvel|dc|speed champions|technic|ninjago|lord of the rings|castle|pirates", full_text))
        fig_bonus = total_figs * (5.50 if is_hot_theme else 4.00)
        est_pov = d["bm_pov"] if d["bm_pov"] else ((num_parts * 0.10) + fig_bonus)

        if est_pov <= 0:
            continue

        margin_ratio = d["price"] / est_pov
        pov_rate = est_pov / d["price"]
        is_good_pov = margin_ratio <= 0.40
        is_fig_goldmine = (total_figs >= 3 and (d["price"] / total_figs) <= 4.50)

        if is_good_pov or is_fig_goldmine:
            badge = "💎 *MEGA-DEAL (Top POV + Figuren)*" if (is_good_pov and is_fig_goldmine) else ("🥷 *FIGUREN-GOLDGRUBE*" if is_fig_goldmine else "📦 *STARKER TEILE-POV*")
            price_per_fig = f"{d['price']/total_figs:.2f} €" if total_figs > 0 else "k.A."
            figs_str = ", ".join(fig_names[:4]) if fig_names else "Keine"

            msg = (
                f"{badge}\n\n"
                f"📦 *Set:* {set_name} ({d['set_num']})\n"
                f"💶 *Preis:* {d['price']:.2f} € ({d['source']})\n"
                f"📈 *Part-Out-Value:* ~{est_pov:.2f} € (*{pov_rate:.2f}x*)\n\n"
                f"🥷 *Minifiguren:* {total_figs} Stück _(Ø {price_per_fig}/Figur)_\n"
                f"👥 *Enthalten:* {figs_str}\n"
                f"🧱 *Teile:* {num_parts} Stück\n\n"
                f"🔗 [Direkt zum Angebot]({d['link']})"
            )
            send_telegram(msg)

    save_seen(seen)

if __name__ == "__main__":
    analyze_and_alert()
