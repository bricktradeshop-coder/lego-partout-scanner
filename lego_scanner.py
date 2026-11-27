import os
import re
import json
import time
import html
import base64
import hashlib
import imaplib
import email
from email.header import decode_header
import requests
import xml.etree.ElementTree as ET

REBRICKABLE_KEY = os.environ["REBRICKABLE_KEY"]
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip().strip("\"'").strip()
if TELEGRAM_TOKEN.startswith("bot"):  # häufiger Fehler: "bot" vor dem Token mitkopiert
    TELEGRAM_TOKEN = TELEGRAM_TOKEN[3:]
TG_TOKEN_OK = bool(re.fullmatch(r"\d+:[A-Za-z0-9_-]+", TELEGRAM_TOKEN))
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip().strip("\"'").strip()
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")

# Diese beiden URLs bitte an deine echten Quellen anpassen (Env-Variablen oder hier direkt):
MYDEALZ_RSS_URL = (
    os.environ.get("MYDEALZ_RSS_URL", "").strip()
    or "https://www.mydealz.de/rss/gruppe/lego"
)  # leerer String (z.B. nicht gesetztes GitHub-Secret) -> Default
BRICKMERGE_TG_CHANNEL = os.environ.get("BRICKMERGE_TG_CHANNEL", "").strip().lstrip("@")

# ---- KI-Anbieter (für die Gebrauchtware-Bewertung) ----
# AI_PROVIDER: "gemini" (Default), "anthropic" oder "openai"
# AI_MODEL: optional, überschreibt das Standardmodell des Anbieters
AI_PROVIDER = os.environ.get("AI_PROVIDER", "").strip().lower() or "gemini"
AI_KEY_ENV = {
    "gemini": "GEMINI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}
AI_DEFAULT_MODEL = {
    "gemini": "gemini-3.8-flash",
    "anthropic": "claude-haiku-5-5",
    "openai": "gpt-4o-mini",  # bitte prüfen, ob für dich aktuell/verfügbar
}
AI_API_KEY = os.environ.get(AI_KEY_ENV.get(AI_PROVIDER, ""), "").strip()
AI_MODEL = os.environ.get("AI_MODEL", "").strip() or AI_DEFAULT_MODEL.get(AI_PROVIDER, "")

# Vorfilter: Mails ohne das Wort "lego" gar nicht erst an die KI schicken (spart Kosten).
# Abschalten mit USED_PREFILTER=0
USED_PREFILTER = os.environ.get("USED_PREFILTER", "1").strip() != "0"

# Bei jedem Lauf eine (stumme) Status-Nachricht senden. Abschalten mit NOTIFY_EVERY_RUN=0
NOTIFY_EVERY_RUN = os.environ.get("NOTIFY_EVERY_RUN", "1").strip() != "0"

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


STATS = {
    "mydealz": 0, "brickmerge": 0, "new": 0, "unresolved": 0, "rated": 0, "sent": 0,
    "used": "nicht geprüft", "mail_unread": 0, "mail_filtered": 0, "mail_rated": 0, "mail_sent": 0,
    "warnings": [],
}


def warn(text):
    print(text)
    STATS["warnings"].append(str(text)[:150])


_tg_down = False  # wird True, wenn Telegram dauerhaft nicht erreichbar ist (falscher Token, Netzfehler)


def _plain(msg):
    """HTML-Nachricht in reinen Text umwandeln (Links bleiben als URL erhalten)."""
    msg = re.sub(r'<a href="([^"]*)">(.*?)</a>', r"\2: \1", msg, flags=re.DOTALL)
    return html.unescape(re.sub(r"<[^>]+>", "", msg))


def send_telegram(msg, silent=False):
    """Sendet eine Nachricht. Gibt True zurück, wenn Telegram sie angenommen hat."""
    global _tg_down
    if _tg_down:
        return False
    problems = []
    if not TELEGRAM_TOKEN:
        problems.append("TELEGRAM_TOKEN ist LEER (Secret fehlt, falscher Name oder falscher Ort)")
    elif not TG_TOKEN_OK:
        problems.append(
            f"TELEGRAM_TOKEN hat ein ungültiges Format (Länge {len(TELEGRAM_TOKEN)}, "
            f"Doppelpunkt vorhanden: {'ja' if ':' in TELEGRAM_TOKEN else 'nein'}; "
            "erwartet wird Zahl:Buchstaben, z.B. 123456789:ABC-def)"
        )
    if not TELEGRAM_CHAT_ID:
        problems.append("TELEGRAM_CHAT_ID ist LEER (Secret fehlt, falscher Name oder falscher Ort)")
    if problems:
        for pr in problems:
            print("Telegram Fehler:", pr)
        _tg_down = True
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": msg[:4000],
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
                "disable_notification": silent,
            },
            timeout=15,
        )
        if r.status_code == 400:
            # Formatierungsfehler (z.B. kaputtes HTML aus KI-Text): als reinen Text erneut senden
            print("Telegram 400, sende als reinen Text:", r.text[:150])
            r = requests.post(
                url,
                json={"chat_id": TELEGRAM_CHAT_ID, "text": _plain(msg)[:4000], "disable_notification": silent},
                timeout=15,
            )
        if r.status_code != 200:
            print("Telegram Fehler:", r.status_code, r.text[:200])
            if r.status_code in (401, 403, 404):  # Token/Chat-ID falsch oder Bot blockiert
                _tg_down = True
            return False
        return True
    except Exception as e:
        print("Telegram Fehler:", e)
        _tg_down = True
        return False


