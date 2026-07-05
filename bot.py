import asyncio
import feedparser
import logging
import os
import json
import hashlib
import urllib.request
import urllib.parse
import requests
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from zoneinfo import ZoneInfo
from pathlib import Path

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

import hotels  # модуль подбора отелей (Нячанг, Дананг, Хойан) — Overpass API (OSM), без ключей

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATA_FILE = Path("sea_bot_data.json")
SCHEDULE_HOUR_UTC = 5
SCHEDULE_MINUTE_UTC = 0
MAX_NEWS = 10

RSS_FEEDS = [
    {"name": "Vietnam Travel", "url": "https://vietnam.travel/feed", "flag": "🇻🇳"},
    {"name": "Vietnam Plus", "url": "https://en.vietnamplus.vn/rss/travel.rss", "flag": "🇻🇳"},
    {"name": "VnExpress Int'l", "url": "https://e.vnexpress.net/rss/travel.rss", "flag": "🇻🇳"},
    {"name": "Tuoi Tre News", "url": "https://tuoitre.vn/rss/tin-moi-nhat.rss", "flag": "🇻🇳"},
    {"name": "Jakarta Post", "url": "https://www.thejakartapost.com/travel.rss", "flag": "🇮🇩"},
    {"name": "Coconuts Bali", "url": "https://coconuts.co/bali/feed/", "flag": "🌴"},
    {"name": "The Bali Sun", "url": "https://thebalisun.com/feed", "flag": "🌴"},
    {"name": "AsiaOne Travel", "url": "https://www.asiaone.com/rss/travel.xml", "flag": "✈️"},
    {"name": "TTR Weekly", "url": "https://www.ttrweekly.com/site/feed/", "flag": "📰"},
    {"name": "VisaGuide.World", "url": "https://visaguide.world/news/feed", "flag": "🛂"},
]

SEA_KEYWORDS = [
    "vietnam", "hanoi", "ho chi minh", "da nang", "danang", "hoi an", "nha trang",
    "halong", "sapa", "phu quoc", "hue", "saigon",
    "indonesia", "bali", "jakarta", "lombok", "komodo", "ubud", "denpasar",
    "seminyak", "canggu", "yogyakarta", "surabaya", "sumatra", "java island",
    "singapore", "sentosa", "changi",
    "beach", "resort", "temple", "diving", "island", "visa", "flight", "travel",
    "tourism", "hotel", "tour", "destination",
    # визы и погранформальности — то, что чаще всего теряется без этих слов
    "e-visa", "evisa", "immigration", "arrival card", "border", "entry requirement",
    "passport", "visa-free", "visa exemption", "overstay",
]

# Слова специально для фильтра "🛂 Визы и правила" — уже, чем общий SEA_KEYWORDS,
# чтобы не тащить в этот раздел пляжи и рестораны
VISA_KEYWORDS = [
    "visa", "e-visa", "evisa", "immigration", "arrival card", "border",
    "entry requirement", "passport", "visa-free", "visa exemption",
    "overstay", "customs", "checkpoint",
]

# Ключевые слова для быстрых фильтров по странам (в дополнение к общему SEA_KEYWORDS)
COUNTRY_KEYWORDS = {
    "vietnam": ["vietnam", "hanoi", "ho chi minh", "da nang", "danang", "hoi an",
                "nha trang", "halong", "sapa", "phu quoc", "hue", "saigon"],
    "indonesia": ["indonesia", "bali", "jakarta", "lombok", "komodo", "ubud",
                  "denpasar", "seminyak", "canggu", "yogyakarta", "surabaya"],
    "singapore": ["singapore", "sentosa", "changi"],
}
COUNTRY_LABELS = {
    "vietnam": "🇻🇳 Вьетнам",
    "indonesia": "🇮🇩 Индонезия/Бали",
    "singapore": "🇸🇬 Сингапур",
}

# Часовые пояса — Бали (WITA) отличается от Джакарты (WIB) на 1 час,
# берём Бали, так как именно за ним в первую очередь следим
TIMEZONES = {
    "vietnam": "Asia/Ho_Chi_Minh",
    "indonesia": "Asia/Makassar",
    "singapore": "Asia/Singapore",
}
ALMATY_TZ = "Asia/Almaty"

# Координаты для погоды — те же города, что в подборе отелей, плюс Бали и Сингапур
WEATHER_LOCATIONS = {
    "nha_trang": {"label": "🏖 Нячанг", "lat": 12.2388, "lon": 109.1967},
    "da_nang": {"label": "🌉 Дананг", "lat": 16.0544, "lon": 108.2022},
    "hoi_an": {"label": "🏮 Хойан", "lat": 15.8801, "lon": 108.3380},
    "bali": {"label": "🌴 Бали (Денпасар)", "lat": -8.6500, "lon": 115.2167},
    "singapore_w": {"label": "🇸🇬 Сингапур", "lat": 1.3521, "lon": 103.8198},
}

