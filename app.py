import email
import imaplib
import os
import base64
import csv
import io
import json
import re
import secrets
import sqlite3
import threading
import time
from urllib.parse import urlencode
from datetime import datetime
from email.header import decode_header, make_header
from email.utils import parseaddr
from pathlib import Path

import requests
try:
    import stripe
except Exception:
    stripe = None
from flask import Flask, flash, redirect, render_template, request, session, url_for
from pypdf import PdfReader


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "subcheck.db"
ONE_TIME_PRICE_RUB = 199
DEFAULT_PRICE = 0.0
SYNC_INTERVAL_SECONDS = 15 * 60
GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GMAIL_API_BASE = "https://gmail.googleapis.com/gmail/v1"

KNOWN_SERVICES = [
    {"name": "YouTube Premium", "category": "Видео", "domains": ["youtube.com", "google.com"], "keywords": ["youtube premium", "youtube premium family", "youtube premium"]},
    {"name": "Netflix", "category": "Видео", "domains": ["netflix.com"], "keywords": ["netflix"]},
    {"name": "ivi", "category": "Видео", "domains": ["ivi.ru"], "keywords": ["ivi", "иви"]},
    {"name": "Okko", "category": "Видео", "domains": ["okko.tv"], "keywords": ["okko", "окко"]},
    {"name": "Wink", "category": "Видео", "domains": ["wink.ru", "rt.ru"], "keywords": ["wink", "винк", "ростелеком wink"]},
    {"name": "Kinopoisk", "category": "Видео", "domains": ["kinopoisk.ru", "yandex.ru", "plus.yandex.ru"], "keywords": ["кинопоиск", "kinopoisk", "yandex plus", "яндекс плюс", "подписка плюс"]},
    {"name": "KION", "category": "Видео", "domains": ["kion.ru", "mts.ru"], "keywords": ["kion"]},
    {"name": "Premier", "category": "Видео", "domains": ["premier.one"], "keywords": ["premier", "премьер"]},
    {"name": "Amediateka", "category": "Видео", "domains": ["amediateka.ru"], "keywords": ["amediateka", "амедиатека"]},
    {"name": "Start", "category": "Видео", "domains": ["start.ru"], "keywords": ["start ru", "start подписка"]},
    {"name": "More.tv", "category": "Видео", "domains": ["more.tv"], "keywords": ["more tv", "more.tv"]},
    {"name": "Spotify", "category": "Музыка", "domains": ["spotify.com"], "keywords": ["spotify", "premium plan"]},
    {"name": "Apple Music", "category": "Музыка", "domains": ["apple.com"], "keywords": ["apple music"]},
    {"name": "Yandex Music", "category": "Музыка", "domains": ["yandex.ru", "plus.yandex.ru"], "keywords": ["яндекс музыка", "yandex music"]},
    {"name": "VK Музыка", "category": "Музыка", "domains": ["vk.com", "vkontakte.ru"], "keywords": ["vk музыка", "vk music", "vkmusic"]},
    {"name": "Deezer", "category": "Музыка", "domains": ["deezer.com"], "keywords": ["deezer"]},
    {"name": "Apple iCloud", "category": "Облако", "domains": ["icloud.com", "apple.com"], "keywords": ["icloud", "apple storage", "icloud+"]},
    {"name": "Google One", "category": "Облако", "domains": ["google.com"], "keywords": ["google one", "google storage"]},
    {"name": "Dropbox", "category": "Облако", "domains": ["dropbox.com"], "keywords": ["dropbox"]},
    {"name": "OneDrive", "category": "Облако", "domains": ["microsoft.com"], "keywords": ["onedrive", "microsoft 365", "office 365"]},
    {"name": "Яндекс 360", "category": "Облако", "domains": ["yandex.ru"], "keywords": ["яндекс 360", "yandex 360", "диск плюс"]},
    {"name": "ChatGPT Plus", "category": "AI", "domains": ["openai.com"], "keywords": ["chatgpt", "chatgpt plus", "openai receipt"]},
    {"name": "Claude Pro", "category": "AI", "domains": ["anthropic.com"], "keywords": ["claude pro", "anthropic"]},
    {"name": "Midjourney", "category": "AI", "domains": ["midjourney.com"], "keywords": ["midjourney"]},
    {"name": "Notion", "category": "Продуктивность", "domains": ["notion.so"], "keywords": ["notion", "notion ai"]},
    {"name": "Todoist", "category": "Продуктивность", "domains": ["todoist.com"], "keywords": ["todoist"]},
    {"name": "Evernote", "category": "Продуктивность", "domains": ["evernote.com"], "keywords": ["evernote"]},
    {"name": "Canva Pro", "category": "Продуктивность", "domains": ["canva.com"], "keywords": ["canva pro", "canva"]},
    {"name": "Adobe", "category": "Продуктивность", "domains": ["adobe.com"], "keywords": ["adobe", "creative cloud"]},
    {"name": "Figma", "category": "Продуктивность", "domains": ["figma.com"], "keywords": ["figma"]},
    {"name": "Telegram Premium", "category": "Связь", "domains": ["telegram.org"], "keywords": ["telegram premium"]},
    {"name": "Discord Nitro", "category": "Связь", "domains": ["discord.com"], "keywords": ["discord nitro", "nitro"]},
    {"name": "Zoom Pro", "category": "Связь", "domains": ["zoom.us"], "keywords": ["zoom pro", "zoom subscription"]},
    {"name": "Megogo", "category": "Видео", "domains": ["megogo.net"], "keywords": ["megogo"]},
]

EXCLUDED_OPERATION_MARKERS = [
    "перевод",
    "сбп",
    "остаток",
    "пополн",
    "снятие",
    "наличн",
    "комисси",
    "прочие операции",
    "перевод с карты",
    "перевод на карту",
    "mobile bank",
    "balance",
]

app = Flask(__name__)
app.secret_key = os.getenv("APP_SECRET_KEY", "subcheck-dev-secret")
sync_thread_started = False


def db_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS payments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                payment_token TEXT NOT NULL UNIQUE,
                paid INTEGER NOT NULL DEFAULT 0,
                access_token TEXT,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS subscriptions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL,
                name TEXT NOT NULL,
                category TEXT NOT NULL,
                price REAL NOT NULL,
                currency TEXT NOT NULL,
                billing_period TEXT NOT NULL,
                card_last4 TEXT,
                source_method TEXT
            )
            """
        )
        try:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN card_last4 TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            conn.execute("ALTER TABLE subscriptions ADD COLUMN source_method TEXT")
        except sqlite3.OperationalError:
            pass
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS mailbox_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_email TEXT NOT NULL UNIQUE,
                imap_host TEXT NOT NULL,
                imap_port INTEGER NOT NULL,
                mailbox_login TEXT NOT NULL,
                app_password TEXT NOT NULL,
                auto_sync_enabled INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL,
                last_sync_at TEXT,
                last_sync_status TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS oauth_connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                account_email TEXT NOT NULL UNIQUE,
                provider TEXT NOT NULL,
                access_token TEXT NOT NULL,
                refresh_token TEXT,
                token_expiry TEXT,
                scope TEXT,
                updated_at TEXT NOT NULL,
                last_sync_at TEXT,
                last_sync_status TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS app_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS paid_checks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                check_token TEXT NOT NULL UNIQUE,
                method TEXT NOT NULL,
                contact_email TEXT,
                input_payload TEXT NOT NULL,
                result_payload TEXT NOT NULL,
                paid INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                paid_at TEXT
            )
            """
        )


def decode_mime_header(raw: str) -> str:
    if not raw:
        return ""
    try:
        return str(make_header(decode_header(raw)))
    except Exception:
        return raw


def normalize_amount(raw_amount: str) -> float:
    cleaned = raw_amount.replace(" ", "").replace(",", ".")
    if cleaned.count(".") > 1:
        parts = cleaned.split(".")
        cleaned = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(cleaned)
    except ValueError:
        return DEFAULT_PRICE


def extract_text_from_message(msg: email.message.Message) -> str:
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            disposition = str(part.get("Content-Disposition", "")).lower()
            if content_type != "text/plain" or "attachment" in disposition:
                continue
            payload = part.get_payload(decode=True)
            if not payload:
                continue
            charset = part.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)


