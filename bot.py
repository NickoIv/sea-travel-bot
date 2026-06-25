import asyncio
import feedparser
import logging
import os
import json
import hashlib
from datetime import datetime
from pathlib import Path
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, CallbackQueryHandler, ContextTypes
)

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
DATA_FILE = Path("sea_bot_data.json")
SCHEDULE_HOUR_UTC = 5
SCHEDULE_MINUTE_UTC = 0
MAX_NEWS = 8

RSS_FEEDS = [
    {"name": "The Thaiger", "url": "https://thethaiger.com/feed", "flag": "🇹🇭"},
    {"name": "AsiaOne Travel", "url": "https://www.asiaone.com/rss/travel.xml", "flag": "✈️"},
    {"name": "Khmer Times", "url": "https://www.khmertimeskh.com/category/tourism/feed", "flag": "🇰🇭"},
    {"name": "Jakarta Post", "url": "https://www.thejakartapost.com/travel.rss", "flag": "🇮🇩"},
    {"name": "Vietnam Travel", "url": "https://vietnam.travel/feed", "flag": "🇻🇳"},
    {"name": "Nation Thailand", "url": "https://www.nationthailand.com/rss/travel", "flag": "🌴"},
    {"name": "Travel Wire Asia", "url": "https://www.travelwireasia.com/feed/", "flag": "🌏"},
    {"name": "TTR Weekly", "url": "https://www.ttrweekly.com/site/feed/", "flag": "📰"},
]

SEA_KEYWORDS = [
    "thailand","bali","vietnam","indonesia","cambodia","myanmar","laos",
    "malaysia","singapore","philippines","southeast asia","phuket","krabi",
    "samui","hanoi","ho chi minh","angkor","lombok","komodo","beach",
    "resort","temple","diving","island","visa","flight","travel","tourism",
    "hotel","tour","destination","asia","bangkok","chiang mai","koh","dao",
]

logging.basicConfig(format="%(asctime)s | %(levelname)s | %(message)s", level=logging.INFO)
log = logging.getLogger(__name__)

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

def news_hash(entry):
    return hashlib.md5((entry.get("link","") + entry.get("title","")).encode()).hexdigest()

def is_relevant(entry):
    text = (entry.get("title","") + " " + entry.get("summary","") + " " + entry.get("link","")).lower()
    return any(kw in text for kw in SEA_KEYWORDS)

def fetch_news(limit=MAX_NEWS):
    results = []
    seen = set()
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
                if hasattr(entry, "published_parsed") and entry.published_parsed:
                    dt = datetime(*entry.published_parsed[:6])
                    published = dt.strftime("%d %b %Y")
                results.append({
                    "hash": h,
                    "title": entry.get("title", "No title")[:100],
                    "link": entry.get("link", ""),
                    "summary": entry.get("summary", "")[:300],
                    "source": feed_cfg["name"],
                    "flag": feed_cfg["flag"],
                    "published": published,
                })
                seen.add(h)
        except Exception as e:
            log.warning(f"Feed error {feed_cfg['name']}: {e}")
    return results[:limit]

def fmt_item(item, i):
    summary = item["summary"].replace("<","&lt;").replace(">","&gt;")
    if summary:
        dot = summary.find(". ")
        if dot > 40:
            summary = summary[:dot+1]
        summary = f"\n<i>{summary[:180]}</i>"
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
    header = f"🌴 <b>Дайджест ЮВА</b> — {date_str}\n{'─'*28}\n\n"
    items = "\n\n".join(fmt_item(n, i+1) for i, n in enumerate(news_list))
    return header + items + "\n\n<i>Подписан на ежедневный дайджест ✅</i>"

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    name = update.effective_user.first_name or "Путешественник"
    kb = [
        [InlineKeyboardButton("🌴 Получить новости", callback_data="get_news"),
         InlineKeyboardButton("🔔 Подписаться", callback_data="subscribe")],
        [InlineKeyboardButton("📍 Страны ЮВА", callback_data="countries"),
         InlineKeyboardButton("ℹ️ О боте", callback_data="about")],
    ]
    await update.message.reply_text(
        f"Привет, {name}! 🌏\n\n"
        "Я слежу за новостями туризма по <b>Юго-Восточной Азии</b> — "
        "Таиланд, Бали, Вьетнам, Камбоджа, Малайзия, Сингапур и другие страны.\n\n"
        "🗓 Дайджест каждое утро в <b>10:00 по Алматы</b>\n"
        "✈️ Слежу за безвизом, рейсами, курортами, ценами\n\n"
        "Выбери действие:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(kb)
    )

async def cmd_news(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    msg = await update.message.reply_text("⏳ Собираю свежие новости...")
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
        f"📊 <b>Статус</b>\n\nТвой статус: {status}\nПодписчиков всего: {len(data['subscribers'])}\n"
        f"Источников RSS: {len(RSS_FEEDS)}\nРассылка: 10:00 Алматы",
        parse_mode="HTML"
    )

async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📖 <b>Команды</b>\n\n/start — главное меню\n/news — новости прямо сейчас\n"
        "/subscribe — подписаться\n/unsubscribe — отписаться\n/status — статус\n/help — справка",
        parse_mode="HTML"
    )

async def cb(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id

    if q.data == "get_news":
        await q.edit_message_text("⏳ Собираю новости...")
        news = fetch_news()
        for n in news: mark_sent(n["hash"])
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
        await q.edit_message_text(
            "🗺 <b>Страны ЮВА</b>\n\n🇹🇭 Таиланд — Пхукет, Краби, Самуи, Бангкок\n"
            "🇻🇳 Вьетнам — Ханой, Хошимин, Дананг\n🇮🇩 Индонезия — Бали, Ломбок, Комодо\n"
            "🇰🇭 Камбоджа — Ангкор, Сиемреап\n🇲🇾 Малайзия — КЛ, Лангкави, Борнео\n"
            "🇸🇬 Сингапур\n🇵🇭 Филиппины — Боракай, Палаван\n🇲🇲 Мьянма\n🇱🇦 Лаос",
            parse_mode="HTML", reply_markup=InlineKeyboardMarkup(kb))

    elif q.data == "about":
        kb = [[InlineKeyboardButton("← Назад", callback_data="back")]]
        await q.edit_message_text(
            "🌴 <b>SEA Travel News Bot</b>\n\n"
            "Собирает новости из 8 RSS-источников, фильтрует по ключевым словам, удаляет дубли.\n\n"
            "📅 Дайджест ежедневно в 10:00 по Алматы\n🆓 Бесплатно и без рекламы",
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

def main():
    if not BOT_TOKEN:
        print("❌ Укажи BOT_TOKEN в переменных окружения Railway!")
        return
    app = Application.builder().token(BOT_TOKEN).build()
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("news", cmd_news))
    app.add_handler(CommandHandler("subscribe", cmd_subscribe))
    app.add_handler(CommandHandler("unsubscribe", cmd_unsubscribe))
    app.add_handler(CommandHandler("status", cmd_status))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CallbackQueryHandler(cb))
    scheduler = AsyncIOScheduler()
    scheduler.add_job(lambda: asyncio.create_task(send_daily(app)),
        trigger="cron", hour=SCHEDULE_HOUR_UTC, minute=SCHEDULE_MINUTE_UTC)
    scheduler.start()
    log.info("🌴 SEA Travel News Bot запущен!")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()
