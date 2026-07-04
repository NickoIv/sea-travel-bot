"""
hotels.py — модуль подбора отелей (Нячанг, Дананг, Хойан) для sea-travel-bot.

Источник данных: Overpass API — прямой публичный доступ к данным
OpenStreetMap. Никакой регистрации, ключа или карты не требуется вообще.

ВАЖНОЕ ОГРАНИЧЕНИЕ, как и с прошлым вариантом на OpenTripMap:
это данные из OpenStreetMap, а не отзывы живых постояльцев. Здесь нет
пользовательских рейтингов и отзывов. Зато есть кое-что более честное,
чем "условная значимость" OpenTripMap — тег `stars` в OSM, если он
заполнен, это официальная звёздность отеля (когда её вносили редакторы
карты). У большинства объектов его нет — тогда сортируем по количеству
заполненных тегов (адрес, сайт, телефон и т.д.) как грубому proxy
"насколько подробно описан объект в OSM".

Публичные Overpass-инстансы иногда медленные/перегружены — поэтому здесь
список из нескольких зеркал с автоматическим переключением при отказе.

ENV переменные (опционально, ключей не требуется):
  HOTELS_REFRESH_HOUR_UTC - час автообновления кэша, по умолчанию 20 (03:00 Алматы)
"""

import os
import json
import html
import time
import asyncio
import logging
import sqlite3
import urllib.parse
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes

log = logging.getLogger("hotels")

REFRESH_HOUR_UTC = int(os.environ.get("HOTELS_REFRESH_HOUR_UTC", "20"))
DB_PATH = os.environ.get("HOTELS_DB_PATH", "hotels.db")

CITIES = {
    "nha_trang": {"label": "🏖 Нячанг", "lat": 12.2388, "lon": 109.1967,
                  "query_name": "Nha Trang, Vietnam", "tz": "Asia/Ho_Chi_Minh"},
    "da_nang": {"label": "🌉 Дананг", "lat": 16.0544, "lon": 108.2022,
                "query_name": "Da Nang, Vietnam", "tz": "Asia/Ho_Chi_Minh"},
    "hoi_an": {"label": "🏮 Хойан", "lat": 15.8801, "lon": 108.3380,
               "query_name": "Hoi An, Vietnam", "tz": "Asia/Ho_Chi_Minh"},
}
CITY_LABELS = {k: v["label"] for k, v in CITIES.items()}


def local_time_str(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%H:%M")

SEARCH_RADIUS_M = 6000
TOP_N = 10
MAX_RETRIES_PER_MIRROR = 2
RETRY_BACKOFF_SEC = 2

# Несколько публичных зеркал Overpass — если одно лежит/тормозит, пробуем следующее.
# overpass.openstreetmap.ru исключён: плохо доступен из дата-центров Railway (US),
# судя по реальным логам продакшена — стабильно таймаутит.
OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]

# Overpass API просит честно представляться в User-Agent (это часть их fair-use
# политики) — без этого некоторые зеркала отвечают 406/понижают приоритет запроса.
REQUEST_HEADERS = {
    "User-Agent": "sea-travel-bot/1.0 (Telegram hotel finder for personal use)"
}

TOURISM_TYPES = "hotel|guest_house|hostel|motel|apartment"


# ---------------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------------