def detect_price_and_currency(text: str):
    if not text:
        return DEFAULT_PRICE, "RUB"
    patterns = [
        (r"(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)\s?(?:руб(?:\.|ля|лей)?|rur|rub|₽)", "RUB"),
        (r"(?:rub|rur|₽)\s?(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)", "RUB"),
        (r"(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)\s?(?:usd|\$|доллар(?:а|ов)?)", "USD"),
        (r"(?:usd|\$)\s?(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)", "USD"),
        (r"(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)\s?(?:eur|€|евро)", "EUR"),
        (r"(?:eur|€)\s?(\d[\d\s]{0,10}(?:[.,]\d{1,2})?)", "EUR"),
    ]
    lowered = text.lower()
    for pattern, currency in patterns:
        match = re.search(pattern, lowered)
        if match:
            return normalize_amount(match.group(1)), currency
    return DEFAULT_PRICE, "RUB"


def detect_period(text: str) -> str:
    lowered = text.lower()
    monthly_markers = ["month", "месяц", "/mo", "/m", "ежемесяч", "каждый месяц"]
    yearly_markers = ["annual", "year", "год", "ежегод", "/year", "per year", "12 month"]
    for marker in monthly_markers:
        if marker in lowered:
            return "monthly"
    for marker in yearly_markers:
        if marker in lowered:
            return "yearly"
    return "monthly"


def classify_sender(sender_email: str, sender_name: str):
    sender_haystack = f"{sender_email} {sender_name}".lower()
    for service in KNOWN_SERVICES:
        if any(domain in sender_haystack for domain in service["domains"]):
            return service["name"], service["category"]
    return None


def detect_service(subject: str, sender_email: str, sender_name: str, body: str):
    sender_match = classify_sender(sender_email, sender_name)
    if sender_match:
        return sender_match

    haystack = f"{subject} {sender_email} {sender_name} {body}".lower()
    payment_markers = [
        "receipt", "invoice", "paid", "payment", "charge", "subscription",
        "квитан", "чек", "списани", "автоплатеж", "подписк", "оплат",
    ]
    if not any(marker in haystack for marker in payment_markers):
        return None

    for service in KNOWN_SERVICES:
        if any(keyword in haystack for keyword in service["keywords"]):
            return service["name"], service["category"]
    return None


def fetch_subscriptions_from_imap(imap_host: str, imap_port: int, mailbox_login: str, app_password: str, max_messages: int = 350):
    results = []
    seen_names = set()
    mail = imaplib.IMAP4_SSL(imap_host, imap_port)
    try:
        mail.login(mailbox_login, app_password)
        status, _ = mail.select("INBOX")
        if status != "OK":
            raise RuntimeError("Не удалось открыть INBOX.")

        status, data = mail.search(None, "ALL")
        if status != "OK" or not data or not data[0]:
            return results

        message_ids = data[0].split()
        message_ids = message_ids[-max_messages:]
        for msg_id in reversed(message_ids):
            fetch_status, msg_data = mail.fetch(msg_id, "(RFC822)")
            if fetch_status != "OK" or not msg_data:
                continue

            raw_email = None
            for item in msg_data:
                if isinstance(item, tuple):
                    raw_email = item[1]
                    break
            if not raw_email:
                continue

            msg = email.message_from_bytes(raw_email)
            subject = decode_mime_header(msg.get("Subject", ""))
            raw_from = decode_mime_header(msg.get("From", ""))
            sender_name, sender_email = parseaddr(raw_from)
            sender_name = decode_mime_header(sender_name)
            sender_email = (sender_email or "").lower()
            body = extract_text_from_message(msg)[:10000]

            service = detect_service(subject, sender_email, sender_name, body)
            if not service:
                continue

            name, category = service
            if name in seen_names:
                continue

            price, currency = detect_price_and_currency(f"{subject}\n{body}")
            period = detect_period(f"{subject}\n{body}")
            results.append((name, category, price, currency, period))
            seen_names.add(name)
    finally:
        try:
            mail.close()
        except Exception:
            pass
        mail.logout()

    return results


def normalize_service_name(value: str) -> str:
    return " ".join((value or "").strip().lower().split())


def compact_text(value: str) -> str:
    lowered = (value or "").lower()
    return re.sub(r"[^a-zа-я0-9]+", "", lowered)


def detect_known_service(text: str):
    haystack = (text or "").lower()
    haystack_compact = compact_text(haystack)
    for service in KNOWN_SERVICES:
        for keyword in service["keywords"]:
            if keyword in haystack:
                return service["name"], service["category"]
            if compact_text(keyword) and compact_text(keyword) in haystack_compact:
                return service["name"], service["category"]
        if any(domain in haystack for domain in service["domains"]):
            return service["name"], service["category"]
    return None


def is_excluded_operation_text(text: str) -> bool:
    lowered = (text or "").lower()
    return any(marker in lowered for marker in EXCLUDED_OPERATION_MARKERS)


def is_credit_amount_text(raw_value: str) -> bool:
    value = str(raw_value or "").strip()
    return value.startswith("+")


def extract_last4_candidates(text: str):
    return set(re.findall(r"(?:\*{2,}|\b)(\d{4})\b", text or ""))


def extract_last4_candidates_from_bytes(raw_bytes: bytes, filename: str):
    try:
        if filename.endswith(".pdf"):
            reader = PdfReader(io.BytesIO(raw_bytes))
            text = "\n".join((page.extract_text() or "") for page in reader.pages)
        else:
            text = raw_bytes.decode("utf-8-sig", errors="replace")
        return extract_last4_candidates(text)
    except Exception:
        return set()


def parse_bank_amount(raw_value: str) -> float:
    if raw_value is None:
        return 0.0
    text = str(raw_value).strip().replace(" ", "")
    text = text.replace(",", ".")
    text = re.sub(r"[^\d\.\-]", "", text)
    if text.count(".") > 1:
        parts = text.split(".")
        text = "".join(parts[:-1]) + "." + parts[-1]
    try:
        return float(text)
    except ValueError:
        return 0.0


