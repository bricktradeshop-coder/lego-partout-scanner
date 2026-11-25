import os
import re
import json
import imaplib
import email
from email.header import decode_header
import requests
import xml.etree.ElementTree as ET
from google import genai
from google.genai import types

REBRICKABLE_KEY = os.environ["REBRICKABLE_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
TELEGRAM_CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

HISTORY_FILE = "seen_deals.json"


def load_seen():
    if os.path.exists(HISTORY_FILE):
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return set(json.load(f))
    return set()


def save_seen(seen):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(list(seen), f, indent=2)


def send_telegram(msg):
    url = f"https://telegram.org{TELEGRAM_TOKEN}/sendMessage"
    requests.post(
        url,
        json={"chat_id": TELEGRAM_CHAT_ID, "text": msg, "parse_mode": "Markdown"},
        timeout=15,
    )


# ==========================================
# MODUL 1: NEUWARE SCANNER (mydealz + brickmerge)
# ==========================================
def scan_new_sets(seen):
    deals = []

    # 1. mydealz RSS-Feed abrufen
    try:
        r = requests.get(
            "https://mydealz.de",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        root = ET.fromstring(r.text)
        for item in root.findall(".//item")[:15]:
            title = item.find("title").text or ""
            link = item.find("link").text or ""
            set_m = re.search(r"\b(\d{4,5})\b", title)
            price_m = re.search(r"(\d+[.,]\d{2})\s*€", title) or re.search(
                r"(\d+)\s*€", title
            )
            if set_m and price_m:
                deals.append(
                    {
                        "source": "mydealz",
                        "title": title,
                        "set_num": f"{set_m.group(1)}-1",
                        "price": float(price_m.group(1).replace(",", ".")),
                        "bm_pov": None,
                        "link": link,
                    }
                )
    except Exception as e:
        print("mydealz Fehler:", e)

    # 2. brickmerge Telegram-Webfeed abrufen
    try:
        r = requests.get(
            "https://t.me",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        messages = r.text.split("tgme_widget_message_text")[-10:]
        for msg in messages:
            text = re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", msg))
            set_m = re.search(r"\b(\d{4,5})\b", text)
            price_m = re.search(r"(?:ab|für)\s+(\d+[.,]\d{2})\s*€", text)
            pov_m = re.search(r"Part-Out-Value:\s*ca\.\s*(\d+[.,]\d{2})\s*€", text)
            link_m = re.search(r"(https://brckmrg\.de/[a-zA-Z0-9\-_/]+)", text)
            if set_m and price_m:
                deals.append(
                    {
                        "source": "brickmerge",
                        "title": text[:100].strip(),
                        "set_num": f"{set_m.group(1)}-1",
                        "price": float(price_m.group(1).replace(",", ".")),
                        "bm_pov": float(pov_m.group(1).replace(",", "."))
                        if pov_m
                        else None,
                        "link": link_m.group(1)
                        if link_m
                        else f"https://brickmerge.de{set_m.group(1)}-1",
                    }
                )
    except Exception as e:
        print("brickmerge Fehler:", e)

    headers = {"Authorization": f"key {REBRICKABLE_KEY}"}
    bad_words = [
        "duplo",
        "dots",
        "art",
        "brickheadz",
        "keychain",
        "schlüsselanhänger",
        "buch",
        "book",
        "plush",
    ]

    for d in deals:
        deal_id = f"NEW_{d['set_num']}_{d['price']}"
        if deal_id in seen:
            continue
        seen.add(deal_id)

        res_set = requests.get(
            f"https://rebrickable.com{d['set_num']}/",
            headers=headers,
            timeout=15,
        )
        if res_set.status_code != 200:
            continue
        set_info = res_set.json()
        set_name = set_info.get("name", d["title"])
        num_parts = set_info.get("num_parts", 0)

        full_text = f"{set_name} {d['title']}".lower()
        if any(w in full_text for w in bad_words):
            continue

        res_figs = requests.get(
            f"https://rebrickable.com{d['set_num']}/minifigs/",
            headers=headers,
            timeout=15,
        )
        total_figs = 0
        fig_names = []
        if res_figs.status_code == 200:
            for f in res_figs.json().get("results", []):
                qty = f.get("quantity", 1)
                total_figs += qty
                fig_names.append(f"{qty}x {f.get('set_name')}")

        is_hot_theme = bool(
            re.search(
                r"star wars|harry potter|marvel|dc|speed champions|technic|ninjago|lord of the rings|castle|pirates",
                full_text,
            )
        )
        fig_bonus = total_figs * (5.50 if is_hot_theme else 4.00)
        est_pov = d["bm_pov"] if d["bm_pov"] else ((num_parts * 0.10) + fig_bonus)

        if est_pov <= 0:
            continue

        margin_ratio = d["price"] / est_pov
        pov_rate = est_pov / d["price"]
        is_good_pov = margin_ratio <= 0.40
        is_fig_goldmine = total_figs >= 3 and (d["price"] / total_figs) <= 4.50

        if is_good_pov or is_fig_goldmine:
            badge = (
                "💎 *NEUWARE: MEGA-DEAL*"
                if (is_good_pov and is_fig_goldmine)
                else (
                    "🥷 *NEUWARE: FIGUREN-GOLDGRUBE*"
                    if is_fig_goldmine
                    else "📦 *NEUWARE: TOP POV*"
                )
            )
            price_per_fig = f"{d['price']/total_figs:.2f} €" if total_figs > 0 else "k.A."
            figs_str = ", ".join(fig_names[:4]) if fig_names else "Keine"

            msg = (
                f"{badge}\n\n"
                f"📦 *Set:* {set_name} ({d['set_num']})\n"
                f"💶 *Preis:* {d['price']:.2f} € ({d['source']})\n"
                f"📈 *Part-Out-Value:* ~{est_pov:.2f} € (*{pov_rate:.2f}x*)\n"
                f"🥷 *Minifiguren:* {total_figs} Stück _(Ø {price_per_fig}/Figur)_\n"
                f"👥 *Enthalten:* {figs_str}\n"
                f"🧱 *Teile:* {num_parts} Stück\n\n"
                f"🔗 [Direkt zum Angebot]({d['link']})"
            )
            send_telegram(msg)


# ==========================================
# MODUL 2: GEBRAUCHTWARE KI-SCANNER (Kleinanzeigen / eBay Alerts)
# ==========================================
def scan_used_lots(seen):
    if not (GEMINI_API_KEY and GMAIL_USER and GMAIL_APP_PASSWORD):
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    try:
        mail = imaplib.IMAP4_SSL("://gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, "UNSEEN")
        if not messages or not messages[0]:
            mail.logout()
            return

        email_ids = messages[0].split()[-10:]

        for e_id in email_ids:
            res, msg_data = mail.fetch(e_id, "(RFC822)")
            if not msg_data or not msg_data[0]:
                continue

            msg = email.message_from_bytes(msg_data[0][1])

            raw_subject = msg.get("Subject", "Kein Betreff")
            subject, encoding = decode_header(raw_subject)[0]
            if isinstance(subject, bytes):
                subject = subject.decode(encoding or "utf-8", errors="ignore")

            html_body = ""
            if msg.is_multipart():
                for part in msg.walk():
                    if part.get_content_type() == "text/html":
                        payload = part.get_payload(decode=True)
                        if payload:
                            html_body = payload.decode(errors="ignore")
                            break
            else:
                payload = msg.get_payload(decode=True)
                if payload:
                    html_body = payload.decode(errors="ignore")

            ad_links = re.findall(
                r'https://www\.(?:kleinanzeigen|ebay)\.de/[^\s"\'<>]+', html_body
            )
            img_urls = re.findall(
                r'https://img\.kleinanzeigen\.de/api/v1/prod-ads/images/[^\s"\'<>]+|https://i\.ebayimg\.com/images/[^\s"\'<>]+',
                html_body,
            )

            clean_text = re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", html_body))[
                :2500
            ]
            unique_key = f"USED_{hash(subject + clean_text[:200])}"
            if unique_key in seen:
                continue
            seen.add(unique_key)

            contents = []
            prompt = (
                "Du bist ein erfahrener LEGO Part-Out-Ankäufer. Analysiere diese Suchauftrags-Mail von Kleinanzeigen/eBay.\n"
                "Prüfe:\n"
                "1. Ist es echtes LEGO (keine Fremdmarken wie Playmobil, Mega Construx, Duplo, Lepin)?\n"
                "2. Lohnt sich das Angebot preislich für den Einzelteil-Weiterverkauf (z.B. Konvolut unter 10-12 €/kg, viele Minifiguren, Star Wars, Technic, Burgen/Pirates, bedruckte Teile)?\n"
                "3. Falls ein Bild dabei ist: Siehst du wertvolle Minifiguren, seltene Farben oder eher nur kaputte/vergilbte Standardsteine?\n\n"
                "Antworte AUSSCHLIESSLICH im folgenden JSON-Format:\n"
                '{"is_good_deal": true/false, "title": "Kurzer Titel des Angebots", "price": "Preis aus Mail", '
                '"reason": "Warum es ein guter Deal ist (Figuren, Kilopreis, Themenwelt)", '
Verwende Code mit Vorsicht.
'"reply_message": "Kurze, freundliche Kaufnachricht zum Kopieren an den Verkäufer"}\n\n'
f"Betreff: {subject}\nInhalt: {clean_text}"
)
contents.append(prompt)
if img_urls:
try:
img_res = requests.get(img_urls[0], timeout=10)
if img_res.status_code == 200:
contents.append(
types.Part.from_bytes(
data=img_res.content, mime_type="image/jpeg"
)
)
except Exception:
pass
ai_res = client.models.generate_content(
model="gemini-2.5-flash",
contents=contents,
config=types.GenerateContentConfig(
response_mime_type="application/json"
),
)
result = json.loads(ai_res.text)
if result.get("is_good_deal"):
best_link = ad_links[0] if ad_links else "kleinanzeigen.de"
telegram_msg = (
f"♻️ GEBRAUCHTWARE: KI-SCHNÄPPCHEN! ♻️\n\n"
f"📦 Angebot: {result.get('title')}\n"
f"💶 Preis: {result.get('price')}\n"
f"🧠 KI-Bewertung: {result.get('reason')}\n\n"
f"💬 Direkt-Nachricht (zum Kopieren):\n{result.get('reply_message')}\n\n"
f"🔗 Inserat öffnen"
)
send_telegram(telegram_msg)
mail.logout()
except Exception as e:
print("Gebrauchtware-Scanner Fehler:", e)
if name == "main":
seen = load_seen()
scan_new_sets(seen)
scan_used_lots(seen)
save_seen(seen)

if __name__ == "__main__":
    analyze_and_alert()