def parse_price(s):
    """'1.299,00' -> 1299.0, '29,99' -> 29.99, '1.299' -> 1299.0, '29.99' -> 29.99"""
    s = s.strip()
    if "," in s:
        s = s.replace(".", "").replace(",", ".")
    elif re.fullmatch(r"\d{1,3}(\.\d{3})+", s):
        s = s.replace(".", "")
    return float(s)


PRICE_RE = re.compile(r"(\d[\d.]*(?:,\d{1,2})?)\s*(?:€|Euro\b|EUR\b)")
POV_RE = re.compile(r"(?:Part-Out-Value|POV)\s*:?\s*(?:ca\.?)?\s*(\d[\d.]*(?:,\d{1,2})?)\s*€")


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
            warn(f"Rebrickable Fehler: {e}")
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
            warn(f"mydealz: HTTP {r.status_code}")
            return deals
        root = ET.fromstring(r.content)
        for item in root.findall(".//item")[:40]:
            title_el = item.find("title")
            link_el = item.find("link")
            desc_el = item.find("description")
            title = (title_el.text or "") if title_el is not None else ""
            link = (link_el.text or "") if link_el is not None else ""
            desc = ""
            if desc_el is not None and desc_el.text:
                desc = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", html.unescape(desc_el.text))))
            price_m = PRICE_RE.search(title)
            if not price_m:
                continue
            try:
                price = parse_price(price_m.group(1))
            except ValueError:
                continue
            cands = explicit_set_numbers(desc)
            for c in set_candidates(title, price):
                if c not in cands:
                    cands.append(c)
            cands = cands[:3]
            pov = None
            pov_m = POV_RE.search(desc)  # Brickmerge-Daten im Deal-Text: "POV: ca. 102,83 €"
            if pov_m:
                try:
                    pov = parse_price(pov_m.group(1))
                except ValueError:
                    pov = None
            if cands:
                deals.append(
                    {
                        "source": "mydealz",
                        "title": title,
                        "cands": cands,
                        "price": price,
                        "bm_pov": pov,
                        "link": link,
                    }
                )
    except Exception as e:
        warn(f"mydealz Fehler: {e}")
    return deals


def explicit_set_numbers(text):
    """Setnummern aus eindeutigen Mustern: 'Artikel-Nr: 40806' oder 'brickmerge.de/40806-1'."""
    out = []
    for pat in (r"Artikel-Nr:\s*(\d{4,5})", r"brickmerge\.de/(\d{4,5})-1"):
        for m in re.finditer(pat, text):
            if m.group(1) not in out:
                out.append(m.group(1))
    return out