def parse_bank_date(raw_value: str):
    if not raw_value:
        return None
    candidates = [
        "%Y-%m-%d",
        "%d.%m.%Y",
        "%d/%m/%Y",
        "%Y/%m/%d",
        "%d-%m-%Y",
    ]
    text = str(raw_value).strip().split(" ")[0]
    for fmt in candidates:
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def normalize_merchant_label(raw_value: str) -> str:
    text = (raw_value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def infer_category_from_merchant(merchant: str) -> str:
    known = detect_known_service(merchant)
    if known:
        return known[1]
    service = detect_service(merchant, "", "", merchant)
    if service:
        return service[1]
    return "Другое"


def infer_service_from_merchant(merchant: str) -> str:
    known = detect_known_service(merchant)
    if known:
        return known[0]
    service = detect_service(merchant, "", "", merchant)
    if service:
        return service[0]
    return normalize_merchant_label(merchant)[:80] or "Неизвестный сервис"


def detect_period_from_days(day_values) -> str:
    if not day_values or len(day_values) < 2:
        return "monthly"
    sorted_days = sorted(day_values)
    diffs = []
    for idx in range(1, len(sorted_days)):
        diffs.append((sorted_days[idx] - sorted_days[idx - 1]).days)
    if not diffs:
        return "monthly"
    avg = sum(diffs) / len(diffs)
    if 25 <= avg <= 35:
        return "monthly"
    if 330 <= avg <= 390:
        return "yearly"
    return "monthly"


def extract_subscriptions_from_csv(csv_text: str, card_last4: str):
    reader = csv.DictReader(io.StringIO(csv_text))
    records = []
    for row in reader:
        lowered_map = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        merchant = (
            lowered_map.get("merchant")
            or lowered_map.get("description")
            or lowered_map.get("details")
            or lowered_map.get("name")
            or lowered_map.get("операция")
            or lowered_map.get("описание")
            or lowered_map.get("контрагент")
            or ""
        )
        op_category = (
            lowered_map.get("category")
            or lowered_map.get("категория")
            or ""
        )
        amount_raw = (
            lowered_map.get("amount")
            or lowered_map.get("sum")
            or lowered_map.get("value")
            or lowered_map.get("сумма")
            or "0"
        )
        currency = (
            lowered_map.get("currency")
            or lowered_map.get("валюта")
            or "RUB"
        ).upper()
        date_raw = (
            lowered_map.get("date")
            or lowered_map.get("operation_date")
            or lowered_map.get("transaction_date")
            or lowered_map.get("дата")
            or ""
        )
        card_hint = (
            lowered_map.get("card_last4")
            or lowered_map.get("card")
            or lowered_map.get("карта")
            or ""
        )

        if card_last4 and card_hint:
            digits = re.sub(r"\D", "", card_hint)
            if digits and not digits.endswith(card_last4):
                continue

        operation_text = f"{op_category} {merchant}".strip()
        if is_excluded_operation_text(operation_text):
            continue
        if is_credit_amount_text(amount_raw):
            continue

        amount = parse_bank_amount(amount_raw)
        if amount <= 0:
            continue
        tx_date = parse_bank_date(date_raw)
        merchant = normalize_merchant_label(merchant)
        if not merchant:
            continue

        records.append(
            {
                "merchant": merchant,
                "amount": amount,
                "currency": currency,
                "date": tx_date,
            }
        )

    grouped = {}
    for rec in records:
        key = normalize_service_name(rec["merchant"])
        grouped.setdefault(
            key,
            {
                "merchant": rec["merchant"],
                "amounts": [],
                "dates": [],
                "currency": rec["currency"],
                "is_known_service": bool(detect_known_service(rec["merchant"])),
            },
        )
        grouped[key]["amounts"].append(rec["amount"])
        if rec["date"]:
            grouped[key]["dates"].append(rec["date"])

    result = []
    for _, group in grouped.items():
        if len(group["amounts"]) < 2 and not group["is_known_service"]:
            continue
        avg_amount = sum(group["amounts"]) / len(group["amounts"])
        period = "monthly" if group["is_known_service"] else detect_period_from_days(group["dates"])
        service_name = infer_service_from_merchant(group["merchant"])
        category = infer_category_from_merchant(group["merchant"])
        result.append((service_name, category, round(avg_amount, 2), group["currency"], period, card_last4))
    return result


def infer_amount_from_line(line: str) -> float:
    candidates = extract_debit_amount_candidates(line)
    if not candidates:
        return 0.0
    return candidates[0]


def extract_debit_amount_candidates(line: str):
    matches = list(
        re.finditer(
            r"([+-]?\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d{2})|[+-]?\d+[.,]\d{2})",
            line,
        )
    )
    candidates = []
    for m in matches:
        token = m.group(1).strip()
        if token.startswith("+"):
            continue
        # Skip date-like token (e.g. 30.05 from 30.05.2026)
        if re.fullmatch(r"\d{1,2}\.\d{2}", token):
            continue
        amount = parse_bank_amount(token)
        if amount <= 0:
            continue
        score = 2 if "," in token else 1
        candidates.append((score, amount))

    if not candidates:
        return []
    candidates.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [value for _, value in candidates]


def find_nearest_debit_amount(lines, idx: int):
    for shift in [0, 1, 2, 3, -1, -2]:
        j = idx + shift
        if j < 0 or j >= len(lines):
            continue
        values = extract_debit_amount_candidates(lines[j])
        if values:
            return values[0]
    return 0.0


def is_transaction_header_line(line: str) -> bool:
    return bool(re.match(r"^\d{2}\.\d{2}\.\d{4}\s+\d{2}:\d{2}", line.strip()))


def split_pdf_into_transaction_blocks(lines):
    # Locked profile for this bank PDF layout:
    # each operation starts with "dd.mm.yyyy hh:mm" line.
    blocks = []
    current = []
    for line in lines:
        if is_transaction_header_line(line):
            if current:
                blocks.append(current)
            current = [line]
        else:
            if current:
                current.append(line)
    if current:
        blocks.append(current)
    return blocks


def extract_operation_amount_from_block(block_lines):
    # Locked profile for this bank PDF layout:
    # first debit money value in block is operation amount.
    joined = " ".join(block_lines)
    matches = list(
        re.finditer(
            r"([+-]?\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d{2})|[+-]?\d+[.,]\d{2})",
            joined,
        )
    )
    candidates = []
    for m in matches:
        token = m.group(1).strip()
        if token.startswith("+"):
            continue
        if re.fullmatch(r"\d{1,2}\.\d{2}", token):
            continue
        value = parse_bank_amount(token)
        if value <= 0:
            continue
        candidates.append(value)
    if not candidates:
        return 0.0
    # In bank rows first debit amount is operation value,
    # later values are often balances.
    return candidates[0]


def find_known_services_in_lines(lines):
    found = []
    for idx, line in enumerate(lines):
        known = detect_known_service(line)
        if known:
            found.append((idx, known, line))
    return found


def build_fallback_block_around_line(lines, idx, radius=4):
    start = max(0, idx - radius)
    end = min(len(lines), idx + radius + 1)
    return lines[start:end]


def infer_date_from_line(line: str):
    match = re.search(r"(\d{2}[./-]\d{2}[./-]\d{4}|\d{4}[./-]\d{2}[./-]\d{2})", line)
    if not match:
        return None
    return parse_bank_date(match.group(1))


def infer_currency_from_line(line: str) -> str:
    _, currency = detect_price_and_currency(line)
    return currency or "RUB"


def extract_merchant_from_line(line: str) -> str:
    cleaned = re.sub(r"\d{2}[./-]\d{2}[./-]\d{4}|\d{4}[./-]\d{2}[./-]\d{2}", " ", line)
    cleaned = re.sub(r"(\d{1,3}(?:[ \u00A0]\d{3})*(?:[.,]\d{2})|\d+[.,]\d{2})", " ", cleaned)
    cleaned = re.sub(r"(руб|rur|rub|₽|usd|\$|eur|€)", " ", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\*?\d{4}", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" -:|")
    return normalize_merchant_label(cleaned)


def extract_subscriptions_from_pdf(pdf_bytes: bytes, card_last4: str):
    # Locked profile strategy:
    # 1) parse by transaction blocks
    # 2) fallback to local neighborhood around known service line
    # 3) keep known service even when amount is missing (0.00)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    full_text_parts = []
    for page in reader.pages:
        full_text_parts.append(page.extract_text() or "")
    full_text = "\n".join(full_text_parts)
    lines = [line.strip() for line in full_text.splitlines() if line.strip()]

    records = []
    blocks = split_pdf_into_transaction_blocks(lines)
    used_service_names = set()
    for block in blocks:
        block_text = " ".join(block)
        known = detect_known_service(block_text)
        if not known:
            continue
        if is_excluded_operation_text(block_text):
            continue
        if card_last4:
            block_last4 = extract_last4_candidates(block_text)
            if block_last4 and card_last4 not in block_last4:
                continue
        amount = extract_operation_amount_from_block(block)
        if amount <= 0:
            amount = DEFAULT_PRICE
        date_value = infer_date_from_line(block[0]) if block else None
        records.append(
            {
                "merchant": known[0],
                "amount": amount,
                "currency": infer_currency_from_line(block_text),
                "date": date_value,
            }
        )
        used_service_names.add(known[0].lower())

    # Fallback for PDFs where transaction blocks are split badly:
    # search known services line-by-line and build a local neighborhood block.
    for idx, known, _ in find_known_services_in_lines(lines):
        known_name = known[0].lower()
        if known_name in used_service_names:
            continue
        local_block = build_fallback_block_around_line(lines, idx, radius=5)
        local_text = " ".join(local_block)
        if is_excluded_operation_text(local_text):
            continue
        if card_last4:
            nearby_last4 = extract_last4_candidates(local_text)
            if nearby_last4 and card_last4 not in nearby_last4:
                continue
        amount = extract_operation_amount_from_block(local_block)
        if amount <= 0:
            amount = DEFAULT_PRICE
        date_value = None
        for line in local_block:
            date_value = infer_date_from_line(line)
            if date_value:
                break
        records.append(
            {
                "merchant": known[0],
                "amount": amount,
                "currency": infer_currency_from_line(local_text),
                "date": date_value,
            }
        )
        used_service_names.add(known_name)

    grouped = {}
    for rec in records:
        key = normalize_service_name(rec["merchant"])
        grouped.setdefault(
            key,
            {
                "merchant": rec["merchant"],
                "amounts": [],
                "dates": [],
                "currency": rec["currency"],
                "is_known_service": bool(detect_known_service(rec["merchant"])),
            },
        )
        grouped[key]["amounts"].append(rec["amount"])
        if rec["date"]:
            grouped[key]["dates"].append(rec["date"])

    result = []
    for _, group in grouped.items():
        if len(group["amounts"]) < 2 and not group["is_known_service"]:
            continue
        avg_amount = sum(group["amounts"]) / len(group["amounts"])
        period = "monthly" if group["is_known_service"] else detect_period_from_days(group["dates"])
        service_name = infer_service_from_merchant(group["merchant"])
        category = infer_category_from_merchant(group["merchant"])
        result.append((service_name, category, round(avg_amount, 2), group["currency"], period, card_last4))
    return result


def cleanup_noise_subscriptions(account_email: str, card_last4: str | None = None) -> int:
    with db_conn() as conn:
        if card_last4:
            rows = conn.execute(
                "SELECT id, name, category, card_last4 FROM subscriptions WHERE email = ? AND card_last4 = ?",
                (account_email, card_last4),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT id, name, category, card_last4 FROM subscriptions WHERE email = ?",
                (account_email,),
            ).fetchall()

        delete_ids = []
        for row in rows:
            name = row["name"] or ""
            if is_excluded_operation_text(name):
                delete_ids.append(row["id"])
                continue
            if row["category"] == "Другое" and re.search(r"^\d+\s+перевод", name.lower()):
                delete_ids.append(row["id"])
                continue
            if name.strip().lower() in {"остаток на", "прочие операции"}:
                delete_ids.append(row["id"])

        if delete_ids:
            conn.executemany("DELETE FROM subscriptions WHERE id = ?", [(i,) for i in delete_ids])
        return len(delete_ids)


def extract_gmail_text(payload: dict) -> str:
    parts_text = []

    def walk(part: dict):
        mime_type = part.get("mimeType", "")
        body = part.get("body", {}) or {}
        data = body.get("data", "")
        if data and mime_type in ("text/plain", "text/html"):
            try:
                padding = "=" * (-len(data) % 4)
                decoded = base64.urlsafe_b64decode(data + padding).decode("utf-8", errors="replace")
                if mime_type == "text/html":
                    decoded = re.sub(r"<[^>]+>", " ", decoded)
                parts_text.append(decoded)
            except Exception:
                pass
        for sub in part.get("parts", []) or []:
            walk(sub)

    walk(payload or {})
    return "\n".join(parts_text)


def get_google_oauth_config():
    client_id = os.getenv("GOOGLE_CLIENT_ID", "").strip() or get_setting("google_client_id")
    client_secret = os.getenv("GOOGLE_CLIENT_SECRET", "").strip() or get_setting("google_client_secret")
    return client_id, client_secret


def set_setting(key: str, value: str) -> None:
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO app_settings (key, value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = excluded.updated_at
            """,
            (key, value, datetime.utcnow().isoformat()),
        )


def get_setting(key: str) -> str:
    with db_conn() as conn:
        row = conn.execute("SELECT value FROM app_settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else ""


def get_preferred_provider(account_email: str) -> str:
    key = f"preferred_provider::{account_email.lower()}"
    value = get_setting(key).strip().lower()
    return value if value in ("gmail", "mailru") else "gmail"


def set_preferred_provider(account_email: str, provider: str) -> None:
    normalized = provider.strip().lower()
    if normalized not in ("gmail", "mailru"):
        return
    key = f"preferred_provider::{account_email.lower()}"
    set_setting(key, normalized)


def save_oauth_connection(
    account_email: str,
    provider: str,
    access_token: str,
    refresh_token: str,
    token_expiry: str,
    scope: str,
) -> None:
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM oauth_connections WHERE account_email = ? AND provider = ?",
            (account_email, provider),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE oauth_connections
                SET access_token = ?, refresh_token = ?, token_expiry = ?, scope = ?, updated_at = ?
                WHERE account_email = ? AND provider = ?
                """,
                (access_token, refresh_token, token_expiry, scope, now, account_email, provider),
            )
        else:
            conn.execute(
                """
                INSERT INTO oauth_connections (
                    account_email, provider, access_token, refresh_token, token_expiry, scope, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_email, provider, access_token, refresh_token, token_expiry, scope, now),
            )


def get_oauth_connection(account_email: str, provider: str):
    with db_conn() as conn:
        return conn.execute(
            """
            SELECT access_token, refresh_token, token_expiry, scope, updated_at, last_sync_at, last_sync_status
            FROM oauth_connections
            WHERE account_email = ? AND provider = ?
            """,
            (account_email, provider),
        ).fetchone()


def update_oauth_sync_status(account_email: str, provider: str, status: str):
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE oauth_connections
            SET last_sync_at = ?, last_sync_status = ?
            WHERE account_email = ? AND provider = ?
            """,
            (datetime.utcnow().isoformat(), status, account_email, provider),
        )


def refresh_google_access_token(refresh_token: str):
    client_id, client_secret = get_google_oauth_config()
    if not client_id or not client_secret:
        raise RuntimeError("Не настроены GOOGLE_CLIENT_ID/GOOGLE_CLIENT_SECRET.")

    response = requests.post(
        GOOGLE_TOKEN_URL,
        data={
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        },
        timeout=20,
    )
    if response.status_code != 200:
        raise RuntimeError("Не удалось обновить access token Google.")
    return response.json()


def ensure_google_access_token(account_email: str):
    row = get_oauth_connection(account_email, "google")
    if not row:
        raise RuntimeError("Gmail не подключен.")

    access_token = row["access_token"]
    refresh_token = row["refresh_token"] or ""
    token_expiry = row["token_expiry"] or ""
    if token_expiry:
        try:
            expiry = datetime.fromisoformat(token_expiry)
            if datetime.utcnow() >= expiry and refresh_token:
                token_data = refresh_google_access_token(refresh_token)
                access_token = token_data["access_token"]
                expires_in = int(token_data.get("expires_in", 3600))
                new_expiry = datetime.utcnow().timestamp() + expires_in - 60
                expiry_iso = datetime.utcfromtimestamp(new_expiry).isoformat()
                save_oauth_connection(
                    account_email=account_email,
                    provider="google",
                    access_token=access_token,
                    refresh_token=refresh_token,
                    token_expiry=expiry_iso,
                    scope=row["scope"] or "",
                )
        except ValueError:
            pass
    return access_token


def fetch_subscriptions_from_gmail_api(account_email: str, max_messages: int = 80):
    access_token = ensure_google_access_token(account_email)
    headers = {"Authorization": f"Bearer {access_token}"}
    query = "receipt OR invoice OR subscription OR подписка OR списание OR payment"
    list_response = requests.get(
        f"{GMAIL_API_BASE}/users/me/messages",
        params={"q": query, "maxResults": max_messages},
        headers=headers,
        timeout=20,
    )
    if list_response.status_code != 200:
        details = ""
        try:
            payload = list_response.json()
            details = payload.get("error", {}).get("message", "") or str(payload)
        except Exception:
            details = list_response.text[:240]
        raise RuntimeError(
            f"Не удалось получить список писем Gmail (HTTP {list_response.status_code}). {details}"
        )

    payload = list_response.json()
    messages = payload.get("messages", [])
    results = []
    seen_names = set()

    for item in messages:
        msg_id = item.get("id")
        if not msg_id:
            continue
        message_response = requests.get(
            f"{GMAIL_API_BASE}/users/me/messages/{msg_id}",
            params={"format": "full"},
            headers=headers,
            timeout=20,
        )
        if message_response.status_code != 200:
            continue
        msg = message_response.json()
        headers_map = {}
        for h in msg.get("payload", {}).get("headers", []):
            name = h.get("name", "")
            value = h.get("value", "")
            if name:
                headers_map[name.lower()] = value

        subject = headers_map.get("subject", "")
        raw_from = headers_map.get("from", "")
        sender_name, sender_email = parseaddr(raw_from)
        sender_email = (sender_email or "").lower()
        snippet = msg.get("snippet", "")
        body_text = extract_gmail_text(msg.get("payload", {}))
        text_blob = f"{subject}\n{snippet}\n{body_text}"

        service = detect_service(subject, sender_email, sender_name, text_blob)
        if not service:
            continue
        name, category = service
        if name in seen_names:
            continue

        price, currency = detect_price_and_currency(text_blob)
        period = detect_period(text_blob)
        results.append((name, category, price, currency, period))
        seen_names.add(name)
    return results


def save_mailbox_connection(account_email: str, imap_host: str, imap_port: int, mailbox_login: str, app_password: str, auto_sync_enabled: bool) -> None:
    now = datetime.utcnow().isoformat()
    with db_conn() as conn:
        existing = conn.execute(
            "SELECT id FROM mailbox_connections WHERE account_email = ?",
            (account_email,),
        ).fetchone()
        if existing:
            conn.execute(
                """
                UPDATE mailbox_connections
                SET imap_host = ?, imap_port = ?, mailbox_login = ?, app_password = ?,
                    auto_sync_enabled = ?, updated_at = ?
                WHERE account_email = ?
                """,
                (imap_host, imap_port, mailbox_login, app_password, 1 if auto_sync_enabled else 0, now, account_email),
            )
        else:
            conn.execute(
                """
                INSERT INTO mailbox_connections (
                    account_email, imap_host, imap_port, mailbox_login, app_password,
                    auto_sync_enabled, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (account_email, imap_host, imap_port, mailbox_login, app_password, 1 if auto_sync_enabled else 0, now),
            )


def get_mailbox_connection(account_email: str):
    with db_conn() as conn:
        return conn.execute(
            """
            SELECT imap_host, imap_port, mailbox_login, auto_sync_enabled, last_sync_at, last_sync_status
            FROM mailbox_connections
            WHERE account_email = ?
            """,
            (account_email,),
        ).fetchone()


def upsert_subscriptions(account_email: str, parsed, existing_rows, source_method: str | None = None) -> int:
    existing_keys = {
        (
            normalize_service_name(row["name"]),
            row["billing_period"] if "billing_period" in row.keys() else "monthly",
            row["source_method"] if "source_method" in row.keys() else (source_method or "manual"),
        )
        for row in existing_rows
    }
    inserted = 0
    with db_conn() as conn:
        for item in parsed:
            if len(item) >= 6:
                name, category, price, currency, period, card_last4 = item
            else:
                name, category, price, currency, period = item
                card_last4 = None
            method_value = source_method or "manual"
            key = (normalize_service_name(name), period, method_value)
            if key in existing_keys:
                continue
            existing = conn.execute(
                """
                SELECT id, price
                FROM subscriptions
                WHERE email = ? AND name = ? COLLATE NOCASE AND billing_period = ? AND source_method = ?
                LIMIT 1
                """,
                (account_email, name, period, method_value),
            ).fetchone()
            if existing:
                if float(existing["price"]) == 0 and float(price) > 0:
                    conn.execute(
                        """
                        UPDATE subscriptions
                        SET price = ?, currency = ?, category = ?, card_last4 = COALESCE(?, card_last4), source_method = ?
                        WHERE id = ?
                        """,
                        (price, currency, category, card_last4, method_value, existing["id"]),
                    )
                existing_keys.add(key)
                continue
            conn.execute(
                """
                INSERT INTO subscriptions (email, name, category, price, currency, billing_period, card_last4, source_method)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (account_email, name, category, price, currency, period, card_last4, method_value),
            )
            inserted += 1
            existing_keys.add(key)
    return inserted


def cleanup_duplicate_subscriptions(account_email: str, source_method: str | None = None) -> int:
    with db_conn() as conn:
        if source_method:
            rows = conn.execute(
                """
                SELECT id, name, billing_period, price
                FROM subscriptions
                WHERE email = ? AND COALESCE(source_method, 'manual') = ?
                ORDER BY id DESC
                """,
                (account_email, source_method),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT id, name, billing_period, price
                FROM subscriptions
                WHERE email = ?
                ORDER BY id DESC
                """,
                (account_email,),
            ).fetchall()

        keep_by_key = {}
        delete_ids = []
        for row in rows:
            key = (normalize_service_name(row["name"]), row["billing_period"])
            if key not in keep_by_key:
                keep_by_key[key] = row
                continue
            kept = keep_by_key[key]
            if float(kept["price"]) == 0 and float(row["price"]) > 0:
                delete_ids.append(kept["id"])
                keep_by_key[key] = row
            else:
                delete_ids.append(row["id"])

        if delete_ids:
            conn.executemany("DELETE FROM subscriptions WHERE id = ?", [(i,) for i in delete_ids])
        return len(delete_ids)


def run_sync_for_connection(row) -> None:
    account_email = row["account_email"]
    try:
        parsed = fetch_subscriptions_from_imap(
            imap_host=row["imap_host"],
            imap_port=int(row["imap_port"]),
            mailbox_login=row["mailbox_login"],
            app_password=row["app_password"],
        )
        with db_conn() as conn:
            existing_rows = conn.execute("SELECT name FROM subscriptions WHERE email = ?", (account_email,)).fetchall()
        inserted = upsert_subscriptions(account_email, parsed, existing_rows)
        status = f"ok: imported {inserted} new"
    except Exception as exc:
        status = f"error: {str(exc)[:220]}"

    with db_conn() as conn:
        conn.execute(
            """
            UPDATE mailbox_connections
            SET last_sync_at = ?, last_sync_status = ?
            WHERE account_email = ?
            """,
            (datetime.utcnow().isoformat(), status, account_email),
        )


def sync_worker_loop() -> None:
    while True:
        try:
            with db_conn() as conn:
                rows = conn.execute(
                    """
                    SELECT account_email, imap_host, imap_port, mailbox_login, app_password
                    FROM mailbox_connections
                    WHERE auto_sync_enabled = 1
                    """
                ).fetchall()
            for row in rows:
                run_sync_for_connection(row)
        except Exception:
            pass
        time.sleep(SYNC_INTERVAL_SECONDS)


def ensure_sync_worker_started() -> None:
    global sync_thread_started
    if sync_thread_started:
        return
    thread = threading.Thread(target=sync_worker_loop, daemon=True, name="subcheck-sync-worker")
    thread.start()
    sync_thread_started = True


def get_access_context(access_token: str):
    with db_conn() as conn:
        payment = conn.execute(
            """
            SELECT email
            FROM payments
            WHERE access_token = ? AND paid = 1
            """,
            (access_token,),
        ).fetchone()
        if not payment:
            return None, []

        subs = conn.execute(
            """
            SELECT id, name, category, price, currency, billing_period, card_last4, COALESCE(source_method, 'manual') AS source_method
            FROM subscriptions
            WHERE email = ?
            ORDER BY name
            """,
            (payment["email"],),
        ).fetchall()
        return payment["email"], subs


def get_method_subscriptions(access_token: str, method: str):
    email_value, subs = get_access_context(access_token)
    if not email_value:
        return None, []
    filtered = [row for row in subs if (row["source_method"] or "manual") == method]
    return email_value, filtered


def serialize_rows(rows):
    result = []
    for row in rows:
        result.append({key: row[key] for key in row.keys()})
    return result


def get_base_url() -> str:
    configured = os.getenv("BASE_URL", "").strip().rstrip("/")
    if configured:
        return configured
    return request.host_url.rstrip("/")


def is_stripe_configured() -> bool:
    return bool(
        stripe is not None
        and os.getenv("STRIPE_SECRET_KEY", "").strip()
        and os.getenv("STRIPE_PRICE_ID", "").strip()
    )


def mark_check_paid(check_token: str):
    with db_conn() as conn:
        conn.execute(
            """
            UPDATE paid_checks
            SET paid = 1, paid_at = COALESCE(paid_at, ?)
            WHERE check_token = ?
            """,
            (datetime.utcnow().isoformat(), check_token),
        )


@app.route("/", methods=["GET"])
def index():
    return redirect(url_for("check_method"))


@app.route("/check", methods=["GET"])
def check_method():
    return render_template("check_method.html", one_time_price=ONE_TIME_PRICE_RUB)


@app.route("/check/input", methods=["GET"])
def check_input():
    method = request.args.get("method", "").strip().lower()
    if method not in ("gmail", "mailru", "card"):
        flash("Выбери способ проверки.")
        return redirect(url_for("check_method"))
    return render_template(
        "check_input.html",
        method=method,
        one_time_price=ONE_TIME_PRICE_RUB,
        google_client_id_configured=bool(get_google_oauth_config()[0]),
    )


@app.route("/check/start-payment", methods=["POST"])
def check_start_payment():
    method = request.form.get("method", "").strip().lower()
    if method not in ("gmail", "mailru", "card"):
        flash("Некорректный способ проверки.")
        return redirect(url_for("check_method"))

    contact_email = request.form.get("contact_email", "").strip().lower()
    if not contact_email or "@" not in contact_email:
        flash("Укажи контактный email.")
        return redirect(url_for("check_input", method=method))

    try:
        if method == "card":
            card_last4 = re.sub(r"\D", "", request.form.get("card_last4", ""))[-4:]
            if len(card_last4) != 4:
                flash("Укажи последние 4 цифры карты.")
                return redirect(url_for("check_input", method=method))
            statement_file = request.files.get("statement_file")
            if not statement_file or not statement_file.filename:
                flash("Загрузи выписку CSV или PDF.")
                return redirect(url_for("check_input", method=method))
            raw_bytes = statement_file.read()
            filename = (statement_file.filename or "").lower()
            if filename.endswith(".pdf"):
                parsed = extract_subscriptions_from_pdf(raw_bytes, card_last4)
            else:
                csv_text = raw_bytes.decode("utf-8-sig", errors="replace")
                parsed = extract_subscriptions_from_csv(csv_text, card_last4)
            result_payload = [
                {
                    "name": item[0],
                    "category": item[1],
                    "price": float(item[2]),
                    "currency": item[3],
                    "billing_period": item[4],
                    "card_last4": item[5] if len(item) >= 6 else None,
                }
                for item in parsed
            ]
            input_payload = {"method": method, "card_last4": card_last4, "filename": statement_file.filename}
        elif method == "mailru":
            mailbox_login = request.form.get("mailbox_login", "").strip()
            app_password = request.form.get("app_password", "").strip()
            if not mailbox_login or not app_password:
                flash("Заполни логин Mail.ru и пароль приложения.")
                return redirect(url_for("check_input", method=method))
            parsed = fetch_subscriptions_from_imap(
                imap_host="imap.mail.ru",
                imap_port=993,
                mailbox_login=mailbox_login,
                app_password=app_password,
            )
            result_payload = [
                {
                    "name": item[0],
                    "category": item[1],
                    "price": float(item[2]),
                    "currency": item[3],
                    "billing_period": item[4],
                    "card_last4": None,
                }
                for item in parsed
            ]
            input_payload = {"method": method, "mailbox_login": mailbox_login}
        else:
            flash("Для Gmail используй быстрый вход через Google (подключим следующим шагом). Пока выбери Mail.ru или Карту.")
            return redirect(url_for("check_input", method=method))
    except Exception as exc:
        flash(f"Ошибка анализа: {exc}")
        return redirect(url_for("check_input", method=method))

    check_token = secrets.token_urlsafe(24)
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO paid_checks (check_token, method, contact_email, input_payload, result_payload, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                check_token,
                method,
                contact_email,
                json.dumps(input_payload, ensure_ascii=False),
                json.dumps(result_payload, ensure_ascii=False),
                datetime.utcnow().isoformat(),
            ),
        )
    return redirect(url_for("check_pay", check_token=check_token))


@app.route("/check/pay/<check_token>", methods=["GET"])
def check_pay(check_token: str):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT method, contact_email, paid FROM paid_checks WHERE check_token = ?",
            (check_token,),
        ).fetchone()
    if not row:
        flash("Сессия проверки не найдена.")
        return redirect(url_for("check_method"))
    return render_template(
        "check_pay.html",
        check_token=check_token,
        method=row["method"],
        contact_email=row["contact_email"],
        paid=bool(row["paid"]),
        one_time_price=ONE_TIME_PRICE_RUB,
    )


@app.route("/check/pay/<check_token>/confirm", methods=["POST"])
def check_confirm_payment(check_token: str):
    with db_conn() as conn:
        row = conn.execute(
            "SELECT paid FROM paid_checks WHERE check_token = ?",
            (check_token,),
        ).fetchone()
    if not row:
        flash("Сессия проверки не найдена.")
        return redirect(url_for("check_method"))

    if row["paid"]:
        return redirect(url_for("check_results", check_token=check_token))

    # Fallback for local/demo when Stripe is not configured.
    if not is_stripe_configured():
        mark_check_paid(check_token)
        return redirect(url_for("check_results", check_token=check_token))

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    price_id = os.getenv("STRIPE_PRICE_ID", "").strip()
    base_url = get_base_url()

    checkout_session = stripe.checkout.Session.create(
        mode="payment",
        line_items=[{"price": price_id, "quantity": 1}],
        success_url=f"{base_url}{url_for('check_pay_success', check_token=check_token)}?session_id={{CHECKOUT_SESSION_ID}}",
        cancel_url=f"{base_url}{url_for('check_pay', check_token=check_token)}",
        metadata={"check_token": check_token},
    )
    return redirect(checkout_session.url, code=303)


@app.route("/check/pay/<check_token>/success", methods=["GET"])
def check_pay_success(check_token: str):
    session_id = request.args.get("session_id", "").strip()
    if not session_id:
        flash("Не удалось подтвердить оплату: отсутствует session_id.")
        return redirect(url_for("check_pay", check_token=check_token))

    with db_conn() as conn:
        row = conn.execute(
            "SELECT paid FROM paid_checks WHERE check_token = ?",
            (check_token,),
        ).fetchone()
    if not row:
        flash("Сессия проверки не найдена.")
        return redirect(url_for("check_method"))

    if row["paid"]:
        return redirect(url_for("check_results", check_token=check_token))

    if not is_stripe_configured():
        mark_check_paid(check_token)
        return redirect(url_for("check_results", check_token=check_token))

    stripe.api_key = os.getenv("STRIPE_SECRET_KEY", "").strip()
    checkout_session = stripe.checkout.Session.retrieve(session_id)
    payment_status = (checkout_session.get("payment_status") or "").lower()
    metadata = checkout_session.get("metadata") or {}
    token_from_session = metadata.get("check_token", "")

    if payment_status == "paid" and token_from_session == check_token:
        mark_check_paid(check_token)
        return redirect(url_for("check_results", check_token=check_token))

    flash("Оплата еще не подтверждена. Попробуй обновить страницу через пару секунд.")
    return redirect(url_for("check_pay", check_token=check_token))


@app.route("/check/results/<check_token>", methods=["GET"])
def check_results(check_token: str):
    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT method, paid, result_payload
            FROM paid_checks
            WHERE check_token = ?
            """,
            (check_token,),
        ).fetchone()
    if not row:
        flash("Сессия проверки не найдена.")
        return redirect(url_for("check_method"))
    if not row["paid"]:
        flash("Сначала оплати проверку.")
        return redirect(url_for("check_pay", check_token=check_token))

    subs = json.loads(row["result_payload"] or "[]")
    monthly_total = sum(float(s["price"]) for s in subs if s.get("billing_period") == "monthly")
    yearly_total = sum(float(s["price"]) for s in subs if s.get("billing_period") == "yearly")
    return render_template(
        "check_results.html",
        method=row["method"],
        subs=subs,
        monthly_total=monthly_total,
        yearly_total=yearly_total,
    )


@app.route("/payments/stripe/webhook", methods=["POST"])
def stripe_webhook():
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "").strip()
    payload = request.data
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        if secret:
            event = stripe.Webhook.construct_event(payload, sig_header, secret)
        else:
            event = json.loads(payload.decode("utf-8"))
    except Exception:
        return "invalid payload", 400

    event_type = event.get("type", "")
    if event_type == "checkout.session.completed":
        obj = event.get("data", {}).get("object", {})
        check_token = (obj.get("metadata", {}) or {}).get("check_token", "")
        if check_token:
            mark_check_paid(check_token)

    return "ok", 200


@app.route("/start-payment", methods=["POST"])
def start_payment():
    email_value = request.form.get("email", "").strip().lower()
    if not email_value or "@" not in email_value:
        flash("Укажи корректный email.")
        return redirect(url_for("index"))

    payment_token = secrets.token_urlsafe(18)
    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO payments (email, payment_token, created_at)
            VALUES (?, ?, ?)
            """,
            (email_value, payment_token, datetime.utcnow().isoformat()),
        )
    return redirect(url_for("pay", payment_token=payment_token))


@app.route("/pay/<payment_token>", methods=["GET"])
def pay(payment_token: str):
    with db_conn() as conn:
        payment = conn.execute(
            """
            SELECT email, paid
            FROM payments
            WHERE payment_token = ?
            """,
            (payment_token,),
        ).fetchone()
    if not payment:
        flash("Сессия оплаты не найдена.")
        return redirect(url_for("index"))

    return render_template(
        "pay.html",
        payment_token=payment_token,
        email=payment["email"],
        paid=bool(payment["paid"]),
        one_time_price=ONE_TIME_PRICE_RUB,
    )


@app.route("/pay/<payment_token>/confirm", methods=["POST"])
def confirm_payment(payment_token: str):
    access_token = secrets.token_urlsafe(24)
    with db_conn() as conn:
        payment = conn.execute(
            "SELECT email, paid FROM payments WHERE payment_token = ?",
            (payment_token,),
        ).fetchone()
        if not payment:
            flash("Сессия оплаты не найдена.")
            return redirect(url_for("index"))
        if not payment["paid"]:
            conn.execute(
                """
                UPDATE payments
                SET paid = 1, paid_at = ?, access_token = ?
                WHERE payment_token = ?
                """,
                (datetime.utcnow().isoformat(), access_token, payment_token),
            )
        else:
            existing = conn.execute(
                "SELECT access_token FROM payments WHERE payment_token = ?",
                (payment_token,),
            ).fetchone()
            access_token = existing["access_token"]

    return redirect(url_for("analysis_method", access=access_token))


@app.route("/analysis/method", methods=["GET"])
def analysis_method():
    return redirect(url_for("check_method"))


@app.route("/analysis/input", methods=["GET"])
def analysis_input():
    method = request.args.get("method", "").strip().lower()
    if method not in ("gmail", "mailru", "card"):
        return redirect(url_for("check_method"))
    return redirect(url_for("check_input", method=method))


@app.route("/analysis/results", methods=["GET"])
def analysis_results():
    flash("Старый экран результатов отключен. Используй платный сценарий проверки.")
    return redirect(url_for("check_method"))


@app.route("/dashboard", methods=["GET"])
def dashboard_legacy_redirect():
    access_token = request.args.get("access", "").strip()
    return redirect(url_for("analysis_method", access=access_token))


@app.route("/dashboard/set-provider", methods=["POST"])
def set_provider():
    access_token = request.form.get("access_token", "").strip()
    provider = request.form.get("provider", "").strip()
    email_value, _ = get_access_context(access_token)
    if not email_value:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))
    set_preferred_provider(email_value, provider)
    return redirect(url_for("analysis_input", access=access_token, method=provider))


@app.route("/dashboard/add", methods=["POST"])
def add_subscription():
    access_token = request.form.get("access_token", "").strip()
    email_value, _ = get_access_context(access_token)
    if not email_value:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    name = request.form.get("name", "").strip()
    category = request.form.get("category", "").strip() or "Другое"
    currency = request.form.get("currency", "").strip().upper() or "RUB"
    billing_period = request.form.get("billing_period", "monthly")

    try:
        price = float(request.form.get("price", "0").strip())
    except ValueError:
        flash("Цена должна быть числом.")
        return redirect(url_for("analysis_results", access=access_token, method="card"))

    if not name or price <= 0:
        flash("Заполни название и цену больше нуля.")
        return redirect(url_for("analysis_results", access=access_token, method="card"))

    with db_conn() as conn:
        conn.execute(
            """
            INSERT INTO subscriptions (email, name, category, price, currency, billing_period, source_method)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (email_value, name, category, price, currency, billing_period, "card"),
        )
    flash("Подписка добавлена.")
    return redirect(url_for("analysis_results", access=access_token, method="card"))


