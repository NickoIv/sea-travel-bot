import asyncio
import feedparser
import logging
import os
import json
import hashlib
import urllib.request
import urllib.parse
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes,
    MessageHandler, filters
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATA_FILE = Path("sea_bot_data.json")
SCHEDULE_HOUR_UTC = 5
SCHEDULE_MINUTE_UTC = 0
MAX_NEWS = 8

RSS_FEEDS = [
    {"name": "Vietnam Travel", "url": "https://vietnam.travel/feed", "flag": "🇻🇳"},
    {"name": "Vietnam Plus", "url": "https://en.vietnamplus.vn/rss/travel.rss", "flag": "🇻🇳"},
    {"name": "Jakarta Post", "url": "https://www.thejakartapost.com/travel.rss", "flag": "🇮🇩"},
    {"name": "Coconuts Bali", "url": "https://coconuts.co/bali/feed/", "flag": "🌴"},
    {"name": "AsiaOne Travel", "url": "https://www.asiaone.com/rss/travel.xml", "flag": "✈️"},
    {"name": "TTR Weekly", "url": "https://www.ttrweekly.com/site/feed/", "flag": "📰"},
]

SEA_KEYWORDS = [
    "vietnam","hanoi","ho chi minh","da nang","danang","hoi an","nha trang",
    "halong","sapa","phu quoc","hue","saigon",
    "indonesia","bali","jakarta","lombok","komodo","ubud","denpasar",
    "seminyak","canggu","yogyakarta","surabaya","sumatra","java island",
    "singapore","sentosa","changi",
    "beach","resort","temple","diving","island","visa","flight","travel",
    "tourism","hotel","tour","destination",
]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)


# ─── Перевод — несколько методов с fallback ──────────────────────────────────

def translate_method1(text: str) -> str:
    """Google Translate неофициальный API v1"""
    params = urllib.parse.urlencode({
        "client": "gtx", "sl": "auto", "tl": "ru", "dt": "t", "q": text,
    })
    url = f"https://translate.googleapis.com/translate_a/single?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = "".join(block[0] for block in data[0] if block[0])
    return result.strip()


def translate_method2(text: str) -> str:
    """Google Translate неофициальный API v2"""
    params = urllib.parse.urlencode({
        "client": "dict-chrome-ex", "sl": "auto", "tl": "ru", "dt": "t", "q": text,
    })
    url = f"https://translate.googleapis.com/translate_a/single?{params}"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = "".join(block[0] for block in data[0] if block[0])
    return result.strip()


def translate_method3(text: str) -> str:
    """MyMemory бесплатный API"""
    params = urllib.parse.urlencode({"q": text, "langpair": "en|ru"})
    url = f"https://api.mymemory.translated.net/get?{params}"
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    with urllib.request.urlopen(req, timeout=8) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    result = data.get("responseData", {}).get("translatedText", "")
    if result and result.upper() != text.upper():
        return result.strip()
    raise Exception("MyMemory returned same text")


def translate_to_russian(text: str) -> str:
    if not text or not text.strip():
        return text
    text = text[:500]
    for method in [translate_method1, translate_method2, translate_method3]:
        try:
            result = method(text)
            if result and result.strip():
                return result
        except Exception as e:
            log.warning(f"Translate method failed: {e}")
    log.warning("All translation methods failed, returning original")
    return text


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

def is_relevant(entry):
    text = (entry.get("title","") + " " + entry.get("summary","") + " " + entry.get("link","")).lower()
    return any(kw in text for kw in SEA_KEYWORDS)

def fetch_news(limit=MAX_NEWS):
    results = []
    seen = set()
    now = datetime.utcnow()
    max_age_days = 30

    for feed_cfg in RSS_FEEDS:
        try:
            feed = feedparser.parse(feed_cfg["url"])
            for entry in feed.entries[:15]:
                h = news_hash(entry)
                if h in seen or is_sent(h):
                    continue
                if not is_relevant(entry):
                    continue

                published = ""
                pub_dt = None
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    pub_dt = datetime(*entry.published_parsed[:6])
                    if (now - pub_dt).days > max_age_days:
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
        except Exception as e:
            log.warning(f"Feed error {feed_cfg['name']}: {e}")

    results.sort(key=lambda x: x["pub_dt"] or datetime.min, reverse=True)
    for r in results:
        r.pop("pub_dt", None)
    return results[:limit]


# ─── Форматирование ──────────────────────────────────────────────────────────