def fetch_brickmerge():
    deals = []
    if not BRICKMERGE_TG_CHANNEL:
        return deals
    try:
        page = None
        # t.me war im Juli 2026 zeitweise nicht erreichbar -> telegram.me als Ausweichadresse
        for host in ("t.me", "telegram.me"):
            try:
                r = requests.get(f"https://{host}/s/{BRICKMERGE_TG_CHANNEL}", headers=UA, timeout=15)
            except Exception as e:
                warn(f"brickmerge ({host}): {e}")
                continue
            if r.status_code == 200:
                page = r.text
                break
            warn(f"brickmerge ({host}): HTTP {r.status_code}")
        if page is None:
            return deals
        if "tgme_widget_message_text" not in page:
            warn("brickmerge: keine Beiträge gefunden (Kanalname falsch oder Web-Vorschau deaktiviert)")
            return deals

        messages = page.split("tgme_widget_message_text")[-10:]
        for msg in messages:
            msg = re.sub(r"^[^<>]*>", "", msg, count=1)  # Rest des öffnenden Tags entfernen
            raw = html.unescape(msg)
            text = html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]*?>", " ", msg)))
            price_m = re.search(r"(?:ab|für|Preis:)\s*(\d[\d.]*(?:,\d{1,2})?)\s*(?:€|Euro\b)", text)
            pov_m = POV_RE.search(text)
            link_m = re.search(
                r"(https://(?:brckmrg\.de|(?:www\.)?brickmerge\.de)/[a-zA-Z0-9\-_/]+)", raw
            )
            if not price_m:
                continue
            try:
                price = parse_price(price_m.group(1))
                pov = parse_price(pov_m.group(1)) if pov_m else None
            except ValueError:
                continue
            cands = explicit_set_numbers(text)
            for c in set_candidates(text, price):
                if c not in cands:
                    cands.append(c)
            cands = cands[:3]
            if not cands:
                continue
            deals.append(
                {
                    "source": "brickmerge",
                    "title": text[:100].strip(),
                    "cands": cands,
                    "price": price,
                    "bm_pov": pov,
                    "link": link_m.group(1) if link_m else f"https://www.brickmerge.de/{cands[0]}-1",
                }
            )
    except Exception as e:
        warn(f"brickmerge Fehler: {e}")
    return deals


def scan_new_sets(seen):
    md = fetch_mydealz()
    bm = fetch_brickmerge()
    deals = md + bm
    new_count = sum(1 for d in deals if f"NEW_{d['cands'][0]}_{d['price']}" not in seen)
    sent_count = 0
    STATS["mydealz"], STATS["brickmerge"], STATS["new"] = len(md), len(bm), new_count
    print(
        f"Neuware: mydealz {len(md)} Angebote mit Setnummer+Preis, "
        f"brickmerge {len(bm)}"
        + ("" if BRICKMERGE_TG_CHANNEL else " (Kanal nicht gesetzt, übersprungen)")
        + f"; davon neu: {new_count}"
    )

    for d in deals:
        try:
            deal_id = f"NEW_{d['cands'][0]}_{d['price']}"
            if deal_id in seen:
                continue

            set_num, set_info = resolve_set(d["cands"])
            if not set_info:
                STATS["unresolved"] += 1
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

            if est_pov <= 0 or d["price"] <= 0:
                seen.add(deal_id)
                continue

            STATS["rated"] += 1
            margin_ratio = d["price"] / est_pov
            pov_rate = est_pov / d["price"]
            is_good_pov = margin_ratio <= 0.40
            is_fig_goldmine = total_figs >= 3 and (d["price"] / total_figs) <= 4.50

            if not (is_good_pov or is_fig_goldmine):
                seen.add(deal_id)
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
            if send_telegram(msg):
                seen.add(deal_id)  # erst nach erfolgreichem Senden als gesehen markieren
                sent_count += 1
                STATS["sent"] += 1
        except Exception as e:
            warn(f"Fehler bei Deal {d.get('title', '?')[:40]}: {e}")

    print(f"Neuware: {sent_count} Nachricht(en) gesendet")


IMAGE_MIMES = {"image/jpeg", "image/png", "image/gif", "image/webp"}


def make_ai_client():
    """Erzeugt den Client des gewählten Anbieters (SDK wird erst hier importiert)."""
    if AI_PROVIDER == "gemini":
        from google import genai

        return genai.Client(api_key=AI_API_KEY)
    if AI_PROVIDER == "anthropic":
        import anthropic

        return anthropic.Anthropic(api_key=AI_API_KEY)
    if AI_PROVIDER == "openai":
        import openai

        return openai.OpenAI(api_key=AI_API_KEY)
    raise ValueError(f"Unbekannter AI_PROVIDER: {AI_PROVIDER!r} (erlaubt: gemini, anthropic, openai)")