@app.route("/dashboard/connect-mailru", methods=["POST"])
def connect_mailru():
    access_token = request.form.get("access_token", "").strip()
    email_account, existing_subs = get_access_context(access_token)
    if not email_account:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    mailbox_login = request.form.get("mailbox_login", "").strip()
    app_password = request.form.get("app_password", "").strip()
    auto_sync_enabled = request.form.get("auto_sync_enabled") == "on"

    if not mailbox_login or not app_password:
        flash("Заполни логин Mail.ru и пароль приложения.")
        return redirect(url_for("check_input", method="mailru"))

    try:
        parsed = fetch_subscriptions_from_imap(
            imap_host="imap.mail.ru",
            imap_port=993,
            mailbox_login=mailbox_login,
            app_password=app_password,
        )
    except imaplib.IMAP4.error:
        flash("Не удалось подключить Mail.ru. Проверь логин и пароль приложения.")
        return redirect(url_for("check_input", method="mailru"))
    except Exception as exc:
        flash(f"Ошибка подключения Mail.ru: {exc}")
        return redirect(url_for("check_input", method="mailru"))

    save_mailbox_connection(
        account_email=email_account,
        imap_host="imap.mail.ru",
        imap_port=993,
        mailbox_login=mailbox_login,
        app_password=app_password,
        auto_sync_enabled=auto_sync_enabled,
    )
    inserted = upsert_subscriptions(email_account, parsed, existing_subs, source_method="mailru")
    if inserted == 0:
        flash("Mail.ru подключен, но новых подписок не найдено.")
    else:
        flash(f"Mail.ru подключен. Найдено {inserted} новых подписок.")
    if auto_sync_enabled:
        flash("Авто-синхронизация Mail.ru включена.")
    return redirect(url_for("check_method"))


