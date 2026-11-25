import os
import re
import json
import time
import html
import hashlib
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

# Diese beiden URLs bitte an deine echten Quellen anpassen (Env-Variablen oder hier direkt):
MYDEALZ_RSS_URL = (
    os.environ.get("MYDEALZ_RSS_URL", "").strip()
    or "https://www.mydealz.de/rss/gruppe/lego"
)  # leerer String (z.B. nicht gesetztes GitHub-Secret) -> Default
BRICKMERGE_TG_CHANNEL = os.environ.get("BRICKMERGE_TG_CHANNEL", "").strip().lstrip("@")

# Gemini-Modell: gemini-2.5-flash ist für neue Nutzer nicht mehr verfügbar.
# Bei Bedarf per Env-Variable GEMINI_MODEL überschreiben (leer -> Default).
GEMINI_MODEL = os.environ.get("GEMINI_MODEL", "").strip() or "gemini-3.8-flash"

HISTORY_FILE = "seen_deals.json"
MAX_HISTORY = 3000
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 Chrome/124.0 Safari/537.36"}
RB_BASE = "https://rebrickable.com/api/v3/lego"


# ==========================================
# Hilfsfunktionen
# ==========================================
def load_seen():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return list(dict.fromkeys(json.load(f)))
        except Exception:
            return []
    return []


def save_seen(seen_list):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(seen_list[-MAX_HISTORY:], f, indent=2)


class Seen:
    """Set-ähnlich, behält aber die Reihenfolge, damit alte Einträge abgeschnitten werden können."""

    def __init__(self, items):
        self.items = list(items)
        self._set = set(self.items)

    def __contains__(self, x):
        return x in self._set

    def add(self, x):
        if x not in self._set:
            self._set.add(x)
            self.items.append(x)


def esc(s):
    return html.escape(str(s), quote=False)


def send_telegram(msg):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
            timeout=15,
        )
        if r.status_code != 200:
            print("Telegram Fehler:", r.status_code, r.text[:200])
            return False
        return True
    except Exception as e:
        print("Telegram Fehler:", e)
        return False


def parse_price(s):
    """'1.299,00' -> 1299.0, '29,99' -> 29.99, '1.299' -> 1299.0, '29.99' -> 29.99"""
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


PRICE_RE = re.compile(r"(\d[\d.]*(?:,\d{1,2})?)\s*€")


def set_candidates(text, price=None):
    """Alle 4-5-stelligen Zahlen, die plausibel Setnummern sind (keine Jahreszahlen, kein Preis)."""
    out = []
    for m in re.finditer(r"(?<![\d.,])(\d{4,5})(?![\d]|[.,]\d)", text):
        n = m.group(1)
        if 1990 <= int(n) <= 2035 and len(n) == 4:
            continue
        if price is not None and int(n) == int(price):
            continue
        if n not in out:
            out.append(n)
    return out[:3]


def rb_get(path, retries=3):
    headers = {"Authorization": f"key {REBRICKABLE_KEY}"}
    for _ in range(retries):
        try:
            r = requests.get(f"{RB_BASE}{path}", headers=headers, timeout=15)
        except Exception as e:
            print("Rebrickable Fehler:", e)
            return None
        time.sleep(1.1)  # Rate-Limit ~1 Request/Sekunde
        if r.status_code == 429:
            time.sleep(5)
            continue
        if r.status_code == 200:
            try:
                return r.json()
            except Exception:
                return None
        return None
    return None


def resolve_set(candidates):
    for c in candidates:
        info = rb_get(f"/sets/{c}-1/")
        if info:
            return f"{c}-1", info
    return None, None


BAD_RE = re.compile(
    r"\b(duplo|dots|art|brickheadz|keychain|schl(ü|ue)sselanh(ä|ae)nger|buch|book|plush)\b",
    re.IGNORECASE,
)
HOT_RE = re.compile(
    r"\b(star wars|harry potter|marvel|dc|speed champions|technic|ninjago|lord of the rings|castle|pirates)\b",
    re.IGNORECASE,
)


# ==========================================
# MODUL 1: NEUWARE SCANNER (mydealz + brickmerge)
# ==========================================
def fetch_mydealz():
    deals = []
    try:
        r = requests.get(MYDEALZ_RSS_URL, headers=UA, timeout=15)
        if r.status_code != 200:
            print("mydealz Status:", r.status_code)
            return deals
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:15]:
            title_el = item.find("title")
            link_el = item.find("link")
            title = (title_el.text or "") if title_el is not None else ""
            link = (link_el.text or "") if link_el is not None else ""
            price_m = PRICE_RE.search(title)
            if not price_m:
                continue
            try:
                price = parse_price(price_m.group(1))
            except ValueError:
                continue
            cands = set_candidates(title, price)
            if cands:
                deals.append(
                    {
                        "source": "mydealz",
                        "title": title,
                        "cands": cands,
                        "price": price,
                        "bm_pov": None,
                        "link": link,
                    }
                )
    except Exception as e:
        print("mydealz Fehler:", e)
    return deals