def extract_json(text):
    """JSON aus der Antwort holen, auch wenn der Anbieter ```json-Zäune oder Text drumherum liefert."""
    text = (text or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except Exception:
        m = re.search(r"\{.*\}", text, re.DOTALL)
        if not m:
            return None
        try:
            data = json.loads(m.group(0))
        except Exception:
            return None
    if isinstance(data, list):
        data = data[0] if data else None
    return data if isinstance(data, dict) else None


def call_provider(client, prompt, image=None):
    """Ein einzelner KI-Aufruf. image = (bytes, mime) oder None. Gibt den Antworttext zurück."""
    if image and image[1] not in IMAGE_MIMES:
        image = None

    if AI_PROVIDER == "gemini":
        from google.genai import types

        contents = [prompt]
        if image:
            contents.append(types.Part.from_bytes(data=image[0], mime_type=image[1]))
        res = client.models.generate_content(
            model=AI_MODEL,
            contents=contents,
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        return res.text

    b64 = base64.b64encode(image[0]).decode("ascii") if image else None

    if AI_PROVIDER == "anthropic":
        content = []
        if image:
            content.append(
                {"type": "image", "source": {"type": "base64", "media_type": image[1], "data": b64}}
            )
        content.append({"type": "text", "text": prompt})
        res = client.messages.create(
            model=AI_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": content}],
        )
        return "".join(b.text for b in res.content if getattr(b, "type", "") == "text")

    if AI_PROVIDER == "openai":
        content = [{"type": "text", "text": prompt}]
        if image:
            content.append(
                {"type": "image_url", "image_url": {"url": f"data:{image[1]};base64,{b64}"}}
            )
        res = client.chat.completions.create(
            model=AI_MODEL,
            messages=[{"role": "user", "content": content}],
            response_format={"type": "json_object"},
        )
        return res.choices[0].message.content

    raise ValueError(f"Unbekannter AI_PROVIDER: {AI_PROVIDER!r}")


TEMP_ERRORS = ("503", "429", "529", "UNAVAILABLE", "RESOURCE_EXHAUSTED", "overloaded", "rate limit")


def ai_analyze(client, prompt, image=None, retries=3):
    """KI-Aufruf mit Wiederholung bei vorübergehenden Fehlern. Gibt ein dict oder None zurück."""
    for attempt in range(retries):
        try:
            return extract_json(call_provider(client, prompt, image))
        except Exception as e:
            temporary = any(t.lower() in str(e).lower() for t in TEMP_ERRORS)
            if not temporary or attempt == retries - 1:
                raise
            time.sleep(5 * (attempt + 1))  # 5 s, 10 s


def looks_like_lego(subject, text):
    return "lego" in f"{subject} {text}".lower()


# ==========================================
# MODUL 2: GEBRAUCHTWARE KI-SCANNER (Kleinanzeigen / eBay Alerts)
# ==========================================
def scan_used_lots(seen):
    missing = [
        name
        for name, val in (
            (AI_KEY_ENV.get(AI_PROVIDER, "KI-Key"), AI_API_KEY),
            ("GMAIL_USER", GMAIL_USER),
            ("GMAIL_APP_PASSWORD", GMAIL_APP_PASSWORD),
        )
        if not val
    ]
    if missing:
        print("Gebrauchtware übersprungen, fehlende Secrets:", ", ".join(missing))
        STATS["used"] = "übersprungen, Secrets fehlen: " + ", ".join(missing)
        return
    print(f"Gebrauchtware: KI-Anbieter {AI_PROVIDER}, Modell {AI_MODEL}")

    try:
        client = make_ai_client()
    except Exception as e:
        warn(f"KI-Client Fehler: {e}")
        return
    mail = None
    try:
        mail = imaplib.IMAP4_SSL("imap.gmail.com")
        mail.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        mail.select("inbox")

        status, messages = mail.search(None, "UNSEEN")
        if status != "OK" or not messages or not messages[0]:
            print("Gebrauchtware: keine ungelesenen Mails im Posteingang")
            STATS["used"] = "keine ungelesenen Mails"
            return

        email_ids = messages[0].split()[-10:]
        print(f"Gebrauchtware: {len(email_ids)} ungelesene Mail(s) werden geprüft")
        STATS["used"] = "ran"
        STATS["mail_unread"] = len(email_ids)

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

                if USED_PREFILTER and not looks_like_lego(subject, clean_text):
                    STATS["mail_filtered"] += 1
                    seen.add(unique_key)
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
                if _tg_down:
                    break  # Telegram nicht erreichbar: keine KI-Kosten verursachen, nächster Lauf versucht es erneut

                image = None
                if img_urls:
                    try:
                        img_res = requests.get(html.unescape(img_urls[0]), timeout=10)
                        if img_res.status_code == 200:
                            mime = img_res.headers.get("Content-Type", "image/jpeg").split(";")[0].strip()
                            image = (img_res.content, mime if mime.startswith("image/") else "image/jpeg")
                    except Exception:
                        pass

                result = ai_analyze(client, prompt, image)
                if result is None:
                    continue  # nicht als gesehen markieren, nächster Lauf versucht es erneut

                STATS["mail_rated"] += 1
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
                    if not send_telegram(telegram_msg):
                        continue  # Senden fehlgeschlagen: Mail bleibt ungelesen, nächster Lauf versucht es erneut
                    STATS["mail_sent"] += 1

                seen.add(unique_key)
                mail.store(e_id, "+FLAGS", "\\Seen")
            except Exception as e:
                warn(f"Fehler bei Mail {e_id!r}: {str(e)[:120]}")
    except Exception as e:
        warn(f"Gebrauchtware-Scanner Fehler: {e}")
    finally:
        if mail is not None:
            try:
                mail.logout()
            except Exception:
                pass


def send_heartbeat():
    """Stumme Status-Nachricht nach jedem Lauf (abschaltbar mit NOTIFY_EVERY_RUN=0)."""
    if not NOTIFY_EVERY_RUN:
        return
    try:
        from zoneinfo import ZoneInfo
        from datetime import datetime

        zeit = datetime.now(ZoneInfo("Europe/Berlin")).strftime("%d.%m. %H:%M")
    except Exception:
        from datetime import datetime, timezone

        zeit = datetime.now(timezone.utc).strftime("%d.%m. %H:%M UTC")

    st = STATS
    bm_txt = f"brickmerge {st['brickmerge']}" if BRICKMERGE_TG_CHANNEL else "brickmerge aus"
    lines = [
        f"🔎 <b>Scan beendet</b> · {zeit}",
        f"📦 Neuware: mydealz {st['mydealz']}, {bm_txt} · neu {st['new']} · "
        f"bewertet {st['rated']} · gemeldet {st['sent']}",
    ]
    if st["used"] == "ran":
        lines.append(
            f"♻️ Gebrauchtware: {st['mail_unread']} Mail(s), {st['mail_filtered']} vorgefiltert, "
            f"{st['mail_rated']} bewertet, {st['mail_sent']} gemeldet"
        )
    else:
        lines.append(f"♻️ Gebrauchtware: {esc(st['used'])}")
    if st["sent"] + st["mail_sent"] == 0:
        lines.append("Kein Deal in diesem Lauf.")
    if st["warnings"]:
        lines.append("⚠️ <b>Probleme:</b>")
        lines += [f"• {esc(w)}" for w in st["warnings"][:5]]
    send_telegram("\n".join(lines), silent=True)


if __name__ == "__main__":
    # Testnachricht (mit Ton) bei jedem manuellen Start ("Run workflow") oder mit SEND_TEST=1
    if os.environ.get("GITHUB_EVENT_NAME") == "workflow_dispatch" or os.environ.get("SEND_TEST") == "1":
        ok = send_telegram("✅ <b>LEGO-Scanner:</b> Testnachricht, Telegram funktioniert.")
        print("Testnachricht gesendet:", ok)
    seen = Seen(load_seen())
    for scan in (scan_new_sets, scan_used_lots):
        try:
            scan(seen)
        except Exception as e:
            warn(f"{scan.__name__} abgebrochen: {e}")
    save_seen(seen.items)
    send_heartbeat()