@app.route("/dashboard/connect-gmail", methods=["POST"])
def connect_gmail():
    access_token = request.form.get("access_token", "").strip()
    email_account, _ = get_access_context(access_token)
    if not email_account:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    client_id, _ = get_google_oauth_config()
    if not client_id:
        flash("OAuth для Gmail пока не настроен. Нужно добавить GOOGLE_CLIENT_ID и GOOGLE_CLIENT_SECRET.")
        return redirect(url_for("check_input", method="gmail"))

    state = secrets.token_urlsafe(24)
    session["gmail_oauth_state"] = state
    session["gmail_oauth_access_token"] = access_token
    redirect_uri = url_for("gmail_oauth_callback", _external=True)
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email https://www.googleapis.com/auth/gmail.readonly",
        "access_type": "offline",
        "prompt": "consent",
        "state": state,
    }
    return redirect(f"{GOOGLE_AUTH_URL}?{urlencode(params)}")


@app.route("/dashboard/setup-google-oauth", methods=["POST"])
def setup_google_oauth():
    access_token = request.form.get("access_token", "").strip()
    email_account, _ = get_access_context(access_token)
    if not email_account:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    client_id = request.form.get("google_client_id", "").strip()
    client_secret = request.form.get("google_client_secret", "").strip()
    if not client_id or not client_secret:
        flash("Нужно заполнить Client ID и Client Secret.")
        return redirect(url_for("check_input", method="gmail"))

    set_setting("google_client_id", client_id)
    set_setting("google_client_secret", client_secret)
    flash("Google OAuth сохранен. Теперь можно нажать 'Подключить Gmail'.")
    return redirect(url_for("check_input", method="gmail"))


