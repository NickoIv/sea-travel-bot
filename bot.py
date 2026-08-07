import asyncio
import feedparser
import logging
import os
import json
import hashlib
import html
import random
import urllib.request
import urllib.parse
import re
import requests
from pypinyin import pinyin, Style
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, time as dtime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, ContextTypes,
    MessageHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATA_FILE = Path("sea_bot_data.json")
MAX_NEWS = 8

# ── Webhook-режим (Render/аналоги) ──────────────────────────────────────────
# Без явного WEBHOOK_MODE=1 (или запуска на платформе, которая сама
# прописывает свою переменную) бот работает как раньше, локальным polling'ом.
WEBHOOK_MODE = bool(os.getenv("WEBHOOK_MODE") or os.getenv("RENDER") or os.getenv("K_SERVICE"))
DAILY_CRON_SECRET = os.getenv("DAILY_CRON_SECRET", "")
PORT = int(os.getenv("PORT", "8080"))

# ── Хранилище: Upstash Redis (бесплатно, без карты, REST API) ──────────────
# Без карты нельзя использовать Google Cloud Storage — Upstash делает то же
# самое (переживает перезапуски контейнера) без привязки платёжных данных.
UPSTASH_URL = os.getenv("UPSTASH_REDIS_REST_URL", "")
UPSTASH_TOKEN = os.getenv("UPSTASH_REDIS_REST_TOKEN", "")
UPSTASH_KEY = "sea_bot_data"

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

# ─── RSS ─────────────────────────────────────────────────────────────────────

RSS_FEEDS = [
    {"name": "Vietnam Travel", "url": "https://vietnam.travel/feed", "flag": "🇻🇳", "country": "vn"},
    {"name": "Vietnam Plus",   "url": "https://en.vietnamplus.vn/rss/travel.rss", "flag": "🇻🇳", "country": "vn"},
    {"name": "Jakarta Post",   "url": "https://www.thejakartapost.com/travel.rss", "flag": "🇮🇩", "country": "id"},
    {"name": "Coconuts Bali",  "url": "https://coconuts.co/bali/feed/", "flag": "🌴", "country": "id"},
    {"name": "AsiaOne Travel", "url": "https://www.asiaone.com/rss/travel.xml", "flag": "✈️", "country": "sg"},
    {"name": "TTR Weekly",     "url": "https://www.ttrweekly.com/site/feed/", "flag": "📰", "country": "all"},
    {"name": "Egypt Independent", "url": "https://egyptindependent.com/feed/", "flag": "🇪🇬", "country": "eg"},
    {"name": "Google News: Hainan Travel",
     "url": "https://news.google.com/rss/search?q=%22Hainan%22+OR+%22Sanya%22+travel+tourism&hl=en-US&gl=US&ceid=US:en",
     "flag": "🇨🇳", "country": "cn"},
    {"name": "Tropical Hainan", "url": "https://tropicalhainan.com/feed", "flag": "🇨🇳", "country": "cn"},
]

COUNTRY_KEYWORDS = {
    "vn": ["vietnam","hanoi","ho chi minh","da nang","danang","hoi an","nha trang","halong","sapa","phu quoc","hue","saigon","viet"],
    "id": ["indonesia","bali","jakarta","lombok","komodo","ubud","denpasar","seminyak","canggu","yogyakarta","java"],
    "sg": ["singapore","sentosa","changi"],
    "eg": ["egypt","cairo","luxor","aswan","hurghada","sharm el sheikh","alexandria","giza","pyramids","red sea","nile"],
    "cn": ["china","hainan","sanya","haikou"],
}
# Общие "туристические" слова — отдельно от топонимов. Упоминание одного лишь
# города/страны в новости ещё не значит, что новость про туризм (могла быть
# про политику, бизнес, спорт и т.д.) — is_relevant() требует и то, и другое.
TOURISM_TOPIC_KEYWORDS = [
    "travel", "tourism", "tourist", "tour", "visa", "flight", "airline", "airport",
    "hotel", "resort", "beach", "destination", "vacation", "holiday", "cruise",
    "sightseeing", "backpack", "itinerary", "visitor", "attraction",
]
ALL_KEYWORDS = COUNTRY_KEYWORDS["vn"] + COUNTRY_KEYWORDS["id"] + COUNTRY_KEYWORDS["sg"] + COUNTRY_KEYWORDS["eg"] + COUNTRY_KEYWORDS["cn"] + TOURISM_TOPIC_KEYWORDS

# Таймзоны для отображения локального времени — единая на страну (все города
# каждой страны здесь лежат в одном часовом поясе, отдельная таблица не нужна)
TIMEZONES = {
    "vn": "Asia/Ho_Chi_Minh",
    "id": "Asia/Makassar",
    "sg": "Asia/Singapore",
    "eg": "Africa/Cairo",
    "cn": "Asia/Shanghai",
}

# Флаги и названия стран — используется в меню "Страны" и в подписях разделов
COUNTRIES = {
    "vn": ("🇻🇳", "Вьетнам"),
    "id": ("🇮🇩", "Индонезия"),
    "sg": ("🇸🇬", "Сингапур"),
    "eg": ("🇪🇬", "Египет"),
    "cn": ("🇨🇳", "Китай"),
}

def local_time_str(country_code: str) -> str:
    tz = TIMEZONES.get(country_code)
    if not tz:
        return ""
    return datetime.now(ZoneInfo(tz)).strftime("%H:%M")

# ─── Города: координаты (погода) + отели ──────────────────────────────────────
# Единый источник данных на город — из него строятся и погода, и отели, и
# навигация. Новости, виза и рейсы остаются на уровне страны (у RSS-фидов
# нет городской гранулярности, а рейсы бронируются в аэропорт всей страны).

CITIES = {
    "vn": [
        {"key": "hanoi", "icon": "🏙", "name": "Ханой", "en": "Hanoi", "lat": 21.0285, "lon": 105.8542},
        {"key": "da_nang", "icon": "🌉", "name": "Дананг", "en": "Da Nang", "lat": 16.0544, "lon": 108.2022},
        {"key": "hoi_an", "icon": "🏮", "name": "Хойан", "en": "Hoi An", "lat": 15.8801, "lon": 108.3380},
        {"key": "nha_trang", "icon": "🏖", "name": "Нячанг", "en": "Nha Trang", "lat": 12.2388, "lon": 109.1967},
        {"key": "phu_quoc", "icon": "🏝", "name": "Фукуок", "en": "Phu Quoc", "lat": 10.2899, "lon": 103.9840},
    ],
    "id": [
        {"key": "denpasar", "icon": "🌆", "name": "Денпасар / Юг Бали", "en": "Denpasar Bali", "lat": -8.6705, "lon": 115.2126},
        {"key": "ubud", "icon": "🌿", "name": "Убуд", "en": "Ubud Bali", "lat": -8.5069, "lon": 115.2625},
        {"key": "lombok", "icon": "🏝", "name": "Ломбок", "en": "Lombok", "lat": -8.6524, "lon": 116.3240},
    ],
    "sg": [
        {"key": "singapore", "icon": "🇸🇬", "name": "Сингапур", "en": "Singapore", "lat": 1.3521, "lon": 103.8198},
    ],
    "eg": [
        {"key": "cairo", "icon": "🏛", "name": "Каир", "en": "Cairo", "lat": 30.0444, "lon": 31.2357},
        {"key": "hurghada", "icon": "🏖", "name": "Хургада", "en": "Hurghada", "lat": 27.2579, "lon": 33.8116},
        {"key": "sharm", "icon": "🤿", "name": "Шарм-эль-Шейх", "en": "Sharm El Sheikh", "lat": 27.9158, "lon": 34.3300},
        {"key": "luxor", "icon": "🏺", "name": "Луксор", "en": "Luxor", "lat": 25.6872, "lon": 32.6396},
    ],
    "cn": [
        {"key": "hainan", "icon": "🏝", "name": "Хайнань (Санья)", "en": "Sanya Hainan", "lat": 18.2528, "lon": 109.5119},
    ],
}

def find_city(country_code: str, city_key: str) -> dict | None:
    return next((c for c in CITIES.get(country_code, []) if c["key"] == city_key), None)

# ─── Статичные данные: визы и рейсы (уровень страны) ──────────────────────────

