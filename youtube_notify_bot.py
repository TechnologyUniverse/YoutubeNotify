import os
import sys
import json
import logging
import asyncio
import feedparser

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

LOG_FILE = 'bot.log'
STATE_FILE = 'state.json'
ANTISPAM_DELAY = 120

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding='utf-8'),
        logging.StreamHandler(sys.stdout)
    ]
)

logging.getLogger('apscheduler.scheduler').setLevel(logging.WARNING)
logger = logging.getLogger(__name__)

load_dotenv()

TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')

YOUTUBE_CHANNEL_IDS = [
    cid.strip()
    for cid in os.getenv('YOUTUBE_CHANNEL_IDS', '').split(',')
    if cid.strip()
]

TG_CHANNELS = {
    pair.split(':')[0]: pair.split(':')[1]
    for pair in os.getenv('TG_CHANNELS', '').split(',')
    if ':' in pair
}

CHANNEL_NAMES = {
    "UC2qbVIfOigWXrUoQjQjaRVw": "Technology Universe",
    "UCK-x6Di4CT74zDD1JBo5vsA": "Technology Universe Podcast"
}

if not TELEGRAM_TOKEN:
    logger.error("❌ TELEGRAM_TOKEN не задан")
    sys.exit(1)

if not YOUTUBE_CHANNEL_IDS:
    logger.error("❌ YOUTUBE_CHANNEL_IDS не задан")
    sys.exit(1)

if not TG_CHANNELS:
    logger.error("❌ TG_CHANNELS не задан")
    sys.exit(1)


def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}


def save_state(state):
    with open(STATE_FILE, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def fetch_feed(channel_id: str):
    url = f'https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}'
    return feedparser.parse(url)


async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    state = load_state()

    for channel_id in YOUTUBE_CHANNEL_IDS:
        feed = fetch_feed(channel_id)
        if not feed.entries:
            continue

        # Самое новое видео в RSS (YouTube всегда кладёт его первым)
        latest = feed.entries[0]
        latest_video_id = latest.yt_videoid

        # 🔹 Первый запуск — ничего не отправляем, только запоминаем
        if channel_id not in state:
            state[channel_id] = latest_video_id
            save_state(state)
            logger.info(
                f"Первый запуск для канала {channel_id}. "
                f"Видео зафиксировано, без отправки."
            )
            continue

        # 🔹 Если нового видео нет — выходим
        if state.get(channel_id) == latest_video_id:
            continue

        title = latest.title
        link = latest.link

        channel_name = CHANNEL_NAMES.get(channel_id, "YouTube")

        title_lower = title.lower()

        is_live = False
        live_key = f"live_{latest_video_id}"

        if ('live' in title_lower or 'стрим' in title_lower) and state.get(live_key) != latest_video_id:
            is_live = True
            state[live_key] = latest_video_id
            save_state(state)

        is_premiere = False

        if 'премьера' in title_lower or 'premiere' in title_lower:
            is_premiere = True

        # 🔹 ГИБРИДНЫЙ фильтр Shorts
        title_lower = title.lower()
        link_lower = link.lower()

        is_short = False
        reasons = []

        if '#shorts' in title_lower:
            is_short = True
            reasons.append('#shorts in title')

        if '/shorts/' in link_lower:
            is_short = True
            reasons.append('/shorts/ in link')

        if is_short:
            logger.warning(
                f"possible_short | канал={channel_id} | видео={latest_video_id} | "
                f"причины={', '.join(reasons)} | {title}"
            )
            state[channel_id] = latest_video_id
            save_state(state)
            continue

        tg_channel = TG_CHANNELS.get(
            channel_id,
            list(TG_CHANNELS.values())[0]
        )

        if is_live:
            caption = (
                f"🔴 <b>Начался стрим</b>\n\n"
                f"📺 <b>{title}</b>\n"
                f"🏷 <i>{channel_name}</i>\n\n"
                f"👉 <a href=\"{link}\">Смотреть стрим</a>\n\n"
                f"#live #стрим #youtube"
            )
        elif is_premiere:
            caption = (
                f"⏰ <b>Премьера</b>\n\n"
                f"🎬 <b>{title}</b>\n"
                f"🏷 <i>{channel_name}</i>\n\n"
                f"👉 <a href=\"{link}\">Перейти к премьере</a>\n\n"
                f"#premiere #youtube"
            )
        else:
            caption = (
                f"🚀 <b>Новое видео</b>\n\n"
                f"🎬 <b>{title}</b>\n"
                f"🏷 <i>{channel_name}</i>\n\n"
                f"👉 <a href=\"{link}\">Смотреть на YouTube</a>\n\n"
                f"#youtube #video"
            )

        thumb = None
        if hasattr(latest, 'media_thumbnail') and latest.media_thumbnail:
            thumb = latest.media_thumbnail[0]['url']

        await asyncio.sleep(ANTISPAM_DELAY)

        if thumb:
            await context.bot.send_photo(
                chat_id=tg_channel,
                photo=thumb,
                caption=caption,
                parse_mode=ParseMode.HTML
            )
        else:
            await context.bot.send_message(
                chat_id=tg_channel,
                text=caption,
                parse_mode=ParseMode.HTML
            )

        # 🔹 Обновляем состояние ТОЛЬКО после успешной отправки
        state[channel_id] = latest_video_id
        save_state(state)

        logger.info(f"Отправлено новое видео: {title}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 Бот запущен и отслеживает YouTube‑каналы."
    )

async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🔄 Ручная проверка запущена")
    await check_updates(context)

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Планировщик задач (JobQueue) уже инициализирован внутри Application
    app.job_queue.run_repeating(
        check_updates,
        interval=1800,
        first=10
    )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('checknow', checknow))

    logger.info("Бот успешно запущен")
    app.run_polling()

if __name__ == "__main__":
    main()