WEATHER_CODES = {
    0: "☀️ Ясно", 1: "🌤 Малооблачно", 2: "⛅ Переменная облачность", 3: "☁️ Облачно",
    45: "🌫 Туман", 48: "🌫 Изморозь",
    51: "🌦 Морось", 53: "🌦 Морось", 55: "🌦 Морось",
    61: "🌧 Дождь", 63: "🌧 Дождь", 65: "🌧 Сильный дождь",
    80: "🌧 Ливень", 81: "🌧 Сильный ливень", 82: "⛈ Очень сильный ливень",
    95: "⛈ Гроза", 96: "⛈ Гроза с градом", 99: "⛈ Сильная гроза с градом",
}

# ISO 3166-1 alpha-2 коды для Nager.Date — Сингапур может не поддерживаться,
# обрабатываем это как обычный "нет данных", а не падение
HOLIDAY_COUNTRY_CODES = {
    "vietnam": "VN",
    "indonesia": "ID",
    "singapore": "SG",
}

# Официальные визовые порталы — проверено перед добавлением, не с агрегаторов.
# Важно: для граждан Казахстана виза в Сингапур ОБЯЗАТЕЛЬНА (подтверждено
# на официальном сайте ICA) — это не visa-free направление, в отличие от
# распространённого заблуждения.
VISA_LINKS = {
    "vietnam": {
        "label": "🇻🇳 Вьетнам",
        "note": "Электронная виза (e-visa) доступна для всех стран, до 90 дней",
        "url": "https://evisa.gov.vn",
    },
    "indonesia": {
        "label": "🇮🇩 Индонезия",
        "note": "e-VOA онлайн, 30 дней + одно продление на 30 дней",
        "url": "https://evisa.imigrasi.go.id",
    },
    "singapore": {
        "label": "🇸🇬 Сингапур",
        "note": "⚠️ Для граждан Казахстана виза ОБЯЗАТЕЛЬНА — это не visa-free направление",
        "url": "https://www.ica.gov.sg/enter-transit-depart/entering-singapore/visa_requirements/visa-detail-page/kazakhstan",
    },
}


def fmt_visa_links() -> str:
    lines = ["🔗 <b>Официальные визовые порталы</b>\n"]
    for info in VISA_LINKS.values():
        lines.append(f"{info['label']}\n{info['note']}\n<a href=\"{info['url']}\">Открыть официальный сайт</a>\n")
    lines.append("<i>Требования меняются — сверяй актуальные условия прямо на портале перед подачей.</i>")
    return "\n".join(lines)


# Какие "отельные" города (ключи из hotels.CITIES) относятся к какой стране —
# для навигации Страна → Отели. Вьетнам ведёт к выбору из 3 городов,
# Индонезия сразу к Бали, Сингапур сразу к Сингапуру.
COUNTRY_HOTEL_CITIES = {
    "vietnam": ["nha_trang", "da_nang", "hoi_an"],
    "indonesia": ["bali"],
    "singapore": ["singapore"],
}

# Какие погодные локации (ключи WEATHER_LOCATIONS) относятся к какой стране
COUNTRY_WEATHER_KEYS = {
    "vietnam": ["nha_trang", "da_nang", "hoi_an"],
    "indonesia": ["bali"],
    "singapore": ["singapore_w"],
}

# Короткое имя страны без флага — для заголовков
COUNTRY_SHORT = {
    "vietnam": "Вьетнам",
    "indonesia": "Индонезия (Бали)",
    "singapore": "Сингапур",
}


def country_hub_text(country_key: str) -> str:
    """Заголовок странички страны с текущим временем."""
    tz = TIMEZONES[country_key]
    return (
        f"{COUNTRY_LABELS[country_key]}\n"
        f"🕐 Местное время: <b>{local_time_str(tz)}</b> "
        f"(у тебя в Алматы {local_time_str(ALMATY_TZ)})\n\n"
        "Что показать?"
    )


def country_hub_kb(country_key: str):
    """Инлайн-меню внутри страны."""
    rows = [
        [InlineKeyboardButton("📰 Новости", callback_data=f"c_news:{country_key}"),
         InlineKeyboardButton("🌤 Погода", callback_data=f"c_weather:{country_key}")],
        [InlineKeyboardButton("🛂 Виза", callback_data=f"c_visa:{country_key}"),
         InlineKeyboardButton("📅 Праздники", callback_data=f"c_holidays:{country_key}")],
        [InlineKeyboardButton("🏨 Отели", callback_data=f"c_hotels:{country_key}")],
    ]
    return InlineKeyboardMarkup(rows)


def country_back_kb(country_key: str):
    return InlineKeyboardMarkup([[InlineKeyboardButton("← Назад к стране", callback_data=f"c_hub:{country_key}")]])


def fmt_single_visa(country_key: str) -> str:
    info = VISA_LINKS[country_key]
    return (
        f"🛂 <b>Виза — {info['label']}</b>\n\n"
        f"{info['note']}\n\n"
        f"<a href=\"{info['url']}\">Открыть официальный портал</a>\n\n"
        "<i>Требования меняются — сверяй актуальные условия на портале перед подачей.</i>"
    )


