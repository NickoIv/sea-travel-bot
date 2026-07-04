"""
hotels.py — модуль подбора отелей (Нячанг, Дананг, Хойан) для sea-travel-bot.

Источник данных: Google Places API (New). Подключается к существующему
Application одним вызовом register(app) — свой токен бота модулю не нужен,
только GOOGLE_PLACES_API_KEY.

ENV переменные (добавить в Railway Variables к уже существующим):
  GOOGLE_PLACES_API_KEY - ключ Google Cloud с включённым Places API (New)
  HOTELS_REFRESH_HOUR_UTC - час автообновления кэша, по умолчанию 20 (03:00 Алматы)
"""

import os
import json
import html
import time
import asyncio
import logging
import sqlite3
from datetime import datetime, timezone

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

log = logging.getLogger("hotels")

GOOGLE_PLACES_API_KEY = os.environ.get("GOOGLE_PLACES_API_KEY", "")
REFRESH_HOUR_UTC = int(os.environ.get("HOTELS_REFRESH_HOUR_UTC", "20"))
DB_PATH = os.environ.get("HOTELS_DB_PATH", "hotels.db")

CITIES = {
    "nha_trang": "Nha Trang, Vietnam",
    "da_nang": "Da Nang, Vietnam",
    "hoi_an": "Hoi An, Vietnam",
}
CITY_LABELS = {
    "nha_trang": "🏖 Нячанг",
    "da_nang": "🌉 Дананг",
    "hoi_an": "🏮 Хойан",
}

MIN_REVIEWS = 50
MIN_REVIEWS_FLOOR = 15
TOP_N = 10
MAX_RETRIES = 3
RETRY_BACKOFF_SEC = 2

PLACES_SEARCH_URL = "https://places.googleapis.com/v1/places:searchText"
PLACES_PHOTO_URL = "https://places.googleapis.com/v1/{photo_name}/media"

FIELD_MASK_SEARCH = ",".join([
    "places.id", "places.displayName", "places.rating", "places.userRatingCount",
    "places.priceLevel", "places.formattedAddress", "places.googleMapsUri", "places.photos",
])
FIELD_MASK_DETAILS = "reviews.text,reviews.rating"

PRICE_LEVEL_MAP = {
    "PRICE_LEVEL_FREE": "—",
    "PRICE_LEVEL_INEXPENSIVE": "$",
    "PRICE_LEVEL_MODERATE": "$$",
    "PRICE_LEVEL_EXPENSIVE": "$$$",
    "PRICE_LEVEL_VERY_EXPENSIVE": "$$$$",
}


# ---------------------------------------------------------------------------
# Storage (отдельная SQLite-база, не пересекается с sea_bot_data.json)
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hotels (
            city_key TEXT, place_id TEXT, name TEXT, rating REAL,
            review_count INTEGER, price_level TEXT, address TEXT,
            maps_url TEXT, photo_url TEXT, review_snippets TEXT, updated_at TEXT,
            PRIMARY KEY (city_key, place_id)
        )
        """
    )
    conn.commit()
    conn.close()


def save_hotels(city_key: str, hotels: list[dict]):
    conn = sqlite3.connect(DB_PATH)
    conn.execute("DELETE FROM hotels WHERE city_key = ?", (city_key,))
    now = datetime.now(timezone.utc).isoformat()
    for h in hotels:
        conn.execute(
            """
            INSERT INTO hotels
            (city_key, place_id, name, rating, review_count, price_level,
             address, maps_url, photo_url, review_snippets, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (city_key, h["place_id"], h["name"], h["rating"], h["review_count"],
             h["price_level"], h["address"], h["maps_url"], h["photo_url"],
             json.dumps(h["review_snippets"], ensure_ascii=False), now),
        )
    conn.commit()
    conn.close()


def load_hotels(city_key: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM hotels WHERE city_key = ? ORDER BY rating DESC, review_count DESC LIMIT ?",
        (city_key, TOP_N),
    ).fetchall()
    conn.close()
    result = []
    for r in rows:
        d = dict(r)
        d["review_snippets"] = json.loads(d["review_snippets"] or "[]")
        result.append(d)
    return result


