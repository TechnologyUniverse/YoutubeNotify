import os
import sys
import json
import logging
import asyncio
import feedparser
import time
import urllib.request
import ssl
import certifi
import signal

from dotenv import load_dotenv
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

load_dotenv()

# =========================================================
# v1.2.9.40.500 — Stable Production Release (LTS)
# =========================================================

VERSION = "1.2.9.40.500-stable-prod"
STATE_VERSION = "1.2.9.40.500"

SILENT_MODE = os.getenv("SILENT_MODE", "false").lower() == "true"
DEBUG_MODE = os.getenv("DEBUG_MODE", "false").lower() == "true"
BASELINE_MODE = os.getenv("BASELINE_MODE", "true").lower() == "true"
EVENT_TTL = int(os.getenv("EVENT_TTL", "86400"))  # 24h default
BOT_ENV = os.getenv("BOT_ENV", "prod").lower()
STATE_FILE = f"state.{BOT_ENV}.json"

RUN_LOCK = None

LOG_FILE = "bot.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(levelname)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)

logger = logging.getLogger(__name__)

if DEBUG_MODE:
    logger.setLevel(logging.DEBUG)
    logger.debug("DEBUG MODE ENABLED")

TELEGRAM_TOKEN: str = os.getenv("TELEGRAM_TOKEN") or ""
YOUTUBE_CHANNEL_IDS = [
    cid.strip()
    for cid in os.getenv("YOUTUBE_CHANNEL_IDS", "").split(",")
    if cid.strip()
]

TG_CHANNELS = {
    pair.split(":")[0]: pair.split(":")[1]
    for pair in os.getenv("TG_CHANNELS", "").split(",")
    if ":" in pair
}
if TELEGRAM_TOKEN == "":
    logger.error("❌ TELEGRAM_TOKEN не задан")
    sys.exit(1)

if not YOUTUBE_CHANNEL_IDS:
    logger.error("❌ YOUTUBE_CHANNEL_IDS не задан")
    sys.exit(1)

if not TG_CHANNELS:
    logger.error("❌ TG_CHANNELS не задан")
    sys.exit(1)


# =========================================================
# STATE
# =========================================================

def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_state(state):
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp, STATE_FILE)


# =========================================================
# RSS
# =========================================================

def fetch_feed(channel_id: str):
    url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"

    req = urllib.request.Request(
        url,
        headers={"User-Agent": f"YouTubeNotify/{VERSION}"}
    )

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())

    with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
        data = resp.read()

    return feedparser.parse(data)


# =========================================================
# CORE LOGIC
# =========================================================

