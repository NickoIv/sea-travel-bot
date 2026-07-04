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

from apscheduler.schedulers.asyncio import AsyncIOScheduler
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

def translate_to_russian(text: str) -> str:
    """Переводит текст на русский через бесплатный Google Translate API."""
    if not text or not text.strip():
        return text
    try:
        text = text[:500]
        params = urllib.parse.urlencode({
            "client": "gtx",
            "sl": "auto",
            "tl": "ru",
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


# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Путешественник"
    reply_kb = ReplyKeyboardMarkup(
        [["🌴 Все новости", "🛂 Визы и правила"],
         ["🇻🇳 Вьетнам", "🇮🇩 Индонезия", "🇸🇬 Сингапур"],
         ["🔔 Подписаться", "💱 Курс тенге"],
         ["📍 Страны", "ℹ️ О боте"],
         ["🏨 Отели"]],
        resize_keyboard=True,
        is_persistent=True,
    )
    await update.message.reply_text(
        f"Привет, {name}! 🌏\n\n"
        "Я слежу за новостями туризма по <b>Вьетнаму, Индонезии (Бали) и Сингапуру</b>.\n\n"
        "🗓 Дайджест каждое утро в <b>10:00 по Алматы</b>\n"
        "🇷🇺 Все новости переводятся на русский язык\n"
        "🛂 Отдельно фильтрую визовые новости и изменения правил въезда\n"
        "💱 Показываю курс тенге к донгу/рупии/сингапурскому доллару\n"
        "🏨 А ещё подберу топ отелей в Нячанге, Дананге и Хойане\n\n"
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

    elif text == "🛂 Визы и правила":
        msg = await update.message.reply_text("⏳ Ищу визовые новости и изменения правил въезда...")
        news = await asyncio.to_thread(fetch_news, MAX_NEWS, VISA_KEYWORDS)
        for n in news:
            mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)

    elif text in COUNTRY_LABELS.values():
        country_key = next(k for k, v in COUNTRY_LABELS.items() if v == text)
        msg = await update.message.reply_text(f"⏳ Собираю новости — {text}...")
        news = await asyncio.to_thread(fetch_news, MAX_NEWS, COUNTRY_KEYWORDS[country_key])
        for n in news:
            mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)

    elif text == "💱 Курс тенге":
        msg = await update.message.reply_text("⏳ Получаю курс валют...")
        data = await asyncio.to_thread(fetch_kzt_rates)
        await msg.edit_text(fmt_exchange(data), parse_mode="HTML")

    elif text == "🔔 Подписаться":
        add_subscriber(chat_id)
        await update.message.reply_text(
            "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.\n\nОтписаться: /unsubscribe",
            parse_mode="HTML"
        )

    elif text == "📍 Страны":
        await update.message.reply_text(countries_text(), parse_mode="HTML")

    elif text == "ℹ️ О боте":
        await update.message.reply_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Собирает новости о Вьетнаме, Индонезии (Бали) и Сингапуре, "
            "переводит на русский, удаляет дубли.\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы\n"
            "🇷🇺 Автоперевод на русский\n🆓 Без рекламы",
            parse_mode="HTML"
        )

    elif text == "🏨 Отели":
        await hotels.show_city_menu(update, ctx)


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
        "📖 <b>Команды</b>\n\n/start — главное меню\n/news — новости прямо сейчас\n"
        "/subscribe — подписаться\n/unsubscribe — отписаться\n/status — статус\n"
        "/refresh_hotels — обновить данные по отелям прямо сейчас\n/help — справка\n\n"
        "Кнопками внизу можно фильтровать новости по стране, отдельно смотреть "
        "визовые изменения (🛂), и проверить курс тенге (💱).",
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
    text = fmt_digest(news)
    for n in news:
        mark_sent(n["hash"])
    for uid in data["subscribers"]:
        try:
            await app.bot.send_message(uid, text, parse_mode="HTML", disable_web_page_preview=True)
            await asyncio.sleep(0.05)
        except Exception as e:
            log.warning(f"Send error {uid}: {e}")


# ─── Запуск ──────────────────────────────────────────────────────────────────

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

    from telegram.ext import MessageHandler, filters
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_reply_buttons))

    hotels.register(app)  # добавляет /refresh_hotels, CallbackQueryHandler("^city:"), суточный cron

    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(send_daily(app)),
                       trigger="cron", hour=SCHEDULE_HOUR_UTC, minute=SCHEDULE_MINUTE_UTC)
    scheduler.start()

    log.info("🌴 SEA Travel News Bot (+ 🏨 Отели) запущен!")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