def last_updated(city_key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(updated_at) FROM hotels WHERE city_key = ?", (city_key,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Google Places fetching
# ---------------------------------------------------------------------------

def _request_with_retry(method: str, url: str, **kwargs) -> requests.Response:
    last_exc = None
    for attempt in range(1, MAX_RETRIES + 1):
        try:
            resp = requests.request(method, url, **kwargs)
            if resp.status_code == 429 or resp.status_code >= 500:
                raise requests.HTTPError(f"{resp.status_code}: {resp.text[:200]}")
            resp.raise_for_status()
            return resp
        except requests.RequestException as e:
            last_exc = e
            if attempt < MAX_RETRIES:
                wait = RETRY_BACKOFF_SEC * attempt
                log.warning("Request failed (attempt %d/%d), retrying in %ds: %s",
                            attempt, MAX_RETRIES, wait, e)
                time.sleep(wait)
    raise last_exc


def _search_hotels_raw(city_query: str, min_reviews: int) -> list[dict]:
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
        "X-Goog-FieldMask": FIELD_MASK_SEARCH,
    }
    payload = {
        "textQuery": f"best hotels in {city_query}",
        "includedType": "lodging",
        "strictTypeFiltering": True,
        "languageCode": "en",
        "maxResultCount": 20,
    }
    resp = _request_with_retry("POST", PLACES_SEARCH_URL, headers=headers, json=payload, timeout=30)
    places = resp.json().get("places", [])

    hotels = []
    for p in places:
        rating = p.get("rating")
        review_count = p.get("userRatingCount", 0)
        if rating is None or review_count < min_reviews:
            continue
        photo_url = None
        photos = p.get("photos") or []
        if photos:
            photo_name = photos[0]["name"]
            photo_url = f"{PLACES_PHOTO_URL.format(photo_name=photo_name)}?maxWidthPx=800&key={GOOGLE_PLACES_API_KEY}"
        hotels.append({
            "place_id": p["id"],
            "name": p.get("displayName", {}).get("text", "Без названия"),
            "rating": rating,
            "review_count": review_count,
            "price_level": PRICE_LEVEL_MAP.get(p.get("priceLevel", ""), "н/д"),
            "address": p.get("formattedAddress", ""),
            "maps_url": p.get("googleMapsUri", ""),
            "photo_url": photo_url,
            "review_snippets": [],
        })
    hotels.sort(key=lambda h: (h["rating"], h["review_count"]), reverse=True)
    return hotels


def fetch_city_hotels(city_query: str) -> list[dict]:
    hotels = _search_hotels_raw(city_query, MIN_REVIEWS)
    if len(hotels) < 5 and MIN_REVIEWS_FLOOR < MIN_REVIEWS:
        log.info("Only %d hotels passed MIN_REVIEWS=%d for '%s', retrying with floor=%d",
                  len(hotels), MIN_REVIEWS, city_query, MIN_REVIEWS_FLOOR)
        hotels = _search_hotels_raw(city_query, MIN_REVIEWS_FLOOR)
    top_candidates = hotels[:TOP_N]
    for h in top_candidates:
        h["review_snippets"] = fetch_review_snippets(h["place_id"])
    return top_candidates


def fetch_review_snippets(place_id: str, limit: int = 3) -> list[str]:
    url = f"https://places.googleapis.com/v1/places/{place_id}"
    headers = {"X-Goog-Api-Key": GOOGLE_PLACES_API_KEY, "X-Goog-FieldMask": FIELD_MASK_DETAILS}
    try:
        resp = _request_with_retry("GET", url, headers=headers, params={"languageCode": "en"}, timeout=20)
        reviews = resp.json().get("reviews", [])
    except requests.RequestException as e:
        log.warning("Review fetch failed for %s: %s", place_id, e)
        return []
    snippets = []
    for r in reviews[:limit]:
        text = r.get("text", {}).get("text", "").strip().replace("\n", " ")
        if text:
            snippets.append(text[:180] + ("…" if len(text) > 180 else ""))
    return snippets


def refresh_all_cities():
    if not GOOGLE_PLACES_API_KEY:
        log.error("GOOGLE_PLACES_API_KEY не задан — пропускаю обновление отелей.")
        return
    log.info("Refreshing hotel cache for all cities...")
    for key, query in CITIES.items():
        try:
            hotels = fetch_city_hotels(query)
            save_hotels(key, hotels)
            log.info("  %s: %d hotels saved", key, len(hotels))
        except Exception as e:
            log.error("  %s: refresh failed: %s", key, e)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