def fmt_country_weather(country_key: str) -> str:
    keys = COUNTRY_WEATHER_KEYS[country_key]
    with ThreadPoolExecutor(max_workers=max(len(keys), 1)) as pool:
        blocks = list(pool.map(_weather_for_one, keys))
    return f"🌤 <b>Погода — {COUNTRY_SHORT[country_key]}</b>\n" + "─" * 28 + "\n\n" + "\n\n".join(blocks)


def fmt_country_holidays(country_key: str) -> str:
    code = HOLIDAY_COUNTRY_CODES[country_key]
    holidays = fetch_upcoming_holidays(code)
    return "📅 <b>Ближайшие праздники</b>\n\n" + format_holidays_block(COUNTRY_LABELS[country_key], holidays)


def local_time_str(tz_name: str) -> str:
    return datetime.now(ZoneInfo(tz_name)).strftime("%H:%M")


def countries_text() -> str:
    return (
        "🗺 <b>Страны, за которыми слежу</b>\n"
        f"<i>(для сравнения — у тебя в Алматы сейчас {local_time_str(ALMATY_TZ)})</i>\n\n"
        f"🇻🇳 Вьетнам — 🕐 {local_time_str(TIMEZONES['vietnam'])}\n"
        "Ханой, Хошимин, Дананг, Хойан, Нячанг, Фукуок\n\n"
        f"🇮🇩 Индонезия (Бали) — 🕐 {local_time_str(TIMEZONES['indonesia'])}\n"
        "Бали, Ломбок, Комодо, Джакарта\n\n"
        f"🇸🇬 Сингапур — 🕐 {local_time_str(TIMEZONES['singapore'])}\n"
        "Сентоза, центр города"
    )

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ─── Перевод через бесплатный Google Translate ───────────────────────────────

def translate_text(text: str, target_lang: str = "ru") -> str:
    """Переводит текст через бесплатный (неофициальный) Google Translate endpoint.
    target_lang — код языка назначения: 'ru', 'vi' (вьетнамский), 'id' (индонезийский) и т.д."""
    if not text or not text.strip():
        return text
    try:
        text = text[:500]
        params = urllib.parse.urlencode({
            "client": "gtx",
            "sl": "auto",
            "tl": target_lang,
            "dt": "t",
            "q": text,
        })
        url = f"https://translate.googleapis.com/translate_a/single?{params}"
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = ""
        for block in data[0]:
            if block[0]:
                result += block[0]
        return result.strip() if result.strip() else text
    except Exception as e:
        log.warning(f"Перевод не удался: {e}")
        return text


def translate_to_russian(text: str) -> str:
    return translate_text(text, "ru")


# ─── Хранилище данных ────────────────────────────────────────────────────────

def load_data():
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"subscribers": [], "sent_hashes": []}