async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    global RUN_LOCK

    if RUN_LOCK is None:
        RUN_LOCK = asyncio.Lock()

    async with RUN_LOCK:
        state = load_state()

        state.setdefault("state_version", STATE_VERSION)
        state.setdefault("videos", {})
        state.setdefault("live_streams", {})
        state.setdefault("sent_events", {})

        # TTL очистка sent_events
        now_ts = int(time.time())
        expired = [k for k, v in state["sent_events"].items() if now_ts - v > EVENT_TTL]
        for k in expired:
            del state["sent_events"][k]

        for channel_id in YOUTUBE_CHANNEL_IDS:
            try:
                feed = fetch_feed(channel_id)
            except Exception as e:
                logger.error(f"rss_error | {channel_id} | {e}")
                continue

            # Baseline режим — при первом запуске просто фиксируем текущие видео
            if BASELINE_MODE and not state.get("baseline_initialized"):
                for entry in feed.entries:
                    vid = entry.get("yt_videoid")
                    if vid:
                        state["videos"].setdefault(vid, {"published": True, "was_live": False})
                state["baseline_initialized"] = True
                save_state(state)
                logger.info("baseline_initialized")
                continue

            if not feed.entries:
                continue

            # Стабильная сортировка по времени публикации
            feed.entries.sort(
                key=lambda e: e.get("published_parsed") or e.get("updated_parsed") or 0
            )

            for entry in feed.entries:
                video_id = entry.get("yt_videoid")
                if not video_id:
                    continue

                title = entry.get("title", "")
                link = entry.get("link", "")

                if DEBUG_MODE:
                    logger.debug(f"rss_entry | {channel_id} | {video_id} | {title}")

                raw_broadcast = entry.get("yt_livebroadcastcontent")
                broadcast = str(raw_broadcast).lower() if raw_broadcast else "none"

                video_state = state["videos"].setdefault(video_id, {
                    "published": False,
                    "was_live": False,
                })

                live_key = f"{channel_id}|{video_id}"
                live_state = state["live_streams"].setdefault(live_key, {
                    "scheduled": False,
                    "live": False,
                    "ended": False,
                })

                event_type = None

                if broadcast == "upcoming" and not live_state["scheduled"]:
                    event_type = "scheduled"
                    live_state["scheduled"] = True

                elif broadcast == "live" and not live_state["live"]:
                    event_type = "live"
                    live_state["live"] = True
                    video_state["was_live"] = True

                elif broadcast == "none":
                    # Если это был стрим — фиксируем завершение
                    if video_state["was_live"] and not live_state["ended"]:
                        event_type = "ended"
                        live_state["ended"] = True
                    # Иначе считаем обычным видео
                    elif not video_state["published"]:
                        event_type = "video"
                        video_state["published"] = True

                if not event_type:
                    continue

                event_key = f"{channel_id}|{video_id}|{event_type}"
                if event_key in state["sent_events"]:
                    continue

                caption = f"[{event_type.upper()}]\n{title}\n{link}"

                if not SILENT_MODE:
                    chat_id = TG_CHANNELS.get(channel_id)
                    if not chat_id:
                        logger.error(f"tg_channel_not_mapped | {channel_id}")
                        continue

                    try:
                        await context.bot.send_message(
                            chat_id=str(chat_id),
                            text=caption
                        )
                    except Exception as e:
                        logger.error(f"telegram_error | {e}")
                        continue

                state["sent_events"][event_key] = int(time.time())
                logger.info(f"event_sent | {event_key}")

        save_state(state)


# =========================================================
# COMMANDS
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    await update.message.reply_text(
        f"🤖 Бот запущен (v{VERSION})"
    )


async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    if RUN_LOCK and RUN_LOCK.locked():
        await update.message.reply_text("⏳ Проверка уже выполняется")
        return

    await update.message.reply_text("🔄 Ручная проверка запущена")
    await check_updates(context)


async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    state = load_state()

    text = (
        "🩺 <b>Status</b>\n\n"
        f"Версия: <b>{VERSION}</b>\n"
        f"Видео в состоянии: <b>{len(state.get('videos', {}))}</b>\n"
        f"Live-событий: <b>{len(state.get('live_streams', {}))}</b>\n"
        f"Silent mode: <b>{'ON' if SILENT_MODE else 'OFF'}</b>\n"
        "Статус: <b>OK</b>"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def debug(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return

    text = (
        f"DEBUG_MODE: {DEBUG_MODE}\n"
        f"BASELINE_MODE: {BASELINE_MODE}\n"
        f"EVENT_TTL: {EVENT_TTL}\n"
    )
    await update.message.reply_text(text)


def shutdown_handler(signum, frame):
    logger.info("Graceful shutdown triggered")
    sys.exit(0)

signal.signal(signal.SIGTERM, shutdown_handler)
signal.signal(signal.SIGINT, shutdown_handler)


# =========================================================
# MAIN
# =========================================================

def main():
    app = ApplicationBuilder().token(TELEGRAM_TOKEN).build()

    if app.job_queue:
        app.job_queue.run_repeating(
            check_updates,
            interval=1800,
            first=10,
        )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("checknow", checknow))
    app.add_handler(CommandHandler("status", status))
    app.add_handler(CommandHandler("debug", debug))

    logger.info(f"Версия бота: {VERSION}")
    logger.info("Бот успешно запущен")

    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        logger.error("❌ WEBHOOK_URL не задан")
        sys.exit(1)

    logger.info(f"Webhook URL: {webhook_url}")

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        webhook_url=webhook_url,
    )


if __name__ == "__main__":
    main()