def format_hotel_card(idx: int, h: dict) -> str:
    stars = "⭐" * round(h["rating"])
    name = html.escape(h["name"])
    address = html.escape(h["address"])
    lines = [
        f"{idx}. <b>{name}</b>",
        f"{stars} {h['rating']} ({h['review_count']} отзывов) · {h['price_level']}",
        f"📍 {address}",
    ]
    if h["review_snippets"]:
        snippets = " / ".join(html.escape(s) for s in h["review_snippets"][:2])
        lines.append(f"💬 {snippets}")
    if h["maps_url"]:
        lines.append(f'<a href="{html.escape(h["maps_url"])}">Открыть в Google Maps</a>')
    return "\n".join(lines)


async def show_city_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Вызывается из главного меню sea-travel-bot по кнопке '🏨 Отели'."""
    keyboard = [
        [InlineKeyboardButton(label, callback_data=f"city:{key}")]
        for key, label in CITY_LABELS.items()
    ]
    await update.message.reply_text(
        "Выбери город — пришлю топ отелей по рейтингу и живым отзывам "
        "(данные обновляются раз в сутки):",
        reply_markup=InlineKeyboardMarkup(keyboard),
    )


async def on_city_selected(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    city_key = query.data.split(":", 1)[1]
    hotels = load_hotels(city_key)

    if not hotels:
        await query.message.reply_text(
            "Кэш ещё не заполнен для этого города — бот заполняет его автоматически "
            "при старте. Если прошло больше пары минут, вызови /refresh_hotels."
        )
        return

    updated = last_updated(city_key)
    header = (
        f"<b>{html.escape(CITY_LABELS[city_key])} — топ {len(hotels)} отелей</b>\n"
        f"<i>обновлено: {html.escape(updated or '—')}</i>\n"
    )
    await query.message.reply_text(header, parse_mode="HTML")

    for i, h in enumerate(hotels, start=1):
        text = format_hotel_card(i, h)
        try:
            if h["photo_url"]:
                await query.message.reply_photo(photo=h["photo_url"], caption=text, parse_mode="HTML")
            else:
                await query.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)
        except Exception as e:
            log.warning("Failed to send hotel card for %s: %s", h["name"], e)
            await query.message.reply_text(text, parse_mode="HTML", disable_web_page_preview=True)


async def refresh_hotels_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not GOOGLE_PLACES_API_KEY:
        await update.message.reply_text(
            "GOOGLE_PLACES_API_KEY не задан в переменных окружения — добавь его в Railway Variables."
        )
        return
    await update.message.reply_text("Обновляю данные по отелям, подожди ~30 сек...")
    await asyncio.to_thread(refresh_all_cities)
    await update.message.reply_text("Готово. Жми '🏨 Отели' чтобы посмотреть.")


async def _startup_autofill(app: Application):
    if not GOOGLE_PLACES_API_KEY:
        log.warning("GOOGLE_PLACES_API_KEY не задан — модуль отелей не сможет обновлять данные.")
        return
    empty_cities = [key for key in CITIES if not load_hotels(key)]
    if not empty_cities:
        log.info("Hotels cache already populated for all cities.")
        return
    log.info("Empty hotels cache for %s, running initial fetch...", empty_cities)
    await asyncio.to_thread(refresh_all_cities)
    log.info("Hotels startup autofill complete.")


# ---------------------------------------------------------------------------
# Public API for bot.py
# ---------------------------------------------------------------------------

def register(app: Application):
    """Подключает модуль отелей к уже собранному Application.

    Использование в bot.py:
        import hotels
        app = Application.builder().token(BOT_TOKEN).post_init(hotels_and_news_startup).build()
        hotels.register(app)
    """
    init_db()
    app.add_handler(CommandHandler("refresh_hotels", refresh_hotels_cmd))
    # узкий паттерн — не пересекается с CallbackQueryHandler(cb) в bot.py,
    # который слушает get_news/subscribe/countries/about/back
    app.add_handler(CallbackQueryHandler(on_city_selected, pattern=r"^city:"))

    scheduler = AsyncIOScheduler(timezone="UTC")
    scheduler.add_job(refresh_all_cities, "cron", hour=REFRESH_HOUR_UTC, minute=0)
    scheduler.start()
    log.info("Hotels module registered. Daily refresh at %02d:00 UTC.", REFRESH_HOUR_UTC)