def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS hotels (
            city_key TEXT, osm_id TEXT, name TEXT, stars TEXT,
            address TEXT, website TEXT, phone TEXT, photo_url TEXT,
            maps_url TEXT, updated_at TEXT,
            PRIMARY KEY (city_key, osm_id)
        )
        """
    )
    # Миграция: в проде уже может существовать таблица со старой схемой
    # (без booking_url/reviews_url) — CREATE TABLE IF NOT EXISTS её не тронет,
    # поэтому добавляем недостающие колонки вручную.
    existing_cols = {row[1] for row in conn.execute("PRAGMA table_info(hotels)").fetchall()}
    for col in ("booking_url", "reviews_url"):
        if col not in existing_cols:
            conn.execute(f"ALTER TABLE hotels ADD COLUMN {col} TEXT")
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
            (city_key, osm_id, name, stars, address, website, phone, photo_url,
             maps_url, booking_url, reviews_url, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (city_key, h["osm_id"], h["name"], h["stars"], h["address"],
             h["website"], h["phone"], h["photo_url"], h["maps_url"],
             h["booking_url"], h["reviews_url"], now),
        )
    conn.commit()
    conn.close()


def load_hotels(city_key: str) -> list[dict]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM hotels WHERE city_key = ? ORDER BY rowid LIMIT ?",
        (city_key, TOP_N),
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def last_updated(city_key: str) -> str | None:
    conn = sqlite3.connect(DB_PATH)
    row = conn.execute("SELECT MAX(updated_at) FROM hotels WHERE city_key = ?", (city_key,)).fetchone()
    conn.close()
    return row[0] if row else None


# ---------------------------------------------------------------------------
# Overpass fetching
# ---------------------------------------------------------------------------

def _query_overpass(query: str) -> dict:
    """Пробует все зеркала по очереди, с retry внутри каждого.

    GET с query-параметром, а не POST — так задокументировано в примерах
    самого Overpass API и меньше шансов на 406 от строгих зеркал.
    Обязательно передаём свой User-Agent (см. REQUEST_HEADERS выше) и
    уважаем заголовок Retry-After при 429, если он есть.
    """
    last_exc = None
    for endpoint in OVERPASS_ENDPOINTS:
        for attempt in range(1, MAX_RETRIES_PER_MIRROR + 1):
            try:
                resp = requests.get(
                    endpoint, params={"data": query}, headers=REQUEST_HEADERS, timeout=45
                )
                if resp.status_code == 429:
                    wait = int(resp.headers.get("Retry-After", 10))
                    log.warning("Overpass mirror %s rate-limited (429), waiting %ds", endpoint, wait)
                    time.sleep(wait)
                    continue
                if resp.status_code >= 500:
                    raise requests.HTTPError(f"{resp.status_code} from {endpoint}")
                resp.raise_for_status()
                return resp.json()
            except requests.RequestException as e:
                last_exc = e
                log.warning("Overpass mirror %s failed (attempt %d/%d): %s",
                            endpoint, attempt, MAX_RETRIES_PER_MIRROR, e)
                time.sleep(RETRY_BACKOFF_SEC)
        log.warning("Mirror %s exhausted, trying next mirror...", endpoint)
    raise last_exc


def _fetch_wikidata_image(qid: str) -> str | None:
    """Best-effort: если у объекта есть привязка к Wikidata — пробуем достать
    фото оттуда (Wikimedia Commons). Не критично, если не получится."""
    try:
        url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
        resp = requests.get(url, headers=REQUEST_HEADERS, timeout=15)
        resp.raise_for_status()
        entity = resp.json()["entities"][qid]
        p18 = entity.get("claims", {}).get("P18")
        if not p18:
            return None
        filename = p18[0]["mainsnak"]["datavalue"]["value"]
        filename_enc = urllib.parse.quote(filename.replace(" ", "_"))
        return f"https://commons.wikimedia.org/wiki/Special:FilePath/{filename_enc}?width=800"
    except Exception as e:
        log.debug("Wikidata image fetch failed for %s: %s", qid, e)
        return None


def fetch_city_hotels(city_key: str) -> list[dict]:
    city = CITIES[city_key]
    query = f"""
