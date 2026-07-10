import asyncio
import feedparser
import logging
import os
import json
import hashlib
import urllib.request
import urllib.parse
import re
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, ReplyKeyboardMarkup, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATA_FILE = Path("sea_bot_data.json")
SCHEDULE_HOUR_UTC = 5
SCHEDULE_MINUTE_UTC = 0
MAX_NEWS = 8

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
    {"name": "Australian Traveller", "url": "https://www.australiantraveller.com/feed/", "flag": "🇦🇺", "country": "au"},
    {"name": "Egypt Independent", "url": "https://egyptindependent.com/feed/", "flag": "🇪🇬", "country": "eg"},
]

COUNTRY_KEYWORDS = {
    "vn": ["vietnam","hanoi","ho chi minh","da nang","danang","hoi an","nha trang","halong","sapa","phu quoc","hue","saigon","viet"],
    "id": ["indonesia","bali","jakarta","lombok","komodo","ubud","denpasar","seminyak","canggu","yogyakarta","java"],
    "sg": ["singapore","sentosa","changi"],
    "au": ["australia","sydney","melbourne","brisbane","perth","adelaide","gold coast","cairns","great barrier reef","uluru","tasmania","canberra","outback"],
    "eg": ["egypt","cairo","luxor","aswan","hurghada","sharm el sheikh","alexandria","giza","pyramids","red sea","nile"],
}
ALL_KEYWORDS = COUNTRY_KEYWORDS["vn"] + COUNTRY_KEYWORDS["id"] + COUNTRY_KEYWORDS["sg"] + COUNTRY_KEYWORDS["au"] + COUNTRY_KEYWORDS["eg"] + ["beach","resort","diving","island","visa","flight","travel","tourism","hotel","tour"]

# Таймзоны для отображения локального времени под каждой страной
TIMEZONES = {
    "vn": "Asia/Ho_Chi_Minh",
    "id": "Asia/Makassar",
    "sg": "Asia/Singapore",
    "au": "Australia/Sydney",
    "eg": "Africa/Cairo",
}

def local_time_str(country_code: str) -> str:
    tz = TIMEZONES.get(country_code)
    if not tz:
        return ""
    return datetime.now(ZoneInfo(tz)).strftime("%H:%M")

# ─── Статичные данные ─────────────────────────────────────────────────────────

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
    "au": (
        "🇦🇺 <b>Австралия — условия въезда</b>\n\n"
        "🔴 <b>Казахстан/Россия</b>: виза обязательна, безвизового режима и eVisitor нет\n\n"
        "📋 <b>Visitor visa (subclass 600)</b>:\n"
        "• Оформление: онлайн через ImmiAccount (immi.homeaffairs.gov.au)\n"
        "• Стоимость: от AU$150\n"
        "• Срок рассмотрения: от 2 до 4+ недель — подавать заранее\n\n"
        "📄 Нужны: загранпаспорт, подтверждение финансовой состоятельности, бронь обратного билета"
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
}