VISA_INFO = {
    "vn": (
        "🇻🇳 <b>Вьетнам — условия въезда</b>\n\n"
        "🟢 <b>Казахстан</b>: безвизовый въезд до <b>30 дней</b>\n"
        "🟢 <b>Россия</b>: безвизовый въезд до <b>30 дней</b>\n\n"
        "📋 <b>E-Visa</b> (30–90 дней, однократная/многократная):\n"
        "• Сайт: evisa.xuatnhapcanh.gov.vn\n"
        "• Стоимость: $25 (однократная), $50 (многократная)\n"
        "• Срок оформления: 3 рабочих дня\n\n"
        "📄 Нужен паспорт действующий минимум 6 месяцев"
    ),
    "id": (
        "🇮🇩 <b>Индонезия (Бали) — условия въезда</b>\n\n"
        "🟡 <b>Казахстан/Россия</b>: виза по прилёту <b>Visa on Arrival</b>\n"
        "• Стоимость: $35 USD\n"
        "• Срок: 30 дней + продление ещё на 30 дней\n"
        "• Оплата: наличные USD/IDR на стойке в аэропорту\n\n"
        "🟢 <b>E-Visa</b> (удобнее, оформить заранее):\n"
        "• Сайт: molina.imigrasi.go.id\n"
        "• Стоимость: $35 + сбор ~$3\n"
        "• Срок оформления: 3-5 дней\n\n"
        "📄 Нужен обратный билет и бронь отеля"
    ),
    "sg": (
        "🇸🇬 <b>Сингапур — условия въезда</b>\n\n"
        "🟢 <b>Казахстан</b>: безвизовый въезд до <b>30 дней</b>\n"
        "🟢 <b>Россия</b>: безвизовый въезд до <b>30 дней</b>\n\n"
        "📋 С 2024 года обязательна регистрация <b>SG Arrival Card</b>:\n"
        "• Сайт: eservices.ica.gov.sg\n"
        "• Бесплатно, заполнить за 3 дня до прилёта\n\n"
        "📄 Нужен обратный билет и достаточно средств (~S$100/день)"
    ),
    "eg": (
        "🇪🇬 <b>Египет — условия въезда</b>\n\n"
        "🟡 <b>Казахстан/Россия</b>: виза по прилёту <b>Visa on Arrival</b> или e-Visa заранее\n\n"
        "📋 <b>Visa on Arrival</b>:\n"
        "• Оплата наличными в аэропорту: $25\n"
        "• Срок: 30 дней\n\n"
        "🟢 <b>E-Visa</b> (оформить заранее):\n"
        "• Сайт: visa2egypt.gov.eg\n"
        "• Стоимость: $25 + сервисный сбор\n"
        "• Срок оформления: 3-7 дней\n\n"
        "📄 Нужен загранпаспорт, действующий минимум 6 месяцев"
    ),
    "cn": (
        "🇨🇳 <b>Китай (Хайнань) — условия въезда</b>\n\n"
        "🟢 <b>Безвизовый режим для острова Хайнань</b>: граждане ряда стран могут "
        "находиться на острове до <b>30 дней</b> без визы — действует только для "
        "Хайнаня, не для материкового Китая\n"
        "⚠️ Список стран-участниц периодически меняется — <b>обязательно сверь себя "
        "по актуальному списку</b> перед поездкой, для Казахстана точный статус "
        "уточняй на официальном портале\n\n"
        "📋 <b>Материковый Китай</b> (если планируешь выезд за пределы Хайнаня):\n"
        "• Нужна обычная виза, оформляется в посольстве/консульстве КНР заранее\n\n"
        "📄 Нужен загранпаспорт, действующий минимум 6 месяцев"
    ),
}

FLIGHTS_INFO = (
    "✈️ <b>Авиарейсы из Алматы</b>\n\n"
    "🇻🇳 <b>Алматы → Вьетнам</b>\n"
    "• Air Astana: ALA–HAN (с пересадкой)\n"
    "• FlyArystan / Air Arabia: через Дубай/Абу-Даби\n"
    "• VietJet / Vietnam Airlines: через Бангкок\n"
    "• В среднем: от $350–600 туда-обратно\n\n"
    "🇮🇩 <b>Алматы → Бали (DPS)</b>\n"
    "• Обычно через Куала-Лумпур (Air Asia) или Сингапур\n"
    "• В среднем: от $450–700 туда-обратно\n\n"
    "🇸🇬 <b>Алматы → Сингапур (SIN)</b>\n"
    "• Air Astana: прямые рейсы ALA–SIN\n"
    "• Singapore Airlines / Scoot через разные хабы\n"
    "• В среднем: от $400–650 туда-обратно\n\n"
    "🇪🇬 <b>Алматы → Египет (HRG/SSH)</b>\n"
    "• Чартерные и прямые рейсы в Хургаду и Шарм-эль-Шейх (сезонно)\n"
    "• В среднем: от $500–800 туда-обратно\n\n"
    "🇨🇳 <b>Алматы → Хайнань (Санья, SYX)</b>\n"
    "• Обычно с пересадкой через Урумчи, Пекин или Гуанчжоу\n"
    "• В среднем: от $400–700 туда-обратно\n\n"
    "🔍 Поиск билетов: aviasales.ru / skyscanner.com / google.com/flights"
)

# ─── eSIM ───────────────────────────────────────────────────────────────────
# Бот не может сам продать eSIM и выдать QR-код — для этого нужен статус
# партнёра-реселлера у провайдера и подключённый платёжный шлюз через
# BotFather. Честный рабочий вариант — прямая ссылка на конкретную страну
# в надёжном маркетплейсе (Airalo — один из самых известных и недорогих,
# ссылки проверены вручную).
ESIM_LINKS = {
    "vn": "https://www.airalo.com/vietnam-esim",
    "id": "https://www.airalo.com/indonesia-esim",
    "sg": "https://www.airalo.com/singapore-esim",
    "eg": "https://www.airalo.com/egypt-esim",
    "cn": "https://www.airalo.com/china-esim",
}

def fmt_esim_info(country_code: str) -> str:
    _, name = COUNTRIES.get(country_code, ("", "?"))
    url = ESIM_LINKS.get(country_code)
    if not url:
        return "Информация недоступна"
    extra = ""
    if country_code == "cn":
        extra = (
            "⚠️ В Китае заблокированы Google, WhatsApp, большинство соцсетей и др. — "
            "для доступа к привычным сервисам нужен VPN, eSIM с местным интернетом "
            "эту блокировку не обходит.\n\n"
        )
    return (
        f"📶 <b>eSIM — {name}</b>\n\n"
        "Мобильный интернет без физической SIM-карты: устанавливается прямо на "
        "телефон за пару минут, работает сразу по прилёту, не нужно искать салон связи.\n\n"
        f"{extra}"
        f"🔍 <a href=\"{url}\">Выбрать и купить eSIM — {name}</a>\n\n"
        "<i>Ссылка на Airalo — один из самых известных и недорогих маркетплейсов eSIM. "
        "Оплата и активация — на их сайте, бот туда только направляет.</i>"
    )

# ─── Перевод ──────────────────────────────────────────────────────────────────

def translate_to_russian(text: str) -> str:
    if not text or not text.strip():
        return text
    text = text[:500]
    for client in ["gtx", "dict-chrome-ex"]:
        try:
            params = urllib.parse.urlencode({"client": client, "sl": "auto", "tl": "ru", "dt": "t", "q": text})
            url = f"https://translate.googleapis.com/translate_a/single?{params}"
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            result = "".join(b[0] for b in data[0] if b[0])
            if result.strip():
                return result.strip()
        except Exception as e:
            log.warning(f"Translate [{client}] failed: {e}")
    # Fallback: MyMemory
    try:
        params = urllib.parse.urlencode({"q": text, "langpair": "en|ru"})
        req = urllib.request.Request(f"https://api.mymemory.translated.net/get?{params}", headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=8) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        result = data.get("responseData", {}).get("translatedText", "")
        if result:
            return result.strip()
    except Exception as e:
        log.warning(f"MyMemory failed: {e}")
    return text

# ─── Погода ──────────────────────────────────────────────────────────────────

WEATHER_ICONS = {"0":"☀️","1":"🌤","2":"⛅","3":"☁️","45":"🌫","48":"🌫","51":"🌦","61":"🌧","71":"❄️","80":"🌦","95":"⛈"}

def get_weather_one(name: str, lat: float, lon: float) -> str:
    try:
        url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
               f"&current=temperature_2m,weathercode&timezone=auto")
        with urllib.request.urlopen(url, timeout=8) as r:
            d = json.loads(r.read())
        temp = round(d["current"]["temperature_2m"])
        code = str(d["current"]["weathercode"])
        icon = WEATHER_ICONS.get(code, "🌡")
        return f"{icon} {name}: <b>{temp}°C</b>"
    except:
        return f"🌡 {name}: нет данных"