def fetch_brickmerge():
    deals = []
    if not BRICKMERGE_TG_CHANNEL:
        return deals
    try:
        r = requests.get(f"https://t.me/s/{BRICKMERGE_TG_CHANNEL}", headers=UA, timeout=15)
        if r.status_code != 200:
            print("brickmerge Status:", r.status_code)
            return deals
        messages = r.text.split("tgme_widget_message_text")[-10:]
        for msg in messages:
            text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", msg)))
            price_m = re.search(r"(?:ab|für)\s+(\d[\d.]*(?:,\d{1,2})?)\s*€", text)
            pov_m = re.search(r"Part-Out-Value:\s*ca\.\s*(\d[\d.]*(?:,\d{1,2})?)\s*€", text)
            link_m = re.search(r"(https://brckmrg\.de/[a-zA-Z0-9\-_/]+)", text)
            if not price_m:
                continue
            try:
                price = parse_price(price_m.group(1))
                pov = parse_price(pov_m.group(1)) if pov_m else None
            except ValueError:
                continue
            cands = set_candidates(text, price)
            if not cands:
                continue
            deals.append(
                {
                    "source": "brickmerge",
                    "title": text[:100].strip(),
                    "cands": cands,
                    "price": price,
                    "bm_pov": pov,
                    "link": link_m.group(1) if link_m else f"https://brickmerge.de/{cands[0]}-1",
                }
            )
    except Exception as e:
        print("brickmerge Fehler:", e)
    return deals


def scan_new_sets(seen):
    deals = fetch_mydealz() + fetch_brickmerge()

    for d in deals:
        try:
            deal_id = f"NEW_{d['cands'][0]}_{d['price']}"
            if deal_id in seen:
                continue

            set_num, set_info = resolve_set(d["cands"])
            if not set_info:
                continue  # nicht als gesehen markieren -> nächster Lauf versucht es erneut

            set_name = set_info.get("name", d["title"])
            num_parts = set_info.get("num_parts", 0) or 0

            full_text = f"{set_name} {d['title']}"
            if BAD_RE.search(full_text):
                seen.add(deal_id)
                continue

            figs = rb_get(f"/sets/{set_num}/minifigs/")
            total_figs = 0
            fig_names = []
            if figs:
                for f in figs.get("results", []):
                    qty = f.get("quantity", 1)
                    total_figs += qty
                    fig_names.append(f"{qty}x {f.get('set_name')}")

            is_hot_theme = bool(HOT_RE.search(full_text))
            fig_bonus = total_figs * (5.50 if is_hot_theme else 4.00)
            est_pov = d["bm_pov"] if d["bm_pov"] else ((num_parts * 0.10) + fig_bonus)

            seen.add(deal_id)
            if est_pov <= 0 or d["price"] <= 0:
                continue

            margin_ratio = d["price"] / est_pov
            pov_rate = est_pov / d["price"]
            is_good_pov = margin_ratio <= 0.40
            is_fig_goldmine = total_figs >= 3 and (d["price"] / total_figs) <= 4.50

            if not (is_good_pov or is_fig_goldmine):
                continue

            badge = (
                "💎 <b>NEUWARE: MEGA-DEAL</b>"
                if (is_good_pov and is_fig_goldmine)
                else (
                    "🥷 <b>NEUWARE: FIGUREN-GOLDGRUBE</b>"
                    if is_fig_goldmine
                    else "📦 <b>NEUWARE: TOP POV</b>"
                )
            )
            price_per_fig = f"{d['price'] / total_figs:.2f} €" if total_figs > 0 else "k.A."
            figs_str = ", ".join(fig_names[:4]) if fig_names else "Keine"

            msg = (
                f"{badge}\n\n"
                f"📦 <b>Set:</b> {esc(set_name)} ({esc(set_num)})\n"
                f"💶 <b>Preis:</b> {d['price']:.2f} € ({esc(d['source'])})\n"
                f"📈 <b>Part-Out-Value:</b> ~{est_pov:.2f} € (<b>{pov_rate:.2f}x</b>)\n"
                f"🥷 <b>Minifiguren:</b> {total_figs} Stück <i>(Ø {price_per_fig}/Figur)</i>\n"
                f"👥 <b>Enthalten:</b> {esc(figs_str)}\n"
                f"🧱 <b>Teile:</b> {num_parts} Stück\n\n"
                f'🔗 <a href="{html.escape(d["link"], quote=True)}">Direkt zum Angebot</a>'
            )
            send_telegram(msg)
        except Exception as e:
            print("Fehler bei Deal", d.get("title", "?")[:60], ":", e)