HOTELS_INFO = {
    "vn": (
        "🇻🇳 <b>Отели Вьетнама</b>\n\n"
        "🏙 <b>Ханой</b>\n"
        "• Sofitel Legend Metropole ⭐⭐⭐⭐⭐\n"
        "• Movenpick Hotel ⭐⭐⭐⭐⭐\n"
        "• La Siesta Premium ⭐⭐⭐⭐\n\n"
        "🌊 <b>Дананг / Хойан</b>\n"
        "• Intercontinental Sun Peninsula ⭐⭐⭐⭐⭐\n"
        "• Vinpearl Resort & Spa ⭐⭐⭐⭐⭐\n"
        "• Anantara Hoi An ⭐⭐⭐⭐⭐\n\n"
        "🏝 <b>Фукуок</b>\n"
        "• JW Marriott Phu Quoc ⭐⭐⭐⭐⭐\n"
        "• Premier Residences Phu Quoc ⭐⭐⭐⭐⭐\n\n"
        "🔍 Бронирование: booking.com / agoda.com"
    ),
    "id": (
        "🇮🇩 <b>Отели Бали / Индонезии</b>\n\n"
        "🌺 <b>Семиньяк / Кангу (тусовочный)</b>\n"
        "• W Bali Seminyak ⭐⭐⭐⭐⭐\n"
        "• The Layar Private Villas ⭐⭐⭐⭐⭐\n"
        "• Katamama Boutique ⭐⭐⭐⭐⭐\n\n"
        "🌿 <b>Убуд (культура/природа)</b>\n"
        "• Four Seasons Sayan ⭐⭐⭐⭐⭐\n"
        "• Komaneka at Bisma ⭐⭐⭐⭐⭐\n"
        "• Alaya Resort ⭐⭐⭐⭐\n\n"
        "🏖 <b>Нуса-Дуа (пляж/семья)</b>\n"
        "• Mulia Resort Nusa Dua ⭐⭐⭐⭐⭐\n"
        "• Conrad Bali ⭐⭐⭐⭐⭐\n\n"
        "🔍 Бронирование: booking.com / agoda.com"
    ),
    "sg": (
        "🇸🇬 <b>Отели Сингапура</b>\n\n"
        "🌆 <b>Центр / Marina Bay</b>\n"
        "• Marina Bay Sands ⭐⭐⭐⭐⭐ (бассейн на крыше!)\n"
        "• The Fullerton Hotel ⭐⭐⭐⭐⭐\n"
        "• Raffles Singapore ⭐⭐⭐⭐⭐\n\n"
        "🌳 <b>Orchard Road (шопинг)</b>\n"
        "• Four Seasons Singapore ⭐⭐⭐⭐⭐\n"
        "• St. Regis Singapore ⭐⭐⭐⭐⭐\n\n"
        "🏝 <b>Сентоза (пляж/Universal)</b>\n"
        "• Capella Singapore ⭐⭐⭐⭐⭐\n"
        "• Sofitel Singapore Sentosa ⭐⭐⭐⭐⭐\n\n"
        "🔍 Бронирование: booking.com / agoda.com"
    ),
    "au": (
        "🇦🇺 <b>Отели Австралии</b>\n\n"
        "🌆 <b>Сидней</b>\n"
        "• Park Hyatt Sydney ⭐⭐⭐⭐⭐\n"
        "• Shangri-La Sydney ⭐⭐⭐⭐⭐\n"
        "• Ovolo Woolloomooloo ⭐⭐⭐⭐\n\n"
        "🎭 <b>Мельбурн</b>\n"
        "• Crown Towers Melbourne ⭐⭐⭐⭐⭐\n"
        "• The Langham Melbourne ⭐⭐⭐⭐⭐\n\n"
        "🏖 <b>Голд-Кост / Кэрнс</b>\n"
        "• QT Gold Coast ⭐⭐⭐⭐⭐\n"
        "• Pullman Reef Hotel Casino (Cairns) ⭐⭐⭐⭐⭐\n\n"
        "🔍 Бронирование: booking.com / agoda.com"
    ),
    "eg": (
        "🇪🇬 <b>Отели Египта</b>\n\n"
        "🏖 <b>Хургада</b>\n"
        "• Steigenberger Al Dau Beach ⭐⭐⭐⭐⭐\n"
        "• Baron Palace Sahl Hasheesh ⭐⭐⭐⭐⭐\n\n"
        "🤿 <b>Шарм-эль-Шейх</b>\n"
        "• Rixos Sharm El Sheikh ⭐⭐⭐⭐⭐\n"
        "• Four Seasons Sharm El Sheikh ⭐⭐⭐⭐⭐\n\n"
        "🏛 <b>Каир</b>\n"
        "• Four Seasons Cairo at Nile Plaza ⭐⭐⭐⭐⭐\n"
        "• Kempinski Nile Hotel ⭐⭐⭐⭐⭐\n\n"
        "🔍 Бронирование: booking.com / agoda.com"
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
    "🇦🇺 <b>Алматы → Австралия (SYD/MEL)</b>\n"
    "• Прямых рейсов нет, обычно через Дубай, Сингапур или Гуанчжоу\n"
    "• В среднем: от $900–1400 туда-обратно\n\n"
    "🇪🇬 <b>Алматы → Египет (HRG/SSH)</b>\n"
    "• Чартерные и прямые рейсы в Хургаду и Шарм-эль-Шейх (сезонно)\n"
    "• В среднем: от $500–800 туда-обратно\n\n"
    "🔍 Поиск билетов: aviasales.ru / skyscanner.com / google.com/flights"
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

WEATHER_CITIES = {
    "vn": [("Ханой", 21.0285, 105.8542), ("Дананг", 16.0544, 108.2022), ("Хойан", 15.8801, 108.3380), ("Нячанг", 12.2388, 109.1967), ("Фукуок", 10.2899, 103.9840)],
    "id": [("Бали/Денпасар", -8.6705, 115.2126), ("Убуд", -8.5069, 115.2625), ("Ломбок", -8.6524, 116.3240)],
    "sg": [("Сингапур", 1.3521, 103.8198)],
    "au": [("Сидней", -33.8688, 151.2093), ("Мельбурн", -37.8136, 144.9631), ("Брисбен", -27.4698, 153.0251), ("Голд-Кост", -28.0167, 153.4000)],
    "eg": [("Каир", 30.0444, 31.2357), ("Хургада", 27.2579, 33.8116), ("Шарм-эль-Шейх", 27.9158, 34.3300), ("Луксор", 25.6872, 32.6396)],
}

WEATHER_ICONS = {"0":"☀️","1":"🌤","2":"⛅","3":"☁️","45":"🌫","48":"🌫","51":"🌦","61":"🌧","71":"❄️","80":"🌦","95":"⛈"}

def get_weather(country: str) -> str:
    cities = WEATHER_CITIES.get(country, [])
    lines = []
    for name, lat, lon in cities:
        try:
            url = (f"https://api.open-meteo.com/v1/forecast?latitude={lat}&longitude={lon}"
                   f"&current=temperature_2m,weathercode&timezone=auto")
            with urllib.request.urlopen(url, timeout=8) as r:
                d = json.loads(r.read())
            temp = round(d["current"]["temperature_2m"])
            code = str(d["current"]["weathercode"])
            icon = WEATHER_ICONS.get(code, "🌡")
            lines.append(f"{icon} {name}: <b>{temp}°C</b>")
        except:
            lines.append(f"🌡 {name}: нет данных")
    return "\n".join(lines)

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

# ─── RSS-парсинг ─────────────────────────────────────────────────────────────

def news_hash(entry):
    return hashlib.md5((entry.get("link","") + entry.get("title","")).encode()).hexdigest()

def is_relevant(entry, country=None):
    text = (entry.get("title","") + " " + entry.get("summary","") + " " + entry.get("link","")).lower()
    if country:
        return any(kw in text for kw in COUNTRY_KEYWORDS.get(country, []))
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

# ─── Клавиатуры ──────────────────────────────────────────────────────────────

def main_kb():
    return ReplyKeyboardMarkup(
        [["🇻🇳 Вьетнам", "🇮🇩 Индонезия", "🇸🇬 Сингапур"],
         ["🇦🇺 Австралия", "🇪🇬 Египет"],
         ["🌴 Все новости", "💱 Курс валют"],
         ["🔔 Подписаться", "ℹ️ О боте"]],
        resize_keyboard=True, is_persistent=True,
    )

def country_kb(country_code: str, country_name: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📰 Новости", callback_data=f"news_{country_code}"),
         InlineKeyboardButton("☀️ Погода", callback_data=f"weather_{country_code}")],
        [InlineKeyboardButton("🗺️ Виза", callback_data=f"visa_{country_code}"),
         InlineKeyboardButton("🏨 Отели", callback_data=f"hotels_{country_code}")],
        [InlineKeyboardButton("✈️ Рейсы из Алматы", callback_data="flights")],
    ])

# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Путешественник"
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
        parse_mode="HTML"
    )

async def cmd_subscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    add_subscriber(update.effective_user.id)
    await update.message.reply_text(
        "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.\n\nОтписаться: /unsubscribe",
        parse_mode="HTML"
    )

async def cmd_unsubscribe(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    remove_subscriber(update.effective_user.id)
    await update.message.reply_text("🔕 Отписан. Снова: /subscribe")

async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = load_data()
    status = "✅ Подписан" if is_subscribed(update.effective_user.id) else "🔕 Не подписан"
    await update.message.reply_text(f"📊 Статус: {status}\nПодписчиков: {len(data['subscribers'])}", parse_mode="HTML")

# ─── Кнопки Reply ─────────────────────────────────────────────────────────────

COUNTRY_MAP = {
    "🇻🇳 Вьетнам": ("vn", "Вьетнам"),
    "🇮🇩 Индонезия": ("id", "Индонезия"),
    "🇸🇬 Сингапур": ("sg", "Сингапур"),
    "🇦🇺 Австралия": ("au", "Австралия"),
    "🇪🇬 Египет": ("eg", "Египет"),
}

async def handle_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id

    if text in COUNTRY_MAP:
        code, name = COUNTRY_MAP[text]
        flag = text.split()[0]
        time_line = f"\n🕐 Сейчас там: {local_time_str(code)}" if local_time_str(code) else ""
        await update.message.reply_text(
            f"{flag} <b>{name}</b>{time_line}\n\nВыбери раздел:",
            parse_mode="HTML",
            reply_markup=country_kb(code, name),
        )

    elif text == "🌴 Все новости":
        msg = await update.message.reply_text("⏳ Собираю и перевожу новости...")
        news = fetch_news()
        for n in news: mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)

    elif text == "💱 Курс валют":
        await update.message.reply_text(get_rates(), parse_mode="HTML")

    elif text == "🔔 Подписаться":
        add_subscriber(chat_id)
        await update.message.reply_text(
            "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.",
            parse_mode="HTML"
        )

    elif text == "ℹ️ О боте":
        await update.message.reply_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Новости, погода, визы, отели и курс валют\n"
            "по Вьетнаму, Индонезии (Бали) и Сингапуру.\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы\n"
            "🇷🇺 Новости переводятся на русский\n🆓 Без рекламы",
            parse_mode="HTML"
        )