def save_data(data):
    with open(DATA_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_subscriber(uid):
    data = load_data()
    if uid not in data["subscribers"]:
        data["subscribers"].append(uid)
        save_data(data)


def remove_subscriber(uid):
    data = load_data()
    if uid in data["subscribers"]:
        data["subscribers"].remove(uid)
        save_data(data)


def is_subscribed(uid):
    return uid in load_data()["subscribers"]


def mark_sent(h):
    data = load_data()
    data["sent_hashes"].append(h)
    data["sent_hashes"] = data["sent_hashes"][-500:]
    save_data(data)


def is_sent(h):
    return h in load_data()["sent_hashes"]


# ─── Парсинг RSS ─────────────────────────────────────────────────────────────

def news_hash(entry):
    return hashlib.md5((entry.get("link", "") + entry.get("title", "")).encode()).hexdigest()


FEED_REQUEST_HEADERS = {"User-Agent": "sea-travel-bot/1.0 (Telegram news digest for personal use)"}
FEED_TIMEOUT_SEC = 12


def is_relevant(entry, keywords=None):
    text = (entry.get("title", "") + " " + entry.get("summary", "") + " " + entry.get("link", "")).lower()
    return any(kw in text for kw in (keywords or SEA_KEYWORDS))


def _fetch_one_feed(feed_cfg: dict):
    """Скачивает и парсит один фид. Возвращает (feed_cfg, feed) или (feed_cfg, None) при ошибке.

    Важно: feedparser.parse(url) сам по себе не имеет таймаута и может
    зависнуть на медленном/битом сервере. Поэтому сначала качаем контент
    через requests с явным таймаутом, а парсим уже локальный байт-поток.
    """
    try:
        resp = requests.get(feed_cfg["url"], headers=FEED_REQUEST_HEADERS, timeout=FEED_TIMEOUT_SEC)
        resp.raise_for_status()
        return feed_cfg, feedparser.parse(resp.content)
    except Exception as e:
        log.warning(f"Feed error {feed_cfg['name']}: {e}")
        return feed_cfg, None


def fetch_news(limit=MAX_NEWS, keywords=None):
    results = []
    seen = set()
    now = datetime.utcnow()
    max_age_days = 30

    # Фиды независимы друг от друга по сети — тянем параллельно, иначе с
    # десятком источников по 12 сек таймаута каждый digest мог бы собираться
    # почти две минуты в худшем случае.
    with ThreadPoolExecutor(max_workers=len(RSS_FEEDS)) as pool:
        fetched = list(pool.map(_fetch_one_feed, RSS_FEEDS))

    for feed_cfg, feed in fetched:
        if feed is None:
            continue
        for entry in feed.entries[:15]:
            h = news_hash(entry)
            if h in seen or is_sent(h):
                continue
            if not is_relevant(entry, keywords):
                continue

            # Фильтр по дате — только новости не старше 30 дней
            published = ""
            pub_dt = None
            if hasattr(entry, "published_parsed") and entry.published_parsed:
                pub_dt = datetime(*entry.published_parsed[:6])
                age_days = (now - pub_dt).days
                if age_days > max_age_days:
                    continue
                published = pub_dt.strftime("%d %b %Y")

            title_ru = translate_to_russian(entry.get("title", ""))
            summary_raw = entry.get("summary", "")[:400]
            summary_ru = translate_to_russian(summary_raw) if summary_raw else ""

            results.append({
                "hash": h,
                "title": title_ru or entry.get("title", "Без заголовка"),
                "link": entry.get("link", ""),
                "summary": summary_ru,
                "source": feed_cfg["name"],
                "flag": feed_cfg["flag"],
                "published": published,
                "pub_dt": pub_dt,
            })
            seen.add(h)

    # Сортируем по дате — сначала свежие
    results.sort(key=lambda x: x["pub_dt"] or datetime.min, reverse=True)
    # Убираем служебное поле перед возвратом
    for r in results:
        r.pop("pub_dt", None)
    return results[:limit]


# ─── Форматирование ──────────────────────────────────────────────────────────

def fmt_item(item, i):
    summary = item["summary"].replace("<", "&lt;").replace(">", "&gt;") if item["summary"] else ""
    if summary:
        dot = summary.find(". ")
        if dot > 40:
            summary = summary[:dot + 1]
        summary = f"\n<i>{summary[:200]}</i>"
    date = f" • {item['published']}" if item["published"] else ""
    return (
        f"{item['flag']} <b>{i}. {item['title']}</b>\n"
        f"<code>{item['source']}{date}</code>"
        f"{summary}\n"
        f"<a href=\"{item['link']}\">Читать →</a>"
    )


def fmt_digest(news_list):
    if not news_list:
        return "😴 Новых новостей пока нет. Загляни позже!"
    date_str = datetime.utcnow().strftime("%d %B %Y")
    header = f"🌴 <b>Дайджест ЮВА</b> — {date_str}\n{'─' * 28}\n\n"
    items = "\n\n".join(fmt_item(n, i + 1) for i, n in enumerate(news_list))
    return header + items + "\n\n<i>Подписан на ежедневный дайджест ✅</i>"


# ─── Курс валют (KZT → VND/IDR/SGD/USD) ──────────────────────────────────────
# fawazahmed0/exchange-api: 200+ валют, без ключа, статический JSON на CDN,
# обновляется раз в сутки через GitHub Actions. Основной URL — jsdelivr,
# запасной — Cloudflare Pages (так и рекомендовано в документации проекта).

EXCHANGE_URLS = [
    "https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/kzt.json",
    "https://latest.currency-api.pages.dev/v1/currencies/kzt.json",
]


def fetch_kzt_rates() -> dict | None:
    for url in EXCHANGE_URLS:
        try:
            resp = requests.get(url, timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as e:
            log.warning(f"Currency fetch failed for {url}: {e}")
    return None


def fmt_exchange(data: dict | None) -> str:
    if not data or "kzt" not in data:
        return "😕 Не удалось получить курс валют. Попробуй чуть позже."
    rates = data["kzt"]
    lines = [f"💱 <b>Курс тенге</b> — {data.get('date', '—')}", ""]
    if rates.get("usd"):
        lines.append(f"1000 ₸ ≈ {1000 * rates['usd']:.2f} USD")
    if rates.get("vnd"):
        lines.append(f"1000 ₸ ≈ {1000 * rates['vnd']:,.0f} VND — Вьетнам".replace(",", " "))
    if rates.get("idr"):
        lines.append(f"1000 ₸ ≈ {1000 * rates['idr']:,.0f} IDR — Индонезия".replace(",", " "))
    if rates.get("sgd"):
        lines.append(f"1000 ₸ ≈ {1000 * rates['sgd']:.2f} SGD — Сингапур")
    lines.append("\n<i>Источник: exchange-api (fawazahmed0), обновляется раз в сутки</i>")
    return "\n".join(lines)


# ─── Погода (Open-Meteo — без ключа) ─────────────────────────────────────────

def fetch_weather(lat: float, lon: float) -> dict:
    resp = requests.get(
        "https://api.open-meteo.com/v1/forecast",
        params={
            "latitude": lat, "longitude": lon,
            "current": "temperature_2m,relative_humidity_2m,precipitation,weather_code,wind_speed_10m",
            "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max,uv_index_max",
            "timezone": "auto",
            "forecast_days": 3,
        },
        timeout=10,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_air_quality(lat: float, lon: float) -> dict | None:
    try:
        resp = requests.get(
            "https://air-quality-api.open-meteo.com/v1/air-quality",
            params={"latitude": lat, "longitude": lon, "current": "us_aqi"},
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Air quality fetch failed: {e}")
        return None


def _aqi_label(aqi: float) -> str:
    if aqi <= 50:
        return "хорошее"
    if aqi <= 100:
        return "умеренное"
    if aqi <= 150:
        return "вредно для чувствительных групп"
    if aqi <= 200:
        return "вредно"
    return "очень вредно"


def _uv_label(uv: float) -> str:
    if uv < 3:
        return "низкий"
    if uv < 6:
        return "умеренный"
    if uv < 8:
        return "высокий"
    if uv < 11:
        return "очень высокий"
    return "экстремальный"


def format_weather_block(label: str, data: dict | None, air: dict | None) -> str:
    if data is None:
        return f"<b>{label}</b>\n😕 Не удалось получить погоду"
    cur = data.get("current", {})
    daily = data.get("daily", {})
    code = cur.get("weather_code")
    desc = WEATHER_CODES.get(code, "🌡")
    lines = [f"<b>{label}</b>"]

    temp = cur.get("temperature_2m")
    humidity = cur.get("relative_humidity_2m")
    if temp is not None:
        line = f"{desc} {temp:.0f}°C"
        if humidity is not None:
            line += f", влажность {humidity}%"
        lines.append(line)
    else:
        lines.append(f"{desc} данные временно неполные")

    wind = cur.get("wind_speed_10m")
    if wind is not None:
        lines.append(f"💨 Ветер {wind:.0f} км/ч")
    if daily.get("temperature_2m_max"):
        lo, hi = daily["temperature_2m_min"][0], daily["temperature_2m_max"][0]
        rain = daily.get("precipitation_probability_max", [None])[0]
        line = f"Сегодня: {lo:.0f}…{hi:.0f}°C"
        if rain is not None:
            line += f", вероятность дождя {rain}%"
        lines.append(line)
        uv = daily.get("uv_index_max", [None])[0]
        if uv is not None:
            lines.append(f"☀️ УФ-индекс: {uv:.0f} ({_uv_label(uv)})")
    if air and air.get("current", {}).get("us_aqi") is not None:
        aqi = air["current"]["us_aqi"]
        lines.append(f"🌬 Качество воздуха: {aqi} — {_aqi_label(aqi)}")
    return "\n".join(lines)


def _weather_for_one(loc_key: str) -> str:
    loc = WEATHER_LOCATIONS[loc_key]
    try:
        data = fetch_weather(loc["lat"], loc["lon"])
    except Exception as e:
        log.warning(f"Weather fetch failed for {loc_key}: {e}")
        data = None
    air = fetch_air_quality(loc["lat"], loc["lon"]) if data is not None else None
    return format_weather_block(loc["label"], data, air)


def fetch_all_weather_text() -> str:
    with ThreadPoolExecutor(max_workers=len(WEATHER_LOCATIONS)) as pool:
        blocks = list(pool.map(_weather_for_one, WEATHER_LOCATIONS.keys()))
    return "🌤 <b>Погода</b>\n" + "─" * 28 + "\n\n" + "\n\n".join(blocks)


# ─── Праздники и выходные дни (Nager.Date — без ключа) ───────────────────────

def fetch_upcoming_holidays(country_code: str) -> list | None:
    """None означает, что страна не поддерживается источником — не ошибка."""
    try:
        resp = requests.get(f"https://date.nager.at/api/v3/NextPublicHolidays/{country_code}", timeout=10)
        if resp.status_code == 404:
            return None
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        log.warning(f"Holidays fetch failed for {country_code}: {e}")
        return None


def format_holidays_block(label: str, holidays: list | None) -> str:
    if holidays is None:
        return f"<b>{label}</b>\n😕 Источник не покрывает эту страну"
    if not holidays:
        return f"<b>{label}</b>\nБлижайших праздников не найдено"
    lines = [f"<b>{label}</b>"]
    for h in holidays[:5]:
        date_obj = datetime.strptime(h["date"], "%Y-%m-%d")
        extra = f" ({h['name']})" if h.get("name") and h["name"] != h.get("localName") else ""
        lines.append(f"📅 {date_obj.strftime('%d.%m.%Y')} — {h.get('localName', h.get('name'))}{extra}")
    return "\n".join(lines)


def fetch_all_holidays_text() -> str:
    blocks = []
    for key, code in HOLIDAY_COUNTRY_CODES.items():
        holidays = fetch_upcoming_holidays(code)
        blocks.append(format_holidays_block(COUNTRY_LABELS[key], holidays))
    return "📅 <b>Ближайшие праздники и выходные дни</b>\n" + "─" * 28 + "\n\n" + "\n\n".join(blocks)


# ─── Короткая сводка для утренней рассылки (погода одной строкой + курс) ─────

def _short_weather_line(loc_key: str) -> str:
    loc = WEATHER_LOCATIONS[loc_key]
    try:
        data = fetch_weather(loc["lat"], loc["lon"])
        temp = data.get("current", {}).get("temperature_2m")
        code = data.get("current", {}).get("weather_code")
        if temp is not None:
            return f"{loc['label']}: {WEATHER_CODES.get(code, '🌡')} {temp:.0f}°C"
    except Exception as e:
        log.warning(f"Short weather failed for {loc_key}: {e}")
    return f"{loc['label']}: нет данных"


def fetch_morning_addon_text() -> str:
    """Компактный блок для ежедневного дайджеста — не дублирует полные
    /🌤 Погода и /💱 Курс тенге, а даёт быструю сводку одним взглядом."""
    with ThreadPoolExecutor(max_workers=len(WEATHER_LOCATIONS)) as pool:
        weather_lines = list(pool.map(_short_weather_line, WEATHER_LOCATIONS.keys()))

    parts = ["🌤 <b>Погода:</b>"] + weather_lines

    rates = fetch_kzt_rates()
    if rates and "kzt" in rates:
        r = rates["kzt"]
        parts.append("")
        parts.append("💱 <b>1000 ₸ ≈</b>")
        if r.get("vnd"):
            parts.append(f"{1000 * r['vnd']:,.0f} VND".replace(",", " "))
        if r.get("idr"):
            parts.append(f"{1000 * r['idr']:,.0f} IDR".replace(",", " "))
        if r.get("sgd"):
            parts.append(f"{1000 * r['sgd']:.2f} SGD")

    return "\n".join(parts)


# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Путешественник"
    reply_kb = ReplyKeyboardMarkup(
        [["🇻🇳 Вьетнам", "🇮🇩 Индонезия", "🇸🇬 Сингапур"],
         ["🌴 Все новости", "🛂 Все визы"],
         ["🧰 Ещё", "🔔 Подписаться"]],
        resize_keyboard=True,
        is_persistent=True,
    )
    await update.message.reply_text(
        f"Привет, {name}! 🌏\n\n"
        "Выбери страну — внутри будут её <b>новости, погода, виза, отели и время</b>:\n\n"
        "🇻🇳 <b>Вьетнам</b> — Нячанг, Дананг, Хойан и др.\n"
        "🇮🇩 <b>Индонезия</b> — Бали\n"
        "🇸🇬 <b>Сингапур</b>\n\n"
        "🗓 Дайджест каждое утро в <b>10:00 по Алматы</b> — новости + сводка погоды и курса тенге\n"
        "🧰 В кнопке «Ещё» — курс валют, праздники, визовые порталы, справка\n\n"
        "Кнопки внизу всегда под рукой 👇",
        parse_mode="HTML",
        reply_markup=reply_kb,
    )


async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Собираю и перевожу новости...")
    news = await asyncio.to_thread(fetch_news)
    for n in news:
        mark_sent(n["hash"])
    await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)


async def cmd_reply_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text == "🌴 Все новости":
        msg = await update.message.reply_text("⏳ Собираю и перевожу новости...")
        news = await asyncio.to_thread(fetch_news)
        for n in news:
            mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)

    elif text == "🛂 Все визы":
        await update.message.reply_text(fmt_visa_links(), parse_mode="HTML", disable_web_page_preview=True)

    elif text in COUNTRY_LABELS.values():
        country_key = next(k for k, v in COUNTRY_LABELS.items() if v == text)
        await update.message.reply_text(
            country_hub_text(country_key), parse_mode="HTML",
            reply_markup=country_hub_kb(country_key),
        )

    elif text == "🔔 Подписаться":
        add_subscriber(chat_id)
        await update.message.reply_text(
            "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.\n\nОтписаться: /unsubscribe",
            parse_mode="HTML"
        )

    elif text == "🧰 Ещё":
        await update.message.reply_text("🧰 Дополнительные функции:", reply_markup=TOOLS_MENU_KB)


# ─── Инлайн-меню внутри страны ────────────────────────────────────────────────
# Паттерн ^c_ — не пересекается с cb() (get_news|...), tools_, city:/more: из hotels

async def country_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    action, country_key = q.data.split(":", 1)

    if action == "c_hub":
        await q.edit_message_text(
            country_hub_text(country_key), parse_mode="HTML",
            reply_markup=country_hub_kb(country_key),
        )

    elif action == "c_news":
        await q.edit_message_text(f"⏳ Собираю новости — {COUNTRY_SHORT[country_key]}...")
        news = await asyncio.to_thread(fetch_news, MAX_NEWS, COUNTRY_KEYWORDS[country_key])
        for n in news:
            mark_sent(n["hash"])
        await q.edit_message_text(fmt_digest(news), parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=country_back_kb(country_key))

    elif action == "c_weather":
        await q.edit_message_text("⏳ Собираю погоду...")
        weather_text = await asyncio.to_thread(fmt_country_weather, country_key)
        await q.edit_message_text(weather_text, parse_mode="HTML", reply_markup=country_back_kb(country_key))

    elif action == "c_visa":
        await q.edit_message_text(fmt_single_visa(country_key), parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=country_back_kb(country_key))

    elif action == "c_holidays":
        await q.edit_message_text("⏳ Проверяю праздники...")
        holidays_text = await asyncio.to_thread(fmt_country_holidays, country_key)
        await q.edit_message_text(holidays_text, parse_mode="HTML", reply_markup=country_back_kb(country_key))

    elif action == "c_hotels":
        city_keys = COUNTRY_HOTEL_CITIES[country_key]
        if len(city_keys) == 1:
            # одна отельная локация (Бали, Сингапур) — сразу показываем отели
            await hotels.send_city_hotels_via_query(q, city_keys[0])
        else:
            # несколько городов (Вьетнам) — показываем выбор города
            await hotels.show_city_menu_via_query(q, city_keys)


# ─── Инлайн-подменю "🧰 Ещё" ──────────────────────────────────────────────────
# Отдельный хэндлер с паттерном ^tools_ — не пересекается ни с cb()
# (get_news|subscribe|countries|about|back), ни с hotels.py (^city:)

TOOLS_MENU_KB = InlineKeyboardMarkup([
    [InlineKeyboardButton("💱 Курс тенге", callback_data="tools_exchange"),
     InlineKeyboardButton("📅 Праздники", callback_data="tools_holidays")],
    [InlineKeyboardButton("🔗 Визовые порталы", callback_data="tools_visalinks")],
    [InlineKeyboardButton("📍 Страны", callback_data="tools_countries"),
     InlineKeyboardButton("ℹ️ О боте", callback_data="tools_about")],
])

TOOLS_BACK_KB = InlineKeyboardMarkup([[InlineKeyboardButton("← Ещё функции", callback_data="tools_root")]])


async def tools_cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()

    if q.data == "tools_root":
        await q.edit_message_text("🧰 Дополнительные функции:", reply_markup=TOOLS_MENU_KB)

    elif q.data == "tools_exchange":
        await q.edit_message_text("⏳ Получаю курс валют...")
        data = await asyncio.to_thread(fetch_kzt_rates)
        await q.edit_message_text(fmt_exchange(data), parse_mode="HTML", reply_markup=TOOLS_BACK_KB)

    elif q.data == "tools_holidays":
        await q.edit_message_text("⏳ Проверяю ближайшие праздники...")
        holidays_text = await asyncio.to_thread(fetch_all_holidays_text)
        await q.edit_message_text(holidays_text, parse_mode="HTML", reply_markup=TOOLS_BACK_KB)

    elif q.data == "tools_visalinks":
        await q.edit_message_text(fmt_visa_links(), parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=TOOLS_BACK_KB)

    elif q.data == "tools_countries":
        await q.edit_message_text(countries_text(), parse_mode="HTML", reply_markup=TOOLS_BACK_KB)

    elif q.data == "tools_about":
        await q.edit_message_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Собирает новости о Вьетнаме, Индонезии (Бали) и Сингапуре, "
            "переводит на русский, удаляет дубли.\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы (+ короткая сводка погоды и курса)\n"
            "🇷🇺 Автоперевод на русский\n🆓 Без рекламы",
            parse_mode="HTML", reply_markup=TOOLS_BACK_KB
        )


async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_user.id)
    await update.message.reply_text(
        "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.\n\nОтписаться: /unsubscribe",
        parse_mode="HTML"
    )