# ==========================================
# MODUL 2: GEBRAUCHTWARE KI-SCANNER (Kleinanzeigen / eBay Alerts)
# ==========================================
def scan_used_lots(seen):
    if not (GEMINI_API_KEY and GMAIL_USER and GMAIL_APP_PASSWORD):
        return

    client = genai.Client(api_key=GEMINI_API_KEY)
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages or not messages[0]:
            return

        email_ids = messages[0].split()[-10:]

        for e_id in email_ids:
            try:
                # PEEK: Mail bleibt ungelesen, bis sie erfolgreich verarbeitet wurde
                res, msg_data = mail.fetch(e_id, "(BODY.PEEK[])")
                if res != "OK" or not msg_data or not msg_data[0]:
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
                                html_body = payload.decode(
                                    part.get_content_charset() or "utf-8", errors="ignore"
                                )
                                break
                else:
                    payload = msg.get_payload(decode=True)
                    if payload:
                        html_body = payload.decode(
                            msg.get_content_charset() or "utf-8", errors="ignore"
                        )

                ad_links = re.findall(
                    r'https://www\.(?:kleinanzeigen|ebay)\.de/[^\s"\'<>]+', html_body
                )
                img_urls = re.findall(
                    r'https://img\.kleinanzeigen\.de/api/v1/prod-ads/images/[^\s"\'<>]+'
                    r'|https://i\.ebayimg\.com/images/[^\s"\'<>]+',
                    html_body,
                )

                clean_text = html.unescape(
                    re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", html_body))
                )[:2500]
                unique_key = "USED_" + hashlib.sha1(
                    (subject + clean_text[:200]).encode("utf-8", errors="ignore")
                ).hexdigest()
                if unique_key in seen:
                    mail.store(e_id, "+FLAGS", "\\Seen")
                    continue

                prompt = (
                    "Du bist ein erfahrener LEGO Part-Out-Ankäufer. Analysiere diese Suchauftrags-Mail von Kleinanzeigen/eBay.\n"
                    "Prüfe:\n"
                    "1. Ist es echtes LEGO (keine Fremdmarken wie Playmobil, Mega Construx, Duplo, Lepin)?\n"
                    "2. Lohnt sich das Angebot preislich für den Einzelteil-Weiterverkauf "
                    "(z.B. Konvolut unter 10-12 €/kg, viele Minifiguren, Star Wars, Technic, Burgen/Pirates, bedruckte Teile)?\n"
                    "3. Falls ein Bild dabei ist: Siehst du wertvolle Minifiguren, seltene Farben oder eher nur kaputte/vergilbte Standardsteine?\n\n"
                    "Antworte AUSSCHLIESSLICH im folgenden JSON-Format:\n"
                    '{"is_good_deal": true/false, "title": "Kurzer Titel des Angebots", "price": "Preis aus Mail", '
                    '"reason": "Warum es ein guter Deal ist (Figuren, Kilopreis, Themenwelt)", '
                    '"reply_message": "Kurze, freundliche Kaufnachricht zum Kopieren an den Verkäufer"}\n\n'
                    f"Betreff: {subject}\nInhalt: {clean_text}"
                )
                contents = [prompt]
                if img_urls:
                    try:
                        img_res = requests.get(html.unescape(img_urls[0]), timeout=10)
                        if img_res.status_code == 200:
                            mime = img_res.headers.get("Content-Type", "image/jpeg").split(";")[0]
                            if not mime.startswith("image/"):
                                mime = "image/jpeg"
                            contents.append(
                                types.Part.from_bytes(data=img_res.content, mime_type=mime)
                            )
                    except Exception:
                        pass

                ai_res = client.models.generate_content(
                    model=GEMINI_MODEL,
                    contents=contents,
                    config=types.GenerateContentConfig(
                        response_mime_type="application/json"
                    ),
                )
                if not ai_res.text:
                    continue  # nicht als gesehen markieren, nächster Lauf versucht es erneut
                result = json.loads(ai_res.text)
                if isinstance(result, list):
                    result = result[0] if result else {}

                seen.add(unique_key)
                mail.store(e_id, "+FLAGS", "\\Seen")

                if result.get("is_good_deal"):
                    best_link = html.unescape(ad_links[0]) if ad_links else "https://www.kleinanzeigen.de"
                    telegram_msg = (
                        "♻️ <b>GEBRAUCHTWARE: KI-SCHNÄPPCHEN!</b> ♻️\n\n"
                        f"📦 <b>Angebot:</b> {esc(result.get('title', ''))}\n"
                        f"💶 <b>Preis:</b> {esc(result.get('price', ''))}\n"
                        f"🧠 <b>KI-Bewertung:</b> {esc(result.get('reason', ''))}\n\n"
                        f"💬 <b>Direkt-Nachricht (zum Kopieren):</b>\n{esc(result.get('reply_message', ''))}\n\n"
                        f'🔗 <a href="{html.escape(best_link, quote=True)}">Inserat öffnen</a>'
                    )
                    send_telegram(telegram_msg)
            except Exception as e:
                print("Fehler bei Mail", e_id, ":", e)
    except Exception as e:
        print("Gebrauchtware-Scanner Fehler:", e)
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


if __name__ == "__main__":
    seen = Seen(load_seen())
    scan_new_sets(seen)
    scan_used_lots(seen)
    save_seen(seen.items)