@app.route("/oauth/gmail/callback", methods=["GET"])
def gmail_oauth_callback():
    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    expected_state = session.get("gmail_oauth_state", "")
    access_token = session.get("gmail_oauth_access_token", "")
    email_account, existing_subs = get_access_context(access_token)

    if not code or not expected_state or state != expected_state or not email_account:
        flash("Ошибка OAuth-сессии. Попробуй подключить Gmail заново.")
        return redirect(url_for("check_input", method="gmail"))

    client_id, client_secret = get_google_oauth_config()
    if not client_id or not client_secret:
        flash("OAuth для Gmail пока не настроен на сервере.")
        return redirect(url_for("check_input", method="gmail"))

    try:
        token_response = requests.post(
            GOOGLE_TOKEN_URL,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": url_for("gmail_oauth_callback", _external=True),
            },
            timeout=20,
        )
        if token_response.status_code != 200:
            flash("Не удалось получить токен Google OAuth.")
            return redirect(url_for("check_input", method="gmail"))
        token_data = token_response.json()
        access_token_google = token_data.get("access_token", "")
        refresh_token = token_data.get("refresh_token", "")
        expires_in = int(token_data.get("expires_in", 3600))
        token_expiry = datetime.utcfromtimestamp(datetime.utcnow().timestamp() + expires_in - 60).isoformat()
        scope = token_data.get("scope", "")
        if "https://www.googleapis.com/auth/gmail.readonly" not in scope:
            flash(
                "Google не выдал доступ к Gmail. Добавь scope Gmail API в OAuth consent screen, "
                "сохрани изменения и подключи Gmail снова."
            )
            return redirect(url_for("check_input", method="gmail"))

        if not access_token_google:
            flash("Google не вернул access token.")
            return redirect(url_for("check_input", method="gmail"))

        save_oauth_connection(
            account_email=email_account,
            provider="google",
            access_token=access_token_google,
            refresh_token=refresh_token,
            token_expiry=token_expiry,
            scope=scope,
        )
        parsed = fetch_subscriptions_from_gmail_api(email_account)
        inserted = upsert_subscriptions(email_account, parsed, existing_subs, source_method="gmail")
        update_oauth_sync_status(email_account, "google", f"ok: imported {inserted} new")
        flash(f"Gmail подключен. Найдено {inserted} новых подписок.")
    except Exception as exc:
        update_oauth_sync_status(email_account, "google", f"error: {str(exc)[:220]}")
        flash(f"Ошибка Gmail OAuth: {exc}")

    session.pop("gmail_oauth_state", None)
    session.pop("gmail_oauth_access_token", None)
    return redirect(url_for("check_method"))