# ─── Отели (OpenStreetMap / Overpass API, без ключа) ──────────────────────────
# У Agoda и Trip.com нет бесплатного публичного API — ссылки ведут на поиск
# по названию отеля на их сайтах, а не на гарантированную страницу конкретного
# объекта. Названия и адреса отелей — реальные, из OpenStreetMap. Фото — через
# привязку к Wikidata (тег wikidata в OSM), если она у объекта есть.

OVERPASS_ENDPOINTS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
HOTEL_SEARCH_RADIUS_M = 8000
HOTEL_POOL_SHOW = 30   # сколько отелей "раздаём" за один заход (перемешивание)
HOTEL_PAGE_SIZE = 10   # сколько показываем за раз — дальше "Ещё 10" без похода в сеть

# Небольшой резервный список реальных отелей на случай, если все зеркала
# Overpass одновременно недоступны — чтобы раздел никогда не был пустым.
FALLBACK_HOTELS = {
    "vn": {
        "hanoi": ["Sofitel Legend Metropole Hanoi", "Movenpick Hotel Hanoi", "La Siesta Premium Hang Be", "Peridot Grand Hotel"],
        "da_nang": ["InterContinental Danang Sun Peninsula Resort", "Vinpearl Resort & Spa Da Nang", "Furama Resort Danang"],
        "hoi_an": ["Anantara Hoi An Resort", "Almanity Hoi An Wellness Resort", "Hoi An Ancient House Village Resort"],
        "nha_trang": ["Vinpearl Resort Nha Trang", "Amiana Resort Nha Trang", "InterContinental Nha Trang"],
        "phu_quoc": ["JW Marriott Phu Quoc Emerald Bay", "Premier Residences Phu Quoc", "Salinda Resort Phu Quoc"],
    },
    "id": {
        "denpasar": ["W Bali Seminyak", "Mulia Resort Nusa Dua", "Conrad Bali", "The Legian Bali"],
        "ubud": ["Four Seasons Resort Bali at Sayan", "Komaneka at Bisma", "Mandapa a Ritz-Carlton Reserve"],
        "lombok": ["The Oberoi Lombok", "Sheraton Senggigi Beach Resort"],
    },
    "sg": {
        "singapore": ["Marina Bay Sands", "The Fullerton Hotel Singapore", "Raffles Singapore", "Capella Singapore"],
    },
    "eg": {
        "cairo": ["Four Seasons Hotel Cairo at Nile Plaza", "Kempinski Nile Hotel Cairo", "Sofitel Cairo Nile El Gezirah"],
        "hurghada": ["Steigenberger Al Dau Beach Hotel", "Baron Palace Sahl Hasheesh", "Sunrise Grand Select Crystal Bay"],
        "sharm": ["Rixos Sharm El Sheikh", "Four Seasons Resort Sharm El Sheikh", "Baron Resort Sharm El Sheikh"],
        "luxor": ["Sofitel Winter Palace Luxor", "Steigenberger Nile Palace Luxor", "Hilton Luxor Resort & Spa"],
    },
    "cn": {
        "hainan": ["Mandarin Oriental Sanya", "The Ritz-Carlton Sanya, Yalong Bay", "Sheraton Sanya Resort", "Sanya Marriott Hotel Dadonghai Bay"],
    },
}

# Кэш кандидатов на процесс: Overpass дёргаем один раз на город, а не при
# каждом нажатии — дальше берём новую случайную тридцатку из уже полученного
# пула (мгновенно, без обращения к сети).
_city_hotel_cache: dict[tuple, list] = {}
# Текущая "выданная" тридцатка на город — нужна, чтобы "Ещё 10" продолжала
# именно тот же набор, а не мешала его заново на каждой странице.
_shown_hotels_cache: dict[tuple, list] = {}

