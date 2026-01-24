import os
import sys
import json
import logging
import asyncio
import feedparser
import time
from calendar import timegm

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from typing import Any, Dict, cast
import time as _time
from datetime import datetime, timezone

VERSION = "1.2.0"
# =========================================================
# RELEASE: v1.2.0 (STABLE)
# Project: Technology Universe — YouTube Alerts
#
# ✔ Multi-channel YouTube RSS tracking
# ✔ Scheduled stream notifications (with date & time)
# ✔ Live stream start detection (with fallback)
# ✔ New video notifications
# ✔ Shorts filtering (#shorts, /shorts/)
# ✔ Per-channel last_seen_timestamp
# ✔ TTL-based deduplication (anti-spam)
# ✔ State persistence (state.json)
# ✔ Telegram channel routing per YouTube channel
#
# Status: Production-ready
# Branch: 1.2.x
# =========================================================

LOG_FILE = 'bot.log'
STATE_FILE = 'state.json'
ANTISPAM_DELAY = 120
EVENT_TTL = 6 * 60 * 60  # 6 часов

def make_live_key(channel_id: str, video_id: str) -> str:
    return f"{channel_id}|{video_id}"

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
    state.setdefault("live_streams", {})
    state.setdefault("videos", {})
    state.setdefault("last_seen_timestamp", {})
    state.setdefault("initialized", False)
    state.setdefault("sent_events", {})

    now_ts = int(time.time())

    try:

        for channel_id in YOUTUBE_CHANNEL_IDS:
            last_seen = state["last_seen_timestamp"].get(channel_id, 0)

            feed = fetch_feed(channel_id)
            if not feed.entries:
                continue

            if not state["initialized"]:
                newest_ts = 0
                for entry_raw in feed.entries:
                    entry: Dict[str, Any] = entry_raw
                    published_parsed = entry.get("published_parsed")
                    if published_parsed:
                        ts = int(timegm(cast(_time.struct_time, published_parsed)))
                        newest_ts = max(newest_ts, ts)
                state["last_seen_timestamp"][channel_id] = newest_ts
                continue

            for entry_raw in feed.entries:
                entry: Dict[str, Any] = entry_raw
                title = entry.get("title", "")
                link = entry.get("link", "")

                published_parsed = entry.get("published_parsed")
                if published_parsed:
                    published_ts = int(timegm(cast(_time.struct_time, published_parsed)))
                else:
                    published_ts = 0

                if published_ts <= state["last_seen_timestamp"].get(channel_id, 0):
                    continue

                latest_video_id_raw = entry.get("yt_videoid")
                if not isinstance(latest_video_id_raw, str):
                    continue

                latest_video_id = latest_video_id_raw

                video_state = state["videos"].get(latest_video_id, {
                    "scheduled_notified": False,
                    "live_notified": False
                })

                live_key = make_live_key(channel_id, latest_video_id)

                channel_name = CHANNEL_NAMES.get(channel_id, "YouTube")

                title_lower = title.lower() if isinstance(title, str) else ""

                broadcast = entry.get("yt_livebroadcastcontent", "")
                broadcast = broadcast.lower() if isinstance(broadcast, str) else ""

                scheduled_time = None
                raw_ts = entry.get("yt_scheduledstarttime")
                if raw_ts:
                    try:
                        scheduled_time = datetime.fromtimestamp(int(cast(str, raw_ts)), tz=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")
                    except Exception:
                        scheduled_time = None

                live_state = state["live_streams"].get(live_key, {
                    "scheduled_notified": False,
                    "live_notified": False
                })

                is_scheduled_live = False
                is_live = False

                now_utc = int(time.time())

                if broadcast == "live":
                    is_live = True

                elif broadcast == "upcoming":
                    is_scheduled_live = True

                    # FALLBACK: YouTube RSS не всегда меняет статус на "live"
                    if (
                        live_state.get("scheduled_notified")
                        and not live_state.get("live_notified")
                        and published_ts <= now_utc
                    ):
                        is_live = True
                        is_scheduled_live = False
                else:
                    if 'live' in title_lower or 'стрим' in title_lower:
                        is_scheduled_live = True

                is_premiere = False

                if 'премьера' in title_lower or 'premiere' in title_lower:
                    is_premiere = True

                # 🔹 ГИБРИДНЫЙ фильтр Shorts

                link_lower = link.lower() if isinstance(link, str) else ""

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
                    # Removed extra update here to keep single update at end of loop
                    last_seen = state["last_seen_timestamp"][channel_id]
                    continue

                tg_channel = TG_CHANNELS.get(
                    channel_id,
                    list(TG_CHANNELS.values())[0]
                )

                event_type = "scheduled" if is_scheduled_live else "live" if is_live else "video"
                event_key = f"{channel_id}|{latest_video_id}|{event_type}"

                # TTL-антидубликат
                last_sent = state["sent_events"].get(event_key)
                if last_sent and now_ts - last_sent < EVENT_TTL:
                    logger.warning(f"Дубликат подавлен (TTL): {event_key}")
                    continue

                if is_scheduled_live and not live_state["scheduled_notified"]:
                    time_block = (
                        f"🗓 <b>Дата и время:</b> {scheduled_time}\n\n"
                        if scheduled_time else ""
                    )

                    caption = (
                        f"⏰ <b>Запланирован стрим</b>\n\n"
                        f"📺 <b>{title}</b>\n"
                        f"🏷 <i>{channel_name}</i>\n\n"
                        f"{time_block}"
                        f"👉 <a href=\"{link}\">Перейти к стриму</a>\n\n"
                        f"#live #youtube"
                    )
                    live_state["scheduled_notified"] = True
                    state["live_streams"][live_key] = live_state
                    video_state["published"] = True

                elif is_live and not live_state["live_notified"]:
                    caption = (
                        f"🔴 <b>Начался стрим</b>\n\n"
                        f"📺 <b>{title}</b>\n"
                        f"🏷 <i>{channel_name}</i>\n\n"
                        f"👉 <a href=\"{link}\">Смотреть стрим</a>\n\n"
                        f"#live #стрим #youtube"
                    )
                    live_state["live_notified"] = True
                    state["live_streams"][live_key] = live_state

                elif not is_scheduled_live and not is_live and not is_premiere:
                    if video_state.get("published"):
                        continue

                    caption = (
                        f"🚀 <b>Новое видео</b>\n\n"
                        f"📺 <b>{title}</b>\n"
                        f"🏷 <i>{channel_name}</i>\n\n"
                        f"👉 <a href=\"{link}\">Смотреть видео</a>\n\n"
                        f"#video #youtube"
                    )

                else:
                    logger.debug(
                        f"Пропуск: уже обработан | {title} | key={live_key}"
                    )
                    continue

                thumb = None
                media = entry.get("media_thumbnail")
                if isinstance(media, list) and media:
                    thumb = media[0].get("url")

                video_state["published"] = True
                state["videos"][latest_video_id] = video_state
                state["last_seen_timestamp"][channel_id] = max(
                    state["last_seen_timestamp"].get(channel_id, 0),
                    published_ts
                )
                save_state(state)
                last_seen = state["last_seen_timestamp"][channel_id]

                await asyncio.sleep(ANTISPAM_DELAY)

                if thumb and isinstance(thumb, str):
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

                state["sent_events"][event_key] = now_ts

                logger.info(
                    f"Отправлено уведомление: "
                    f"{'LIVE' if is_live else 'SCHEDULED' if is_scheduled_live else 'VIDEO'} | {title} | key={live_key}"
                )

        state["initialized"] = True

        # Очистка старых событий
        for k, ts in list(state["sent_events"].items()):
            if now_ts - ts > EVENT_TTL:
                del state["sent_events"][k]

    finally:
        save_state(state)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            f"🤖 Бот запущен (v{VERSION}) и отслеживает YouTube‑каналы."
        )

async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text("🔄 Ручная проверка запущена")
    await check_updates(context)

def main():
    assert TELEGRAM_TOKEN
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    # Планировщик задач (JobQueue) уже инициализирован внутри Application
    if app.job_queue:
        app.job_queue.run_repeating(
            check_updates,
            interval=1800,
            first=10
        )

    app.add_handler(CommandHandler('start', start))
    app.add_handler(CommandHandler('checknow', checknow))

    logger.info(f"Версия бота: v{VERSION}")
    logger.info("Бот успешно запущен")
    app.run_polling()

if __name__ == "__main__":
    main()