@app.route("/dashboard/sync-gmail", methods=["POST"])
def sync_gmail_now():
    access_token = request.form.get("access_token", "").strip()
    email_account, existing_subs = get_access_context(access_token)
    if not email_account:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))
    try:
        parsed = fetch_subscriptions_from_gmail_api(email_account)
        inserted = upsert_subscriptions(email_account, parsed, existing_subs, source_method="gmail")
        update_oauth_sync_status(email_account, "google", f"ok: imported {inserted} new")
        flash(f"Синхронизация Gmail завершена: добавлено {inserted} подписок.")
    except Exception as exc:
        update_oauth_sync_status(email_account, "google", f"error: {str(exc)[:220]}")
        flash(f"Ошибка синхронизации Gmail: {exc}")
    return redirect(url_for("check_method"))


@app.route("/dashboard/sync-mailru", methods=["POST"])
def sync_mailru_now():
    access_token = request.form.get("access_token", "").strip()
    account_email, existing_subs = get_access_context(access_token)
    if not account_email:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    with db_conn() as conn:
        row = conn.execute(
            """
            SELECT account_email, imap_host, imap_port, mailbox_login, app_password
            FROM mailbox_connections
            WHERE account_email = ?
            """,
            (account_email,),
        ).fetchone()
    if not row or row["imap_host"] != "imap.mail.ru":
        flash("Сначала подключи Mail.ru.")
        return redirect(url_for("check_input", method="mailru"))

    try:
        parsed = fetch_subscriptions_from_imap(
            imap_host=row["imap_host"],
            imap_port=int(row["imap_port"]),
            mailbox_login=row["mailbox_login"],
            app_password=row["app_password"],
        )
        inserted = upsert_subscriptions(account_email, parsed, existing_subs, source_method="mailru")
        with db_conn() as conn:
            conn.execute(
                """
                UPDATE mailbox_connections
                SET last_sync_at = ?, last_sync_status = ?
                WHERE account_email = ?
                """,
                (datetime.utcnow().isoformat(), f"ok: imported {inserted} new", account_email),
            )
        flash(f"Синхронизация Mail.ru завершена: найдено {inserted} новых подписок.")
    except Exception as exc:
        with db_conn() as conn:
            conn.execute(
                """
                UPDATE mailbox_connections
                SET last_sync_at = ?, last_sync_status = ?
                WHERE account_email = ?
                """,
                (datetime.utcnow().isoformat(), f"error: {str(exc)[:220]}", account_email),
            )
        flash(f"Ошибка синхронизации Mail.ru: {exc}")

    return redirect(url_for("check_method"))