# ─── Callback inline ──────────────────────────────────────────────────────────

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    data = q.data

    if data.startswith("news_"):
        country = data[5:]
        names = {"vn": "Вьетнам", "id": "Индонезия/Бали", "sg": "Сингапур", "au": "Австралия", "eg": "Египет"}
        await q.edit_message_text("⏳ Собираю новости...")
        news = fetch_news(country=country)
        for n in news: mark_sent(n["hash"])
        text = fmt_digest(news, title=f"Новости — {names.get(country,'')}")
        await q.edit_message_text(text, parse_mode="HTML", disable_web_page_preview=True)

    elif data.startswith("weather_"):
        country = data[8:]
        names = {"vn": "Вьетнам", "id": "Индонезия/Бали", "sg": "Сингапур", "au": "Австралия", "eg": "Египет"}
        w = get_weather(country)
        await q.edit_message_text(
            f"☀️ <b>Погода — {names.get(country,'')}</b>\n\n{w}",
            parse_mode="HTML"
        )

    elif data.startswith("visa_"):
        country = data[5:]
        await q.edit_message_text(VISA_INFO.get(country, "Информация недоступна"), parse_mode="HTML")

    elif data.startswith("hotels_"):
        country = data[7:]
        await q.edit_message_text(HOTELS_INFO.get(country, "Информация недоступна"), parse_mode="HTML")

    elif data == "flights":
        await q.edit_message_text(FLIGHTS_INFO, parse_mode="HTML")

# ─── Рассылка ────────────────────────────────────────────────────────────────

async def send_daily(app):
    data = load_data()
    if not data["subscribers"]: return
    news = fetch_news()
    if not news: return
    text = fmt_digest(news)
    for n in news: mark_sent(n["hash"])
    for uid in data["subscribers"]:
        try:
            await app.bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning(f"Send error {uid}: {e}")

# ─── Запуск ──────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        print("❌ Укажи BOT_TOKEN!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(send_daily(app)),
        trigger="cron", hour=SCHEDULE_HOUR_UTC, minute=SCHEDULE_MINUTE_UTC)
    scheduler.start()
    log.info("🌴 SEA Travel News Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