async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_user.id)
    await update.message.reply_text("🔕 Отписан. Снова подписаться: /subscribe")


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    status = "✅ Подписан" if is_subscribed(update.effective_user.id) else "🔕 Не подписан"
    await update.message.reply_text(
        f"📊 <b>Статус</b>\n\nТвой статус: {status}\nПодписчиков всего: {len(data['subscribers'])}\n"
        f"Источников RSS: {len(RSS_FEEDS)}\nРассылка: 10:00 Алматы\n🇷🇺 Перевод: включён",
        parse_mode="HTML"
    )


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Команды</b>\n\n/start — главное меню\n/news — все новости прямо сейчас\n"
        "/subscribe — подписаться\n/unsubscribe — отписаться\n/status — статус\n"
        "/refresh_hotels — обновить данные по отелям\n/help — справка\n\n"
        "Выбери страну (🇻🇳/🇮🇩/🇸🇬) — внутри её новости, погода, виза, отели и время. "
        "«🌴 Все новости» — сводка по всем странам. В «🧰 Ещё» — курс тенге, "
        "праздники, визовые порталы и справка о боте.",
        parse_mode="HTML"
    )


# ─── Callback кнопки (новости) ────────────────────────────────────────────────
# Паттерн ограничен конкретными значениями, чтобы не конфликтовать с
# CallbackQueryHandler(hotels.on_city_selected, pattern="^city:") из hotels.py —
# без ограничения этот хэндлер перехватывал бы вообще любой callback_data.

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "get_news":
        await q.edit_message_text("⏳ Собираю и перевожу новости на русский...")
        news = await asyncio.to_thread(fetch_news)
        for n in news:
            mark_sent(n["hash"])
        kb = [[InlineKeyboardButton("🔔 Подписаться на дайджест", callback_data="subscribe")]]
        await q.edit_message_text(fmt_digest(news), parse_mode="HTML",
                                   disable_web_page_preview=True, reply_markup=InlineKeyboardMarkup(kb))

    elif q.data == "subscribe":
        add_subscriber(uid)
        await q.edit_message_text(
            "✅ <b>Подписка оформлена!</b>\n\nДайджест каждое утро в <b>10:00 по Алматы</b>.\nОтписаться: /unsubscribe",
            parse_mode="HTML")

    elif q.data == "countries":
        kb = [[InlineKeyboardButton("← Назад", callback_data="back")]]
        await q.edit_message_text(countries_text(), parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data == "about":
        kb = [[InlineKeyboardButton("← Назад", callback_data="back")]]
        await q.edit_message_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Собирает новости о Вьетнаме, Индонезии (Бали) и Сингапуре, автоматически переводит на русский язык, "
            "фильтрует по ключевым словам, удаляет дубли.\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы\n"
            "🇷🇺 Автоперевод на русский язык\n"
            "🆓 Бесплатно и без рекламы",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data == "back":
        kb = [
            [InlineKeyboardButton("🌴 Получить новости", callback_data="get_news"),
             InlineKeyboardButton("🔔 Подписаться", callback_data="subscribe")],
            [InlineKeyboardButton("📍 Страны ЮВА", callback_data="countries"),
             InlineKeyboardButton("ℹ️ О боте", callback_data="about")],
        ]
        await q.edit_message_text("🌏 Главное меню — SEA Travel News",
                                   reply_markup=InlineKeyboardMarkup(kb))


# ─── Ежедневная рассылка ─────────────────────────────────────────────────────

async def send_daily(app):
    data = load_data()
    if not data["subscribers"]:
        return
    news = await asyncio.to_thread(fetch_news)
    if not news:
        return
    addon = await asyncio.to_thread(fetch_morning_addon_text)
    text = fmt_digest(news) + "\n\n" + "─" * 28 + "\n\n" + addon
    for n in news:
        mark_sent(n["hash"])
    for uid in data["subscribers"]:
        try:
            await app.bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning(f"Send error {uid}: {e}")


# ─── Запуск ──────────────────────────────────────────────────────────────────

async def _daily_job(context):
    """Колбэк для job_queue.run_daily — сигнатура (context), а не (app)."""
    await send_daily(context.application)


async def _post_init(app: Application):
    """post_init для Application: автозаполнение кэша отелей при старте,
    если он пуст (первый запуск / после редеплоя на Railway)."""
    await hotels._startup_autofill(app)


def main():
    if not BOT_TOKEN:
        print("❌ Укажи BOT_TOKEN в переменных окружения!")
        return

    app = Application.builder().token(BOT_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(
        cb, pattern=r"^(get_news|subscribe|countries|about|back)$"
    ))
    app.add_handler(CallbackQueryHandler(country_cb, pattern=r"^c_"))
    app.add_handler(CallbackQueryHandler(tools_cb, pattern=r"^tools_"))

    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_reply_buttons))

    hotels.register(app)  # добавляет /refresh_hotels, CallbackQueryHandler("^city:"), суточный cron

    # Ежедневная рассылка через встроенный job_queue PTB — он работает в том же
    # event loop, что и бот. Прежний вариант (AsyncIOScheduler + asyncio.create_task
    # в лямбде) молча не срабатывал: планировщик стартовал до запуска loop, и
    # create_task не попадал в нужный event loop. Это и была причина, почему
    # дайджест в 10:00 не приходил.
    from datetime import time as dtime, timezone as dtz
    app.job_queue.run_daily(
        _daily_job,
        time=dtime(hour=SCHEDULE_HOUR_UTC, minute=SCHEDULE_MINUTE_UTC, tzinfo=dtz.utc),
        name="daily_digest",
    )

    log.info("🌴 SEA Travel News Bot (+ 🏨 Отели) запущен! Рассылка в %02d:%02d UTC.",
              SCHEDULE_HOUR_UTC, SCHEDULE_MINUTE_UTC)
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