def fmt_item(item, i):
    summary = item["summary"].replace("<","&lt;").replace(">","&gt;") if item["summary"] else ""
    if summary:
        # Убираем HTML теги из summary
        import re
        summary = re.sub(r'<[^>]+>', '', summary)
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

def fmt_digest(news_list):
    if not news_list:
        return "😴 Новых новостей пока нет. Загляни позже!"
    date_str = datetime.utcnow().strftime("%d %B %Y")
    header = f"🌴 <b>Дайджест — Вьетнам, Бали, Сингапур</b>\n{date_str}\n{'─'*28}\n\n"
    items = "\n\n".join(fmt_item(n, i+1) for i, n in enumerate(news_list))
    return header + items + "\n\n<i>Подписан на ежедневный дайджест ✅</i>"


# ─── Клавиатура ──────────────────────────────────────────────────────────────

def persistent_kb():
    return ReplyKeyboardMarkup(
        [["🌴 Получить новости", "🔔 Подписаться"],
         ["📍 Страны", "ℹ️ О боте"]],
        resize_keyboard=True,
        is_persistent=True,
    )


# ─── Команды ─────────────────────────────────────────────────────────────────

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Путешественник"
    await update.message.reply_text(
        f"Привет, {name}! 🌏\n\n"
        "Я слежу за новостями туризма по <b>Вьетнаму, Индонезии (Бали) и Сингапуру</b>.\n\n"
        "🗓 Дайджест каждое утро в <b>10:00 по Алматы</b>\n"
        "🇷🇺 Все новости переводятся на русский язык\n\n"
        "Кнопки внизу всегда под рукой 👇",
        parse_mode="HTML",
        reply_markup=persistent_kb(),
    )

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Собираю и перевожу новости...")
    news = fetch_news()
    for n in news: mark_sent(n["hash"])
    await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)

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
        f"📊 <b>Статус</b>\n\nТвой статус: {status}\nПодписчиков: {len(data['subscribers'])}\n"
        f"Источников RSS: {len(RSS_FEEDS)}\nРассылка: 10:00 Алматы",
        parse_mode="HTML"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Команды</b>\n\n/start — главное меню\n/news — новости прямо сейчас\n"
        "/subscribe — подписаться\n/unsubscribe — отписаться\n/status — статус\n/help — справка",
        parse_mode="HTML"
    )

async def cmd_reply_buttons(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    chat_id = update.effective_chat.id
    if text == "🌴 Получить новости":
        msg = await update.message.reply_text("⏳ Собираю и перевожу новости...")
        news = fetch_news()
        for n in news: mark_sent(n["hash"])
        await msg.edit_text(fmt_digest(news), parse_mode="HTML", disable_web_page_preview=True)
    elif text == "🔔 Подписаться":
        add_subscriber(chat_id)
        await update.message.reply_text(
            "✅ Подписка оформлена!\nДайджест каждое утро в <b>10:00 по Алматы</b>.\n\nОтписаться: /unsubscribe",
            parse_mode="HTML"
        )
    elif text == "📍 Страны":
        await update.message.reply_text(
            "🗺 <b>Страны, за которыми слежу</b>\n\n"
            "🇻🇳 Вьетнам — Ханой, Хошимин, Дананг, Хойан, Нячанг, Фукуок\n"
            "🇮🇩 Индонезия — Бали, Ломбок, Комодо, Джакарта\n"
            "🇸🇬 Сингапур — Сентоза, центр города",
            parse_mode="HTML"
        )
    elif text == "ℹ️ О боте":
        await update.message.reply_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Собирает новости о Вьетнаме, Индонезии (Бали) и Сингапуре, "
            "переводит на русский, удаляет дубли, показывает только свежие (до 30 дней).\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы\n"
            "🇷🇺 Автоперевод на русский\n🆓 Без рекламы",
            parse_mode="HTML"
        )


# ─── Callback inline-кнопки ──────────────────────────────────────────────────

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if q.data == "subscribe":
        add_subscriber(uid)
        await q.edit_message_text(
            "✅ <b>Подписка оформлена!</b>\n\nДайджест каждое утро в <b>10:00 по Алматы</b>.",
            parse_mode="HTML")


# ─── Ежедневная рассылка ─────────────────────────────────────────────────────

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
        print("❌ Укажи BOT_TOKEN в переменных окружения!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(cb))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, cmd_reply_buttons))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(send_daily(app)),
        trigger="cron", hour=SCHEDULE_HOUR_UTC, minute=SCHEDULE_MINUTE_UTC)
    scheduler.start()
    log.info("🌴 SEA Travel News Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