@app.route("/dashboard/cleanup-duplicates", methods=["POST"])
def cleanup_duplicates():
    access_token = request.form.get("access_token", "").strip()
    account_email, _ = get_access_context(access_token)
    if not account_email:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    method = request.form.get("method", "").strip().lower()
    method = method if method in ("gmail", "mailru", "card") else None
    removed = cleanup_duplicate_subscriptions(account_email, method)
    if removed == 0:
        flash("Дубликаты не найдены.")
    else:
        flash(f"Удалено дублей: {removed}.")
    return redirect(url_for("check_method"))


@app.route("/dashboard/clear-subscriptions", methods=["POST"])
def clear_subscriptions():
    access_token = request.form.get("access_token", "").strip()
    account_email, _ = get_access_context(access_token)
    if not account_email:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    method = request.form.get("method", "").strip().lower()
    method = method if method in ("gmail", "mailru", "card") else None
    with db_conn() as conn:
        if method:
            conn.execute(
                "DELETE FROM subscriptions WHERE email = ? AND COALESCE(source_method, 'manual') = ?",
                (account_email, method),
            )
        else:
            conn.execute("DELETE FROM subscriptions WHERE email = ?", (account_email,))
    flash("Список подписок очищен.")
    return redirect(url_for("check_method"))


@app.route("/dashboard/import-card-statement", methods=["POST"])
def import_card_statement():
    access_token = request.form.get("access_token", "").strip()
    account_email, existing_subs = get_access_context(access_token)
    if not account_email:
        flash("Сессия истекла. Оплати доступ заново.")
        return redirect(url_for("index"))

    card_last4 = re.sub(r"\D", "", request.form.get("card_last4", ""))[-4:]
    if len(card_last4) != 4:
        flash("Укажи последние 4 цифры карты.")
        return redirect(url_for("check_input", method="card"))

    statement_file = request.files.get("statement_file")
    if not statement_file or not statement_file.filename:
        flash("Загрузи выписку CSV или PDF.")
        return redirect(url_for("check_input", method="card"))

    try:
        raw_bytes = statement_file.read()
        filename = (statement_file.filename or "").lower()
        found_last4 = extract_last4_candidates_from_bytes(raw_bytes, filename)
        if found_last4 and card_last4 not in found_last4:
            cards_list = ", ".join(sorted(found_last4))
            flash(
                f"В выписке не найдена карта *{card_last4}. Найдены карты: {cards_list}. "
                "Укажи правильные последние 4 цифры."
            )
            return redirect(url_for("check_input", method="card"))
        if filename.endswith(".pdf"):
            parsed = extract_subscriptions_from_pdf(raw_bytes, card_last4)
        else:
            csv_text = raw_bytes.decode("utf-8-sig", errors="replace")
            parsed = extract_subscriptions_from_csv(csv_text, card_last4)
    except Exception as exc:
        flash(f"Не удалось прочитать выписку: {exc}")
        return redirect(url_for("check_input", method="card"))

    if not parsed:
        flash("Подписки по этой карте не найдены в выписке.")
        return redirect(url_for("check_input", method="card"))

    inserted = upsert_subscriptions(account_email, parsed, existing_subs, source_method="card")
    removed_noise = cleanup_noise_subscriptions(account_email, card_last4)
    if inserted == 0:
        flash("Новых подписок по карте не добавлено.")
    else:
        flash(f"По карте *{card_last4} добавлено подписок: {inserted}.")
    if removed_noise > 0:
        flash(f"Удалено шумовых операций: {removed_noise}.")
    return redirect(url_for("check_method"))


if __name__ == "__main__":
    init_db()
    ensure_sync_worker_started()
    app.run(debug=True, use_reloader=False)