def _fetch_osm_hotels(lat: float, lon: float) -> list[dict]:
    query = f"""
[out:json][timeout:20];
(
  node["tourism"~"^(hotel|guest_house|hostel|motel|apartment|resort)$"](around:{HOTEL_SEARCH_RADIUS_M},{lat},{lon});
  way["tourism"~"^(hotel|guest_house|hostel|motel|apartment|resort)$"](around:{HOTEL_SEARCH_RADIUS_M},{lat},{lon});
);
out center tags;
"""
    def _try(endpoint):
        resp = requests.get(
            endpoint, params={"data": query},
            headers={"User-Agent": "sea-travel-bot/1.0 (Telegram hotel finder)"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    # Опрашиваем все зеркала параллельно и берём первый успешный ответ —
    # публичные зеркала Overpass нестабильны, по очереди можно ждать до
    # минуты, если они все одновременно перегружены.
    with ThreadPoolExecutor(max_workers=len(OVERPASS_ENDPOINTS)) as pool:
        futures = {pool.submit(_try, ep): ep for ep in OVERPASS_ENDPOINTS}
        try:
            for future in as_completed(futures, timeout=16):
                endpoint = futures[future]
                try:
                    data = future.result()
                except Exception as e:
                    log.warning(f"Overpass {endpoint} failed: {e}")
                    continue
                hotels = []
                for el in data.get("elements", []):
                    tags = el.get("tags", {})
                    name = tags.get("name")
                    if not name:
                        continue
                    hotels.append({
                        "name": name[:70], "stars": tags.get("stars"),
                        "wikidata": tags.get("wikidata"), "tag_count": len(tags),
                    })
                if hotels:
                    return hotels
        except Exception as e:
            log.warning(f"Overpass mirrors all timed out: {e}")
    return []

def _hotel_rank(h: dict):
    """Сортировка 'от высокой оценки к низкой': у OSM нет пользовательских
    отзывов (это не Booking/TripAdvisor) — упорядочиваем по официальной
    звёздности там, где она указана, а для остальных по полноте карточки
    в OSM (адрес/сайт/телефон и т.д.) как грубому proxy солидности объекта."""
    try:
        stars_val = float(h["stars"]) if h.get("stars") else -1.0
    except (TypeError, ValueError):
        stars_val = -1.0
    return (stars_val, h.get("tag_count", 0))

def shuffle_city_hotels(country_code: str, city_key: str) -> list[dict]:
    """Достаёт (с кэшированием) пул отелей города, выдаёт новую случайную
    тридцатку и сортирует её от высокой оценки к низкой. Реальный сетевой
    запрос к Overpass кэшируется надолго только при успехе — если все зеркала
    недоступны, используем резервный список, но НЕ запоминаем его как
    окончательный результат, чтобы следующая попытка снова сходила в Overpass
    за настоящими данными, а не показывала одни и те же 3-4 отеля навсегда."""
    city = find_city(country_code, city_key)
    if not city:
        return []
    cache_key = (country_code, city_key)
    pool = _city_hotel_cache.get(cache_key)
    if not pool:
        pool = _fetch_osm_hotels(city["lat"], city["lon"])
        if pool:
            _city_hotel_cache[cache_key] = pool
    if not pool:
        fallback_names = FALLBACK_HOTELS.get(country_code, {}).get(city_key, [])
        pool = [{"name": n, "stars": None, "wikidata": None, "tag_count": 0} for n in fallback_names]
    if not pool:
        return []
    chosen = random.sample(pool, min(HOTEL_POOL_SHOW, len(pool)))
    chosen.sort(key=_hotel_rank, reverse=True)
    _shown_hotels_cache[cache_key] = chosen
    return chosen

# Wikimedia Commons отдаёт 403 на прямые ссылки на файлы без описательного
# User-Agent (часть их политики по борьбе со спамом хотлинков). Поэтому фото
# скачиваем сами с этим заголовком и передаём в Telegram уже байтами — иначе
# Telegram пытается получить картинку по голой ссылке своим User-Agent'ом и
# тоже получает 403, фото просто не приходит.
COMMONS_HEADERS = {"User-Agent": "sea-travel-bot/1.0 (Telegram hotel finder; https://github.com/NickoIv/sea-travel-bot)"}

def _fetch_hotel_photo_bytes(qid: str) -> bytes | None:
    """Best-effort: если у отеля в OSM есть привязка к Wikidata — пробуем
    скачать фото оттуда (Wikimedia Commons). Есть далеко не у всех объектов."""
    try:
        url = f"https://www.wikidata.org/wiki/Special:EntityData/{qid}.json"
        resp = requests.get(url, headers=COMMONS_HEADERS, timeout=6)
        resp.raise_for_status()
        entity = resp.json()["entities"][qid]
        p18 = entity.get("claims", {}).get("P18")
        if not p18:
            return None
        filename = p18[0]["mainsnak"]["datavalue"]["value"]
        filename_enc = urllib.parse.quote(filename.replace(" ", "_"))
        photo_url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{filename_enc}?width=800"
        img_resp = requests.get(photo_url, headers=COMMONS_HEADERS, timeout=10)
        img_resp.raise_for_status()
        if not img_resp.headers.get("Content-Type", "").startswith("image/"):
            return None
        return img_resp.content
    except Exception:
        return None

def _name_matches_file_title(name: str, file_title: str) -> bool:
    """Sanity-check перед тем как подставить фото по текстовому поиску (не через
    Wikidata) — без этого можно случайно прицепить чужую картинку к чужому
    названию. Требуем хотя бы одно значимое общее слово (без родовых вроде
    'hotel'/'resort'), а не просто 'что-то нашлось'."""
    stopwords = {"hotel", "resort", "the", "and", "spa", "inn", "villa", "villas"}
    def words(s):
        return {w.lower() for w in re.findall(r"[a-zA-Zа-яА-Я]{3,}", s)} - stopwords
    return bool(words(name) & words(file_title))

def _fetch_named_photo_bytes(name: str, city_en: str) -> bytes | None:
    """Фолбэк, когда у объекта в OSM нет привязки к Wikidata (у отелей и кафе
    это почти всегда так) — ищем фото по названию напрямую в Wikimedia
    Commons, с проверкой совпадения слов, чтобы не подставить не то фото."""
    title = _search_commons_file(f"{name} {city_en}")
    if not title or not _name_matches_file_title(name, title):
        return None
    return _download_commons_file(title)

def _resolve_one_photo(item: dict, city_en: str) -> bytes | None:
    qid = item.get("wikidata")
    if qid:
        photo = _fetch_hotel_photo_bytes(qid)
        if photo:
            return photo
    return _fetch_named_photo_bytes(item["name"], city_en)

def _resolve_page_photos(page: list[dict], city_en: str) -> None:
    """Подтягивает фото только для текущей отображаемой страницы (10 штук),
    а не для всех 30 — иначе ожидание было бы намного дольше. Сначала пробует
    Wikidata (если есть привязка в OSM), затем — поиск по названию в Commons
    напрямую. Мутирует элементы page на месте, добавляя ключ 'photo_bytes'."""
    if not page:
        return
    with ThreadPoolExecutor(max_workers=min(6, len(page))) as pool:
        futures = {pool.submit(_resolve_one_photo, h, city_en): h for h in page}
        try:
            for future in as_completed(futures, timeout=20):
                h = futures[future]
                try:
                    h["photo_bytes"] = future.result()
                except Exception:
                    h["photo_bytes"] = None
        except Exception as e:
            log.warning(f"Photo lookup timed out: {e}")

def _stars_str(stars) -> str:
    try:
        n = round(float(stars))
        return " " + "⭐" * max(1, min(n, 5)) if n else ""
    except (TypeError, ValueError):
        return ""

def fmt_hotels_header(city: dict, country_name: str, count: int) -> str:
    header = f"{city['icon']} <b>Отели — {city['name']}</b> ({country_name})"
    if count:
        header += f"\n<i>{count} вариантов, от высокой оценки к низкой — жми «Показать другие» для новой подборки</i>"
    else:
        header += "\n\n😕 Не удалось получить список отелей. Попробуй ещё раз чуть позже."
    return header

def fmt_hotel_line(i: int, h: dict, city_en: str) -> str:
    name = html.escape(h["name"])
    stars = _stars_str(h.get("stars"))
    q = urllib.parse.quote(f"{h['name']} {city_en}")
    # Agoda/Trip.com не открывали конкретный отель (нет бесплатного текстового
    # поиска) — Booking.com показывает отфильтрованный список по запросу,
    # Google Maps почти всегда попадает точно в объект и показывает реальные
    # рейтинг и отзывы гостей (в отличие от звёздности из OSM).
    booking = f"https://www.booking.com/searchresults.html?ss={q}"
    gmaps = f"https://www.google.com/maps/search/?api=1&query={q}"
    return f"{i}. <b>{name}</b>{stars} — <a href=\"{booking}\">Booking.com</a> · <a href=\"{gmaps}\">Google Maps (отзывы)</a>"

# ─── Фото города (гарантированный визуал) ──────────────────────────────────────
# Фото конкретного отеля есть очень редко (Дананг: 1 из 561!). Зато у самого
# города фото на Wikimedia Commons находится почти всегда — показываем его
# один раз в начале списка, честно подписав как фото города, а не отеля.

_city_photo_cache: dict[str, "bytes | None"] = {}

def _search_commons_file(query: str) -> str | None:
    try:
        resp = requests.get(
            "https://commons.wikimedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "srnamespace": 6, "format": "json", "srlimit": 1},
            headers=COMMONS_HEADERS, timeout=12,
        )
        resp.raise_for_status()
        results = resp.json().get("query", {}).get("search", [])
        return results[0]["title"] if results else None
    except Exception:
        return None

def _download_commons_file(title: str) -> bytes | None:
    try:
        filename = title.split(":", 1)[1] if ":" in title else title
        filename_enc = urllib.parse.quote(filename.replace(" ", "_"))
        url = f"https://commons.wikimedia.org/wiki/Special:FilePath/{filename_enc}?width=1024"
        resp = requests.get(url, headers=COMMONS_HEADERS, timeout=15)
        resp.raise_for_status()
        if not resp.headers.get("Content-Type", "").startswith("image/"):
            return None
        return resp.content
    except Exception:
        return None

def get_city_photo(city: dict) -> bytes | None:
    key = city["key"]
    if key not in _city_photo_cache:
        title = _search_commons_file(f"{city.get('en', city['name'])} city")
        _city_photo_cache[key] = _download_commons_file(title) if title else None
    return _city_photo_cache[key]

# ─── Достопримечательности и кафе (тот же принцип, что и у отелей) ─────────────
# Скраппинг Trip.com не используем: сайт защищён от автоматического сбора
# и требует рендеринга JS, а прямые поисковые ссылки на Trip.com уже не
# сработали для отелей (замена на Google Maps). Здесь та же логика: реальные
# места — из OpenStreetMap через Overpass (бесплатно, без ключа), ссылка —
# на Google Maps с настоящими рейтингом и отзывами.

POI_KINDS = {
    "attractions": {
        "icon": "🏛", "label": "Достопримечательности",
        # Несколько отдельных клаузул вместо одного фильтра — у пляжей,
        # парков и исторических объектов в OSM разные теги (natural/leisure/
        # historic), не только tourism=*. Без этого объекты вроде "пляж"
        # вообще не находились бы, хотя это самое желанное для этих городов.
        "osm_filters": [
            '"tourism"~"^(attraction|museum|viewpoint|gallery|zoo|theme_park|artwork)$"',
            '"historic"',
            '"natural"="beach"',
            '"leisure"="park"',
        ],
        "photos": True,
    },
    "cafes": {
        "icon": "☕", "label": "Кафе и рестораны",
        "osm_filters": ['"amenity"~"^(cafe|restaurant)$"'],
        "photos": True,
    },
}
POI_SEARCH_RADIUS_M = 6000
POI_POOL_SHOW = 30
POI_PAGE_SIZE = 10

_city_poi_cache: dict[tuple, list] = {}
_shown_poi_cache: dict[tuple, list] = {}

# ── Классификация по типу — чтобы список был не "30 непонятных названий
# подряд", а сгруппирован по смыслу (природа / культура / развлечения),
# как в Google Maps ("Things to do") или TripAdvisor. Ключ — значение тега
# OSM, значение — (категория для сортировки/группировки, иконка группы,
# название группы, иконка конкретного пункта, название типа объекта).
ATTRACTION_TYPES = {
    "beach":        ("nature",  "🌄", "Природа и виды", "🏖", "пляж"),
    "viewpoint":    ("nature",  "🌄", "Природа и виды", "🌄", "видовая точка"),
    "park":         ("nature",  "🌄", "Природа и виды", "🌳", "парк"),
    "museum":       ("culture", "🏛", "Культура и история", "🏛", "музей"),
    "gallery":      ("culture", "🏛", "Культура и история", "🎨", "галерея"),
    "artwork":      ("culture", "🏛", "Культура и история", "🗿", "арт-объект"),
    "theme_park":   ("fun",     "🎢", "Развлечения", "🎢", "парк развлечений"),
    "zoo":          ("fun",     "🎢", "Развлечения", "🦁", "зоопарк"),
    "attraction":   ("other",   "📍", "Другое", "📍", "достопримечательность"),
}
CATEGORY_ORDER = {"nature": 0, "culture": 1, "fun": 2, "cafe": 0, "restaurant": 1, "other": 9}

def _classify_poi(kind: str, tags: dict) -> tuple:
    if kind == "cafes":
        if tags.get("amenity") == "restaurant":
            return ("restaurant", "🍽", "Рестораны", "🍽", "ресторан")
        return ("cafe", "☕", "Кафе", "☕", "кафе")
    if tags.get("natural") == "beach":
        return ATTRACTION_TYPES["beach"]
    if tags.get("leisure") == "park":
        return ATTRACTION_TYPES["park"]
    tourism = tags.get("tourism")
    if tourism in ATTRACTION_TYPES:
        return ATTRACTION_TYPES[tourism]
    if tags.get("historic"):
        return ("culture", "🏛", "Культура и история", "🏛", "исторический объект")
    return ATTRACTION_TYPES["attraction"]

def _fetch_osm_poi(kind: str, lat: float, lon: float) -> list[dict]:
    filters = POI_KINDS[kind]["osm_filters"]
    clauses = "".join(
        f'  node[{f}](around:{POI_SEARCH_RADIUS_M},{lat},{lon});\n'
        f'  way[{f}](around:{POI_SEARCH_RADIUS_M},{lat},{lon});\n'
        for f in filters
    )
    query = f"[out:json][timeout:20];\n(\n{clauses});\nout center tags;\n"

    def _try(endpoint):
        resp = requests.get(
            endpoint, params={"data": query},
            headers={"User-Agent": "sea-travel-bot/1.0 (Telegram POI finder)"},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json()

    with ThreadPoolExecutor(max_workers=len(OVERPASS_ENDPOINTS)) as pool:
        futures = {pool.submit(_try, ep): ep for ep in OVERPASS_ENDPOINTS}
        try:
            for future in as_completed(futures, timeout=16):
                endpoint = futures[future]
                try:
                    data = future.result()
                except Exception as e:
                    log.warning(f"Overpass {endpoint} failed: {e}")
                    continue
                items = []
                seen_ids = set()
                for el in data.get("elements", []):
                    el_id = (el.get("type"), el.get("id"))
                    if el_id in seen_ids:
                        continue
                    tags = el.get("tags", {})
                    name = tags.get("name")
                    if not name:
                        continue
                    seen_ids.add(el_id)
                    cat_key, group_icon, group_label, item_icon, item_label = _classify_poi(kind, tags)
                    items.append({
                        "name": name[:70], "wikidata": tags.get("wikidata"),
                        "tag_count": len(tags), "cat_key": cat_key,
                        "group_icon": group_icon, "group_label": group_label,
                        "item_icon": item_icon, "item_label": item_label,
                    })
                if items:
                    return items
        except Exception as e:
            log.warning(f"Overpass mirrors all timed out: {e}")
    return []

def _poi_sort_key(item: dict):
    # Группируем по категории (природа → культура → развлечения → остальное),
    # внутри категории — по полноте карточки в OSM, как и у отелей.
    return (CATEGORY_ORDER.get(item.get("cat_key"), 9), -item.get("tag_count", 0))

def shuffle_city_poi(kind: str, country_code: str, city_key: str) -> list[dict]:
    """Тот же принцип, что и у отелей: реальный запрос к Overpass кэшируется
    надолго только при успехе, дальше — мгновенное перемешивание в памяти."""
    city = find_city(country_code, city_key)
    if not city:
        return []
    cache_key = (kind, country_code, city_key)
    pool = _city_poi_cache.get(cache_key)
    if not pool:
        pool = _fetch_osm_poi(kind, city["lat"], city["lon"])
        if pool:
            _city_poi_cache[cache_key] = pool
    if not pool:
        return []
    chosen = random.sample(pool, min(POI_POOL_SHOW, len(pool)))
    chosen.sort(key=_poi_sort_key)
    _shown_poi_cache[cache_key] = chosen
    return chosen

def fmt_poi_header(kind: str, city: dict, country_name: str, count: int) -> str:
    cfg = POI_KINDS[kind]
    header = f"{cfg['icon']} <b>{cfg['label']} — {city['name']}</b> ({country_name})"
    if count:
        header += f"\n<i>{count} вариантов, по категориям — жми «Показать другие» для новой подборки</i>"
    else:
        header += "\n\n😕 Не удалось получить список. Попробуй ещё раз чуть позже."
    return header

def _has_cjk(text: str) -> bool:
    return any("一" <= ch <= "鿿" for ch in text)

def _cjk_to_pinyin(text: str) -> str:
    """Транслитерация, а не перевод: китайские топонимы часто поэтичны
    ('紫气东来' дословно — 'фиолетовый воздух приходит с востока'), обычный
    перевод получается бессмысленным для навигации. Пиньинь — то, как эти
    места реально подписаны на картах и указателях, по нему их и ищут."""
    syllables = [s[0] for s in pinyin(text, style=Style.TONE, heteronym=False)]
    return " ".join(s.capitalize() for s in syllables)

def _translate_page_names(page: list[dict]) -> None:
    """OSM в некоторых регионах (например, на Хайнане) хранит названия
    иероглифами — нечитаемо для русскоязычного пользователя. Обрабатываем
    только показываемую страницу (не весь пул) и только там, где реально
    нужно — для Вьетнама/Индонезии/Египта названия и так на латинице."""
    for item in page:
        name = item.get("name", "")
        if name and _has_cjk(name):
            try:
                item["name"] = _cjk_to_pinyin(name)[:70]
            except Exception as e:
                log.warning(f"Pinyin conversion failed for {name!r}: {e}")

def fmt_poi_line(i: int, item: dict, city_en: str) -> str:
    name = html.escape(item["name"])
    icon = item.get("item_icon", "📍")
    q = urllib.parse.quote(f"{item['name']} {city_en}")
    gmaps = f"https://www.google.com/maps/search/?api=1&query={q}"
    return f"{i}. {icon} <b>{name}</b> — <a href=\"{gmaps}\">Google Maps</a>"

# ─── Курс валют ──────────────────────────────────────────────────────────────

def get_rates() -> str:
    try:
        with urllib.request.urlopen("https://cdn.jsdelivr.net/npm/@fawazahmed0/currency-api@latest/v1/currencies/usd.json", timeout=8) as r:
            data = json.loads(r.read())["usd"]
        kzt = data.get("kzt", 0)
        vnd = data.get("vnd", 0)
        idr = data.get("idr", 0)
        sgd = data.get("sgd", 0)
        eur = data.get("eur", 0)

        kzt_to_vnd = vnd / kzt * 1000 if kzt else 0
        kzt_to_idr = idr / kzt * 1000 if kzt else 0
        kzt_to_sgd = sgd / kzt * 1000 if kzt else 0

        return (
            "💱 <b>Курс валют</b>\n\n"
            f"🇺🇸 1 USD = <b>{kzt:,.0f} KZT</b>\n"
            f"🇪🇺 1 EUR = <b>{kzt/eur:,.0f} KZT</b>\n\n"
            f"🇻🇳 1000 KZT = <b>{kzt_to_vnd:,.0f} VND</b>\n"
            f"🇮🇩 1000 KZT = <b>{kzt_to_idr:,.0f} IDR</b>\n"
            f"🇸🇬 1000 KZT = <b>{kzt_to_sgd:.2f} SGD</b>\n\n"
            f"<i>Данные: fawazahmed0 Currency API</i>"
        )
    except Exception as e:
        log.warning(f"Rates error: {e}")
        return "💱 Курс валют временно недоступен."

# ─── Хранилище ────────────────────────────────────────────────────────────────
# На Cloud Run файловая система контейнера эфемерна — при каждом перезапуске
# (а он происходит часто, инстансы масштабируются до нуля) локальный файл
# обнулился бы. Поэтому если задан GCS_BUCKET, храним sea_bot_data.json в
# Google Cloud Storage; если нет (например, локальный запуск) — как раньше,
# обычным файлом рядом со скриптом.

def _empty_data():
    return {"subscribers": [], "sent_hashes": []}

def _upstash_cmd(*args):
    """Универсальный REST-эндпоинт Upstash: POST списка команд Redis в теле
    запроса — надёжнее, чем URL-путь, не ловит проблемы с экранированием
    спецсимволов внутри JSON-значения."""
    resp = requests.post(
        UPSTASH_URL, headers={"Authorization": f"Bearer {UPSTASH_TOKEN}"},
        json=list(args), timeout=8,
    )
    resp.raise_for_status()
    return resp.json().get("result")

def load_data():
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            raw = _upstash_cmd("GET", UPSTASH_KEY)
            if raw:
                return json.loads(raw)
        except Exception as e:
            log.warning(f"Upstash load failed: {e}")
        return _empty_data()
    if DATA_FILE.exists():
        with open(DATA_FILE, encoding="utf-8") as f:
            return json.load(f)
    return _empty_data()

def save_data(data):
    if UPSTASH_URL and UPSTASH_TOKEN:
        try:
            _upstash_cmd("SET", UPSTASH_KEY, json.dumps(data, ensure_ascii=False))
        except Exception as e:
            log.warning(f"Upstash save failed: {e}")
        return
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

# ─── RSS-парсинг ─────────────────────────────────────────────────────────────

def news_hash(entry):
    return hashlib.md5((entry.get("link","") + entry.get("title","")).encode()).hexdigest()

def is_relevant(entry, country=None):
    text = (entry.get("title","") + " " + entry.get("summary","") + " " + entry.get("link","")).lower()
    if country:
        place_match = any(kw in text for kw in COUNTRY_KEYWORDS.get(country, []))
        if not place_match:
            return False
        # Упоминания топонима недостаточно — например, "China" встречается и в
        # новостях про политику/бизнес. Требуем ещё и туристическую тематику.
        return any(kw in text for kw in TOURISM_TOPIC_KEYWORDS)
    return any(kw in text for kw in ALL_KEYWORDS)

def fetch_news(limit=MAX_NEWS, country=None):
    results = []
    seen = set()
    now = datetime.utcnow()
    for feed_cfg in RSS_FEEDS:
        if country and feed_cfg["country"] not in (country, "all"):
            continue
        try:
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries[:20]:
                h = news_hash(entry)
                if h in seen or is_sent(h):
                    continue
                if not is_relevant(entry, country):
                    continue
                published = ""
                pub_dt = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6])
                    if (now - pub_dt).days > 30:
                        continue
                    published = pub_dt.strftime("%d %b %Y")
                title_ru = translate_to_russian(entry.get("title", ""))
                summary_raw = re.sub(r'<[^>]+>', '', entry.get("summary", ""))[:400]
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
        except Exception as e:
            log.warning(f"Feed error {feed_cfg['name']}: {e}")
    results.sort(key=lambda x: x["pub_dt"] or datetime.min, reverse=True)
    for r in results:
        r.pop("pub_dt", None)
    return results[:limit]

def fmt_item(item, i):
    summary = item["summary"] if item["summary"] else ""
    if summary:
        dot = summary.find(". ")
        if dot > 40:
            summary = summary[:dot+1]
        summary = f"\n<i>{summary[:200]}</i>"
    date = f"  •  {item['published']}" if item["published"] else ""
    return (
        f"{item['flag']} <b>{i}. {item['title']}</b>\n"
        f"<code>{item['source']}{date}</code>"
        f"{summary}\n"
        f"<a href=\"{item['link']}\">Читать →</a>"
    )

def fmt_digest(news_list, title="Дайджест — Вьетнам, Бали, Сингапур"):
    if not news_list:
        return "😴 Свежих новостей пока нет. Загляни позже!"
    date_str = datetime.utcnow().strftime("%d %B %Y")
    header = f"🌴 <b>{title}</b>\n{date_str}\n{'─'*28}\n\n"
    items = "\n\n".join(fmt_item(n, i+1) for i, n in enumerate(news_list))
    return header + items

def fmt_city_card(country_code: str, city: dict) -> str:
    _, country_name = COUNTRIES.get(country_code, ("", ""))
    tline = local_time_str(country_code)
    weather = get_weather_one(city["name"], city["lat"], city["lon"])
    return (
        f"{city['icon']} <b>{city['name']}</b> · {country_name}\n"
        f"🕐 Сейчас там: {tline}\n"
        f"{weather}\n\n"
        "Выбери, что показать:"
    )

# ─── Навигация: состояние по chat_id ────────────────────────────────────────
# Reply-кнопки (в отличие от inline) не несут в себе контекст — приходит
# просто текст "▶️ Ещё 10" без указания, о каком городе речь. Поэтому храним
# в памяти процесса, на каком экране находится каждый чат.

_nav_state: dict[int, dict] = {}

def get_state(chat_id: int) -> dict:
    return _nav_state.get(chat_id, {"screen": "root"})

def set_state(chat_id: int, **kwargs) -> dict:
    st = _nav_state.setdefault(chat_id, {"screen": "root"})
    st.update(kwargs)
    return st

COUNTRY_LABEL_TO_CODE = {f"{flag} {name}": code for code, (flag, name) in COUNTRIES.items()}
CITY_LABEL_TO_KEY = {
    f"{c['icon']} {c['name']}": (code, c["key"])
    for code, cities in CITIES.items() for c in cities
}

# ─── Клавиатуры (все — Reply, живут в нижней панели) ──────────────────────────

def main_kb():
    return ReplyKeyboardMarkup(
        [["📍 Страны"],
         ["🌴 Все новости", "💱 Курс валют"],
         ["🔔 Подписаться", "ℹ️ О боте"]],
        resize_keyboard=True, is_persistent=True,
    )

def countries_kb():
    labels = [f"{flag} {name}" for code, (flag, name) in COUNTRIES.items()]
    rows = [labels[i:i + 2] for i in range(0, len(labels), 2)]
    rows.append(["⬅️ Назад", "🏠 Главное меню"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

def cities_kb(country_code: str):
    cities = CITIES.get(country_code, [])
    labels = [f"{c['icon']} {c['name']}" for c in cities]
    rows = [labels[i:i + 2] for i in range(0, len(labels), 2)]
    rows.append(["📰 Новости страны", "🗺️ Виза"])
    rows.append(["✈️ Рейсы из Алматы", "📶 eSIM"])
    rows.append(["⬅️ Назад", "🏠 Главное меню"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

def city_kb():
    return ReplyKeyboardMarkup(
        [["🏨 Отели"],
         ["🏛 Достопримечательности", "☕ Кафе"],
         ["📰 Новости страны", "🗺️ Виза"],
         ["✈️ Рейсы из Алматы", "📶 eSIM"],
         ["⬅️ Назад", "🏠 Главное меню"]],
        resize_keyboard=True, is_persistent=True,
    )

def current_kb(state: dict):
    """Клавиатура текущего экрана — используется, чтобы прикреплять её к
    любому ответу (курс валют, виза, рейсы и т.д.), а не только к переходам
    между экранами. Reply-клавиатура в Telegram и так не пропадает без явного
    ReplyKeyboardRemove, но явное прикрепление к каждому сообщению исключает
    любые edge-cases с её сворачиванием в некоторых клиентах."""
    screen = state.get("screen", "root")
    if screen == "countries":
        return countries_kb()
    if screen == "cities":
        return cities_kb(state.get("country"))
    if screen in ("city", "hotels", "poi"):
        return city_kb()
    return main_kb()

def hotels_kb(offset: int, total: int):
    rows = []
    next_offset = offset + HOTEL_PAGE_SIZE
    if next_offset < total:
        rows.append([f"▶️ Ещё {min(HOTEL_PAGE_SIZE, total - next_offset)}"])
    rows.append(["🔀 Показать другие 30"])
    rows.append(["⬅️ Назад", "🏠 Главное меню"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

def poi_kb(offset: int, total: int):
    rows = []
    next_offset = offset + POI_PAGE_SIZE
    if next_offset < total:
        rows.append([f"▶️ Ещё {min(POI_PAGE_SIZE, total - next_offset)}"])
    rows.append(["🔀 Показать другие 30"])
    rows.append(["⬅️ Назад", "🏠 Главное меню"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True, is_persistent=True)

# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Путешественник"
    set_state(update.effective_chat.id, screen="root", country=None, city=None)
    await update.message.reply_text(
        f"Привет, {name}! 🌏\n\n"
        "Выбери страну или раздел 👇",
        parse_mode="HTML",
        reply_markup=main_kb(),
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Команды</b>\n\n"
        "/start — главное меню\n"
        "/subscribe — подписаться на дайджест\n"
        "/unsubscribe — отписаться\n"
        "/status — статус подписки",
        parse_mode="HTML", reply_markup=main_kb(),
    )

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_user.id)
    await update.message.reply_text(
        "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.\n\nОтписаться: /unsubscribe",
        parse_mode="HTML", reply_markup=main_kb(),
    )

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_user.id)
    await update.message.reply_text("🔕 Отписан. Снова: /subscribe", reply_markup=main_kb())

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    status = "✅ Подписан" if is_subscribed(update.effective_user.id) else "🔕 Не подписан"
    await update.message.reply_text(f"📊 Статус: {status}\nПодписчиков: {len(data['subscribers'])}", parse_mode="HTML", reply_markup=main_kb())

# ─── Отели: отправка страницы (текст + фото где есть) ─────────────────────────

async def send_hotels_page(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, code: str, city_key: str,
                            city: dict, hotels: list[dict], offset: int):
    country_name = COUNTRIES.get(code, ("", ""))[1]
    if not hotels:
        await ctx.bot.send_message(chat_id, fmt_hotels_header(city, country_name, 0),
                                    parse_mode="HTML", reply_markup=hotels_kb(0, 0))
        return
    if offset == 0:
        city_photo = await asyncio.to_thread(get_city_photo, city)
        if city_photo:
            try:
                await ctx.bot.send_photo(chat_id, photo=city_photo, caption=f"{city['icon']} {city['name']}, {country_name}")
            except Exception as e:
                log.warning(f"Send city photo failed: {e}")
    page = hotels[offset:offset + HOTEL_PAGE_SIZE]
    city_en = city.get("en", city["name"])
    await asyncio.to_thread(_translate_page_names, page)
    await asyncio.to_thread(_resolve_page_photos, page, city_en)
    header = fmt_hotels_header(city, country_name, len(hotels))
    await ctx.bot.send_message(chat_id, header, parse_mode="HTML", reply_markup=hotels_kb(offset, len(hotels)))
    text_lines = []
    for j, h in enumerate(page):
        i = offset + j + 1
        line = fmt_hotel_line(i, h, city_en)
        photo = h.get("photo_bytes")
        if photo:
            try:
                await ctx.bot.send_photo(chat_id, photo=photo, caption=line, parse_mode="HTML")
                continue
            except Exception as e:
                log.warning(f"Send hotel photo failed: {e}")
        text_lines.append(line)
    if text_lines:
        await ctx.bot.send_message(chat_id, "\n".join(text_lines), parse_mode="HTML", disable_web_page_preview=True)

# ─── Достопримечательности/кафе: отправка страницы ────────────────────────────

async def send_poi_page(ctx: ContextTypes.DEFAULT_TYPE, chat_id: int, kind: str, code: str, city_key: str,
                         city: dict, items: list[dict], offset: int):
    cfg = POI_KINDS[kind]
    country_name = COUNTRIES.get(code, ("", ""))[1]
    if not items:
        await ctx.bot.send_message(chat_id, fmt_poi_header(kind, city, country_name, 0),
                                    parse_mode="HTML", reply_markup=poi_kb(0, 0))
        return
    if offset == 0:
        city_photo = await asyncio.to_thread(get_city_photo, city)
        if city_photo:
            try:
                await ctx.bot.send_photo(chat_id, photo=city_photo, caption=f"{city['icon']} {city['name']}, {country_name}")
            except Exception as e:
                log.warning(f"Send city photo failed: {e}")
    page = items[offset:offset + POI_PAGE_SIZE]
    city_en = city.get("en", city["name"])
    await asyncio.to_thread(_translate_page_names, page)
    if cfg["photos"]:
        await asyncio.to_thread(_resolve_page_photos, page, city_en)
    header = fmt_poi_header(kind, city, country_name, len(items))
    await ctx.bot.send_message(chat_id, header, parse_mode="HTML", reply_markup=poi_kb(offset, len(items)))
    text_lines = []
    last_cat = None
    for j, it in enumerate(page):
        i = offset + j + 1
        cat = it.get("cat_key")
        if cat != last_cat:
            last_cat = cat
            prefix = "\n" if text_lines else ""
            text_lines.append(f"{prefix}{it.get('group_icon','📍')} <b>{it.get('group_label','')}</b>")
        line = fmt_poi_line(i, it, city_en)
        photo = it.get("photo_bytes")
        if photo:
            try:
                await ctx.bot.send_photo(chat_id, photo=photo, caption=line, parse_mode="HTML")
                continue
            except Exception as e:
                log.warning(f"Send poi photo failed: {e}")
        text_lines.append(line)
    if text_lines:
        await ctx.bot.send_message(chat_id, "\n".join(text_lines), parse_mode="HTML", disable_web_page_preview=True)

# ─── Кнопки Reply ─────────────────────────────────────────────────────────────

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    state = get_state(chat_id)

    # ── Главное меню (доступно с любого экрана — не жать "Назад" по многу раз) ──
    if text == "🏠 Главное меню":
        set_state(chat_id, screen="root", country=None, city=None)
        await update.message.reply_text("Главное меню 👇", reply_markup=main_kb())
        return

    if text == "📍 Страны":
        set_state(chat_id, screen="countries", country=None, city=None)
        await update.message.reply_text("🌍 Выбери страну:", reply_markup=countries_kb())
        return

    if text == "🌴 Все новости":
        msg = await update.message.reply_text("⏳ Собираю и перевожу новости...")
        news = fetch_news()
        for n in news: mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)
        return

    if text == "💱 Курс валют":
        await update.message.reply_text(get_rates(), parse_mode="HTML", reply_markup=main_kb())
        return

    if text == "🔔 Подписаться":
        add_subscriber(chat_id)
        await update.message.reply_text(
            "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.",
            parse_mode="HTML", reply_markup=main_kb(),
        )
        return

    if text == "ℹ️ О боте":
        await update.message.reply_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Новости, погода, визы, отели, достопримечательности, кафе и курс валют\n"
            "по Вьетнаму, Индонезии (Бали), Сингапуру, Египту и Хайнаню (Китай).\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы\n"
            "🇷🇺 Новости переводятся на русский\n🆓 Без рекламы\n\n"
            "👤 Автор: Ивашикин Николай",
            parse_mode="HTML", reply_markup=main_kb(),
        )
        return

    # ── Выбор страны ──
    if text in COUNTRY_LABEL_TO_CODE:
        code = COUNTRY_LABEL_TO_CODE[text]
        cities = CITIES.get(code, [])
        if len(cities) == 1:
            city = cities[0]
            set_state(chat_id, screen="city", country=code, city=city["key"])
            await update.message.reply_text(fmt_city_card(code, city), parse_mode="HTML", reply_markup=city_kb())
        else:
            set_state(chat_id, screen="cities", country=code, city=None)
            flag, name = COUNTRIES[code]
            await update.message.reply_text(
                f"{flag} <b>{name}</b>\n🕐 Сейчас там: {local_time_str(code)}\n\nВыбери город:",
                parse_mode="HTML", reply_markup=cities_kb(code),
            )
        return

    # ── Выбор города ──
    if text in CITY_LABEL_TO_KEY:
        code, city_key = CITY_LABEL_TO_KEY[text]
        city = find_city(code, city_key)
        set_state(chat_id, screen="city", country=code, city=city_key)
        await update.message.reply_text(fmt_city_card(code, city), parse_mode="HTML", reply_markup=city_kb())
        return

    # ── Новости / виза / рейсы (уровень страны, доступны из cities и city) ──
    if text == "📰 Новости страны" and state.get("country"):
        code = state["country"]
        name = COUNTRIES.get(code, ("", ""))[1]
        msg = await update.message.reply_text("⏳ Собираю новости...")
        news = fetch_news(country=code)
        for n in news: mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news, title=f"Новости — {name}"), parse_mode="HTML", disable_web_page_preview=True)
        return

    if text == "🗺️ Виза" and state.get("country"):
        await update.message.reply_text(
            VISA_INFO.get(state["country"], "Информация недоступна"),
            parse_mode="HTML", reply_markup=current_kb(state),
        )
        return

    if text == "✈️ Рейсы из Алматы":
        await update.message.reply_text(FLIGHTS_INFO, parse_mode="HTML", reply_markup=current_kb(state))
        return

    if text == "📶 eSIM" and state.get("country"):
        await update.message.reply_text(
            fmt_esim_info(state["country"]),
            parse_mode="HTML", reply_markup=current_kb(state),
        )
        return

    # ── Отели ──
    if text == "🏨 Отели" and state.get("country") and state.get("city"):
        code, city_key = state["country"], state["city"]
        city = find_city(code, city_key)
        if not _city_hotel_cache.get((code, city_key)):
            await update.message.reply_text(f"⏳ Ищу отели — {city['name']}...")
        hotels = await asyncio.to_thread(shuffle_city_hotels, code, city_key)
        set_state(chat_id, screen="hotels", hotel_offset=0)
        await send_hotels_page(ctx, chat_id, code, city_key, city, hotels, 0)
        return

    if text.startswith("▶️ Ещё") and state.get("screen") == "hotels":
        code, city_key = state.get("country"), state.get("city")
        city = find_city(code, city_key)
        hotels = _shown_hotels_cache.get((code, city_key)) or []
        offset = state.get("hotel_offset", 0) + HOTEL_PAGE_SIZE
        set_state(chat_id, hotel_offset=offset)
        await send_hotels_page(ctx, chat_id, code, city_key, city, hotels, offset)
        return

    if text == "🔀 Показать другие 30" and state.get("screen") == "hotels":
        code, city_key = state.get("country"), state.get("city")
        city = find_city(code, city_key)
        if not _city_hotel_cache.get((code, city_key)):
            await update.message.reply_text(f"⏳ Ищу отели — {city['name']}...")
        hotels = await asyncio.to_thread(shuffle_city_hotels, code, city_key)
        set_state(chat_id, hotel_offset=0)
        await send_hotels_page(ctx, chat_id, code, city_key, city, hotels, 0)
        return

    # ── Достопримечательности / кафе ──
    if text == "🏛 Достопримечательности" and state.get("country") and state.get("city"):
        code, city_key, kind = state["country"], state["city"], "attractions"
        city = find_city(code, city_key)
        if not _city_poi_cache.get((kind, code, city_key)):
            await update.message.reply_text(f"⏳ Ищу достопримечательности — {city['name']}...")
        items = await asyncio.to_thread(shuffle_city_poi, kind, code, city_key)
        set_state(chat_id, screen="poi", poi_kind=kind, poi_offset=0)
        await send_poi_page(ctx, chat_id, kind, code, city_key, city, items, 0)
        return

    if text == "☕ Кафе" and state.get("country") and state.get("city"):
        code, city_key, kind = state["country"], state["city"], "cafes"
        city = find_city(code, city_key)
        if not _city_poi_cache.get((kind, code, city_key)):
            await update.message.reply_text(f"⏳ Ищу кафе и рестораны — {city['name']}...")
        items = await asyncio.to_thread(shuffle_city_poi, kind, code, city_key)
        set_state(chat_id, screen="poi", poi_kind=kind, poi_offset=0)
        await send_poi_page(ctx, chat_id, kind, code, city_key, city, items, 0)
        return

    if text.startswith("▶️ Ещё") and state.get("screen") == "poi":
        kind = state.get("poi_kind")
        code, city_key = state.get("country"), state.get("city")
        city = find_city(code, city_key)
        items = _shown_poi_cache.get((kind, code, city_key)) or []
        offset = state.get("poi_offset", 0) + POI_PAGE_SIZE
        set_state(chat_id, poi_offset=offset)
        await send_poi_page(ctx, chat_id, kind, code, city_key, city, items, offset)
        return

    if text == "🔀 Показать другие 30" and state.get("screen") == "poi":
        kind = state.get("poi_kind")
        code, city_key = state.get("country"), state.get("city")
        city = find_city(code, city_key)
        if not _city_poi_cache.get((kind, code, city_key)):
            await update.message.reply_text(f"⏳ Ищу — {city['name']}...")
        items = await asyncio.to_thread(shuffle_city_poi, kind, code, city_key)
        set_state(chat_id, poi_offset=0)
        await send_poi_page(ctx, chat_id, kind, code, city_key, city, items, 0)
        return

    # ── Назад ──
    if text == "⬅️ Назад":
        screen = state.get("screen", "root")
        if screen in ("hotels", "poi"):
            code, city_key = state.get("country"), state.get("city")
            city = find_city(code, city_key)
            set_state(chat_id, screen="city")
            await update.message.reply_text(fmt_city_card(code, city), parse_mode="HTML", reply_markup=city_kb())
        elif screen == "city":
            code = state.get("country")
            cities = CITIES.get(code, [])
            if len(cities) > 1:
                set_state(chat_id, screen="cities", city=None)
                flag, name = COUNTRIES.get(code, ("", "?"))
                await update.message.reply_text(f"{flag} <b>{name}</b>\n\nВыбери город:", parse_mode="HTML", reply_markup=cities_kb(code))
            else:
                set_state(chat_id, screen="countries", country=None, city=None)
                await update.message.reply_text("🌍 Выбери страну:", reply_markup=countries_kb())
        elif screen == "cities":
            set_state(chat_id, screen="countries", country=None, city=None)
            await update.message.reply_text("🌍 Выбери страну:", reply_markup=countries_kb())
        else:
            set_state(chat_id, screen="root", country=None, city=None)
            await update.message.reply_text("Главное меню 👇", reply_markup=main_kb())
        return

# ─── Рассылка ────────────────────────────────────────────────────────────────

async def send_daily_digest(bot):
    """Общее тело ежедневной рассылки — вызывается либо из JobQueue (локальный
    polling-режим), либо из HTTP /cron/daily, который дёргает Cloud Scheduler
    (Cloud Run-режим, где нет собственного постоянно работающего планировщика)."""
    data = load_data()
    if not data["subscribers"]: return
    news = fetch_news()
    if not news: return
    text = fmt_digest(news)
    for n in news: mark_sent(n["hash"])
    for uid in data["subscribers"]:
        try:
            await bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning(f"Send error {uid}: {e}")

async def send_daily_job(ctx: ContextTypes.DEFAULT_TYPE):
    """Обёртка под сигнатуру callback'а JobQueue (используется только в
    локальном polling-режиме)."""
    await send_daily_digest(ctx.bot)

# ─── Запуск ──────────────────────────────────────────────────────────────────
# Локально (WEBHOOK_MODE не задан и платформа не прописала свою переменную)
# бот работает как раньше через run_polling + JobQueue. На хостинге вроде
# Render — через вебхук на HTTP-сервере aiohttp, а ежедневную рассылку
# дёргает внешний бесплатный крон (cron-job.org) HTTP-запросом на
# /cron/daily — свой процесс-планировщик в контейнере, который может
# "уснуть" при простое (бесплатный тариф Render), для этого не подходит.

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    return app

def run_polling_mode():
    app = build_app()
    # JobQueue управляется тем же event loop, что и run_polling — в отличие от
    # отдельного AsyncIOScheduler, запущенного до старта polling-цикла, задания
    # здесь гарантированно срабатывают по расписанию.
    app.job_queue.run_daily(
        send_daily_job,
        time=dtime(hour=5, minute=0, tzinfo=timezone.utc),  # 10:00 Алматы
    )
    log.info("🌴 SEA Travel News Bot запущен (polling)!")
    app.run_polling(drop_pending_updates=True)

def run_webhook_mode():
    from aiohttp import web

    app = build_app()
    webhook_path = f"/webhook/{BOT_TOKEN}"

    async def handle_webhook(request: web.Request):
        data = await request.json()
        update = Update.de_json(data, app.bot)
        await app.update_queue.put(update)
        return web.Response(status=200)

    async def handle_cron(request: web.Request):
        if DAILY_CRON_SECRET and request.headers.get("X-Cron-Secret") != DAILY_CRON_SECRET:
            return web.Response(status=403, text="forbidden")
        # Сборка новостей + перевод каждой через Google Translate может занять
        # больше 30 сек — это дольше таймаута большинства внешних крон-сервисов
        # (в т.ч. cron-job.org). Поэтому отвечаем сразу, а рассылку запускаем
        # фоновой задачей — cron просто "будит" эндпоинт, не дожидаясь результата.
        asyncio.create_task(send_daily_digest(app.bot))
        return web.Response(status=200, text="started")

    async def handle_health(request: web.Request):
        return web.Response(status=200, text="ok")

    async def run():
        await app.initialize()
        await app.start()
        web_app = web.Application()
        web_app.add_routes([
            web.post(webhook_path, handle_webhook),
            web.post("/cron/daily", handle_cron),
            web.get("/", handle_health),
        ])
        runner = web.AppRunner(web_app)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        log.info(f"🌴 SEA Travel News Bot запущен (webhook, порт {PORT})!")
        await asyncio.Event().wait()  # держим процесс живым

    asyncio.run(run())

def main():
    if not BOT_TOKEN:
        print("❌ Укажи BOT_TOKEN!")
        return
    if WEBHOOK_MODE:
        run_webhook_mode()
    else:
        run_polling_mode()

if __name__ == "__main__":
    main()