[out:json][timeout:30];
(
  node["tourism"~"^({TOURISM_TYPES})$"](around:{SEARCH_RADIUS_M},{city['lat']},{city['lon']});
  way["tourism"~"^({TOURISM_TYPES})$"](around:{SEARCH_RADIUS_M},{city['lat']},{city['lon']});
);
out center tags;
"""
    data = _query_overpass(query)
    elements = data.get("elements", [])

    candidates = []
    for el in elements:
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue

        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        address = ", ".join(
            p for p in [
                tags.get("addr:housenumber"), tags.get("addr:street"),
                tags.get("addr:suburb"), tags.get("addr:city"),
            ] if p
        ) or "адрес не указан в OSM"

        maps_url = (
            f"https://www.openstreetmap.org/?mlat={lat}&mlon={lon}#map=18/{lat}/{lon}"
            if lat and lon else
            f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(name + ' ' + address)}"
        )

        # Ссылки на реальные отзывы — без API, просто конструируем поисковый
        # запрос по названию + городу. Сам бот текст отзывов не показывает,
        # но один тап переносит человека на страницу, где они реально есть.
        search_term = urllib.parse.quote(f"{name} {city['query_name']}")
        booking_url = f"https://www.booking.com/searchresults.html?ss={search_term}"
        reviews_url = f"https://www.google.com/maps/search/?api=1&query={search_term}"

        candidates.append({
            "osm_id": f"{el['type']}/{el['id']}",
            "name": name,
            "stars": tags.get("stars"),
            "address": address,
            "website": tags.get("website") or tags.get("contact:website"),
            "phone": tags.get("phone") or tags.get("contact:phone"),
            "wikidata": tags.get("wikidata"),
            "maps_url": maps_url,
            "booking_url": booking_url,
            "reviews_url": reviews_url,
            "photo_url": None,
            "_tag_count": len(tags),
        })

    def sort_key(h):
        try:
            stars_val = float(h["stars"]) if h["stars"] else -1.0
        except ValueError:
            stars_val = -1.0
        return (stars_val >= 0, stars_val, h["_tag_count"])

    candidates.sort(key=sort_key, reverse=True)
    top = candidates[:TOP_N]

    # фото только для финальных top N — не тратим лишние запросы к Wikidata
    for h in top:
        if h.get("wikidata"):
            h["photo_url"] = _fetch_wikidata_image(h["wikidata"])
        h.pop("wikidata", None)
        h.pop("_tag_count", None)

    return top


def refresh_all_cities():
    log.info("Refreshing hotel cache for all cities...")
    city_keys = list(CITIES.keys())
    for i, key in enumerate(city_keys):
        try:
            hotels = fetch_city_hotels(key)
            save_hotels(key, hotels)
            log.info("  %s: %d hotels saved", key, len(hotels))
        except Exception as e:
            log.error("  %s: refresh failed: %s", key, e)
        if i < len(city_keys) - 1:
            # пауза между городами — иначе Overpass fair-use лимит бьёт по
            # следующему запросу, который улетает почти сразу за предыдущим
            time.sleep(8)


# ---------------------------------------------------------------------------
# Telegram handlers
# ---------------------------------------------------------------------------

def format_hotel_card(idx: int, h: dict) -> str:
    name = html.escape(h["name"])
    address = html.escape(h["address"])
    lines = [f"{idx}. <b>{name}</b>"]
    if h["stars"]:
        try:
            lines.append("⭐" * round(float(h["stars"])) + f" ({h['stars']} офиц. звёзд)")
        except ValueError:
            pass
    lines.append(f"📍 {address}")
    if h["phone"]:
        lines.append(f"📞 {html.escape(h['phone'])}")
    if h["website"]:
        url = h["website"] if h["website"].startswith("http") else f"https://{h['website']}"
        lines.append(f'🌐 <a href="{html.escape(url)}">Сайт отеля</a>')
    lines.append(f'<a href="{html.escape(h["maps_url"])}">Открыть на карте</a>')
    review_links = []
    if h.get("reviews_url"):
        review_links.append(f'<a href="{html.escape(h["reviews_url"])}">Google Maps</a>')
    if h.get("booking_url"):
        review_links.append(f'<a href="{html.escape(h["booking_url"])}">Booking.com</a>')
    if review_links:
        lines.append("📝 Читать отзывы: " + " · ".join(review_links))
    return "\n".join(lines)


async def show_city_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    keyboard = [
        [InlineKeyboardButton(f"{info['label']} · 🕐 {local_time_str(info['tz'])}", callback_data=f"city:{key}")]
        for key, info in CITIES.items()
    ]
    await update.message.reply_text(
        "Выбери город — пришлю подборку отелей (данные из OpenStreetMap, "
        "без пользовательских отзывов — обновляется раз в сутки):",
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
        f"<b>{html.escape(CITY_LABELS[city_key])} — {len(hotels)} отелей</b>\n"
        f"🕐 Сейчас там: {local_time_str(CITIES[city_key]['tz'])}\n"
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
    await update.message.reply_text("Обновляю данные по отелям, подожди ~20-40 сек...")
    await asyncio.to_thread(refresh_all_cities)
    await update.message.reply_text("Готово. Жми '🏨 Отели' чтобы посмотреть.")


async def _startup_autofill(app: Application):
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
        app = Application.builder().token(BOT_TOKEN).build()
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
    log.info("Hotels module (Overpass/OSM, no API key) registered. Daily refresh at %02d:00 UTC.",
              REFRESH_HOUR_UTC)
