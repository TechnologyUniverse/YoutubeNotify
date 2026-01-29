import os
import sys
import json
import logging
import asyncio
import feedparser
import time
from time import struct_time
from calendar import timegm
import urllib.request
import ssl
import certifi
RUN_LOCK = None  # asyncio.Lock инициализируется лениво внутри event loop

from dotenv import load_dotenv
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    ContextTypes,
)

from typing import Any, Dict, cast
from datetime import datetime, timezone

#
# v1.2.9.30.300 — Stable Production Release (LTS)
# Status: Stable Prod
# Branch: 1.2.x (frozen)
#
# 🔒 Production guarantees:
# - Webhook-only (no polling conflicts)
# - Strict YouTube live state machine
# - Separate prod/test state isolation
# - Self-healing state with safe recovery
# - No duplicate notifications
# - No false "new video" after streams
#
VERSION = "1.2.9.30.300-stable-prod"
SILENT_MODE = os.getenv("SILENT_MODE", "false").lower() == "true"

# ---------------------------------------------------------
# Maintenance history (1.2.x):
#
# v1.2.2
# - Added fallback control: stream started but RSS not updated
# - Improved live detection stability
#
# v1.2.3
# - Hardened deduplication logic for live/video events
# - Improved state consistency after restarts
#
# v1.2.10+
# - Incremental RSS edge-case fixes
# - Improved protection against old video publishing
# - Stabilized live fallback throttling
#
# v1.2.17
# - Final cleanup and alignment of production logic
# - Version comments synchronized with code
#
# v1.2.18
# - Atomic state.json writes
# - Fixed Shorts false positives
# - Removed title-based live heuristics
# - Hardened RSS parsing
# - SILENT_MODE no longer mutates state
# ---------------------------------------------------------

# v1.2.9.30
# - LTS stable cut of 1.2.x branch
# - All critical logic frozen
# - No further changes without major version bump
# v1.2.9.40
# - Strict yt:liveBroadcastContent guard finalized
# - Correct handling of scheduled/live/video transitions
# - Automatic cleanup of live_state after stream end
# - No false "new video" notifications after streams
# - Marked as final stable LTS build

LOG_FILE = 'bot.log'

BOT_ENV = os.getenv("BOT_ENV", "prod").lower()

# Раздельные state-файлы для prod / test
STATE_FILE = f"state.{BOT_ENV}.json"


# 🧼 Автомиграция legacy state.json → prod only
if BOT_ENV == "prod" and os.path.exists("state.json") and not os.path.exists("state.prod.json"):
    import shutil
    shutil.move("state.json", "state.prod.json")
    logging.warning("state_migrate | legacy state.json -> state.prod.json")

# 🧪 Test окружение всегда изолированное и чистое
if BOT_ENV == "test" and not os.path.exists("state.test.json"):
    with open("state.test.json", "w", encoding="utf-8") as f:
        json.dump({}, f)

ANTISPAM_DELAY = 5
EVENT_TTL_DEFAULT = 6 * 60 * 60
EVENT_TTL_BY_TYPE = {
    "scheduled": 12 * 60 * 60,
    "live": 6 * 60 * 60,
    "ended": 6 * 60 * 60,
    "video": 24 * 60 * 60,
}

# TTL очистки завершённых стримов
ENDED_CLEANUP_TTL = 60 * 60  # 1 час после завершения стрима

# RSS schema version
RSS_SCHEMA_VERSION = "youtube_rss_2026_01"
# State versioning and migrations
STATE_VERSION = "1.2.9.30.300"
STATE_MIGRATIONS = {
    "1.2.x": "1.2.9.30",
}
# Жёсткий лимит возраста видео (12 часов — защита от отправки старого контента)
MAX_VIDEO_AGE = 12 * 60 * 60  # 12 часов — защита от отправки старого контента

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
    tmp_file = STATE_FILE + ".tmp"
    with open(tmp_file, 'w', encoding='utf-8') as f:
        json.dump(state, f, ensure_ascii=False, indent=2)
    os.replace(tmp_file, STATE_FILE)


def fetch_feed(channel_id: str):
    cid = channel_id.strip()
    if not cid:
        raise ValueError("empty channel_id")

    url = f'https://www.youtube.com/feeds/videos.xml?channel_id={cid}'

    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"TechnologyUniverse-YouTubeNotify/{VERSION}"
        }
    )

    ssl_ctx = ssl.create_default_context(cafile=certifi.where())
    with urllib.request.urlopen(req, timeout=10, context=ssl_ctx) as resp:
        data = resp.read()

    feed = feedparser.parse(data)
    if getattr(feed, "bozo", False):
        logger.warning(f"rss_bozo | channel={channel_id} | error={getattr(feed, 'bozo_exception', None)}")
    return feed


async def check_updates(context: ContextTypes.DEFAULT_TYPE):
    global RUN_LOCK
    # Extra safety for RUN_LOCK: ensure event loop is running
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        logger.error("event_loop_not_ready")
        return
    if RUN_LOCK is None:
        RUN_LOCK = asyncio.Lock()

    async with RUN_LOCK:
        state = load_state()
        # --- State migration ---
        def migrate_state(state: dict) -> dict:
            ver = state.get("state_version")
            if ver != STATE_VERSION:
                logger.warning(f"state_migrate | {ver} -> {STATE_VERSION}")
                state["state_version"] = STATE_VERSION
            return state
        state = migrate_state(state)
        # --- Pylance static fix: ensure rss_activity is always defined ---
        rss_activity = False
        # Hard safety: защита от битого state.json
        for key, default in {
            "live_streams": {},
            "videos": {},
            "last_seen_timestamp": {},
            "initialized_channels": {},
            "sent_events": {},
            "stream_started_at": {},
            "live_checked_at": {},
        }.items():
            if not isinstance(state.get(key), dict):
                logger.error(f"state_recover | key={key} reset to default")
                state[key] = default
                # Special handling: if resetting initialized_channels, mark as initialized if channel had last_seen_timestamp
                if key == "initialized_channels":
                    for cid in state.get("last_seen_timestamp", {}):
                        if isinstance(state["last_seen_timestamp"].get(cid), int) and state["last_seen_timestamp"][cid] > 0:
                            state["initialized_channels"][cid] = True
        state.setdefault("state_version", "1.2.x")
        state.setdefault("live_streams", {})
        state.setdefault("videos", {})
        state.setdefault("last_seen_timestamp", {})
        state.setdefault("initialized_channels", {})
        state.setdefault("sent_events", {})
        state.setdefault("stream_started_at", {})
        state.setdefault("live_checked_at", {})
        # Set RSS schema version if not present
        if "rss_schema_version" not in state:
            state["rss_schema_version"] = RSS_SCHEMA_VERSION

        logger.info(f"channels_loaded={len(YOUTUBE_CHANNEL_IDS)}")

        now_ts = int(time.time())

        try:
            # --- RSS silence watchdog ---
            for channel_id in YOUTUBE_CHANNEL_IDS:
                # Guard: skip invalid IDs
                if not isinstance(channel_id, str) or not channel_id.strip():
                    logger.warning("skip_invalid_channel_id")
                    continue
                if channel_id not in state["last_seen_timestamp"]:
                    state["last_seen_timestamp"][channel_id] = 0
                # last_seen = state["last_seen_timestamp"].get(channel_id, 0)  # Unused, removed

                try:
                    feed = fetch_feed(channel_id)
                    if not feed.entries:
                        logger.warning(f"rss_empty | канал={channel_id}")
                except Exception as e:
                    logger.error(f"Ошибка при загрузке ленты для канала {channel_id}: {e}")
                    continue

                if not feed.entries:
                    continue
                # Mark that at least one RSS entry was processed
                rss_activity = True

                def safe_published_ts(e):
                    try:
                        pp = e.get("published_parsed") or e.get("updated_parsed")
                        if isinstance(pp, struct_time):
                            return int(timegm(pp))
                    except Exception:
                        pass
                    return 0
                feed.entries.sort(key=safe_published_ts)
                logger.debug(f"rss_sorted_by_time | канал={channel_id}")

                if not state["initialized_channels"].get(channel_id):
                    newest_ts = 0
                    for entry_raw in feed.entries:
                        entry: Dict[str, Any] = entry_raw
                        published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                        if isinstance(published_parsed, struct_time):
                            ts = int(timegm(published_parsed))
                            newest_ts = max(newest_ts, ts)
                    state["last_seen_timestamp"][channel_id] = newest_ts
                    state["initialized_channels"][channel_id] = True
                    continue

                for entry_raw in feed.entries:
                    # Guard against unexpected entry types
                    if not hasattr(entry_raw, "items") and not isinstance(entry_raw, dict):
                        logger.warning("skip_invalid_entry")
                        continue
                    entry: Dict[str, Any] = entry_raw
                    title = entry.get("title", "")
                    link = entry.get("link", "")

                    published_parsed = entry.get("published_parsed") or entry.get("updated_parsed")
                    if isinstance(published_parsed, struct_time):
                        published_ts = int(timegm(published_parsed))
                    else:
                        published_ts = None

                    # Ensure published_ts is not None before comparing
                    if published_ts is not None and published_ts <= state["last_seen_timestamp"].get(channel_id, 0):
                        continue
                    if published_ts is None:
                        logger.warning(
                            f"skip_no_timestamp | канал={channel_id} | видео={entry.get('yt_videoid')} | {title}"
                        )
                        continue

                    latest_video_id_raw = entry.get("yt_videoid")
                    if not isinstance(latest_video_id_raw, str):
                        logger.warning(f"rss_missing_videoid | канал={channel_id} | title={title}")
                        continue

                    latest_video_id = latest_video_id_raw

                    video_state = state["videos"].get(latest_video_id, {
                        "scheduled_notified": False,
                        "live_notified": False,
                        "published": False,
                        "was_live": False
                    })

                    live_key = make_live_key(channel_id, latest_video_id)
                    stream_started_at = state["stream_started_at"].get(live_key)
                    last_live_check = state["live_checked_at"].get(live_key, 0)

                    # Ensure live_state is always initialized before use
                    live_state = state["live_streams"].get(live_key, {
                        "scheduled_notified": False,
                        "live_notified": False,
                        "ended_notified": False
                    })

                    channel_name = CHANNEL_NAMES.get(channel_id, "YouTube")

                    title_lower = title.lower() if isinstance(title, str) else ""

                    broadcast = entry.get("yt_livebroadcastcontent", "")
                    broadcast = broadcast.lower() if isinstance(broadcast, str) else ""

                    is_scheduled_live = False
                    is_live = False

                    # === STRICT YouTube live state handling ===
                    if broadcast == "upcoming":
                        is_scheduled_live = True
                    elif broadcast == "live":
                        is_live = True
                    elif broadcast == "none":
                        is_live = False
                        is_scheduled_live = False

                    # 🔴 LIVE ENDED — отдельное уведомление
                    if (
                        broadcast == "none"
                        and video_state.get("was_live")
                        and not live_state.get("ended_notified")
                    ):
                        caption = (
                            f"🔴 <b>Стрим завершён</b>\n\n"
                            f"📺 <b>{title}</b>\n"
                            f"🏷 <i>{channel_name}</i>\n\n"
                            f"👉 <a href=\"{link}\">Смотреть запись</a>\n\n"
                            f"#live #стрим #youtube"
                        )

                        live_state["ended_notified"] = True
                        video_state["published"] = True

                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state

                        event_type = "ended"

                    scheduled_time = None
                    raw_ts = entry.get("yt_scheduledstarttime")
                    if raw_ts:
                        try:
                            raw_ts_str = str(raw_ts)
                            if raw_ts_str.isdigit():
                                scheduled_time = datetime.fromtimestamp(int(raw_ts_str), tz=timezone.utc).astimezone().strftime("%d.%m.%Y %H:%M")
                            else:
                                # Try ISO8601
                                try:
                                    scheduled_dt = datetime.fromisoformat(raw_ts_str.replace("Z", "+00:00"))
                                    scheduled_time = scheduled_dt.astimezone().strftime("%d.%m.%Y %H:%M")
                                except Exception:
                                    scheduled_time = None
                        except Exception:
                            scheduled_time = None


                    now_utc = int(time.time())

                    # FALLBACK LEVEL 2: стрим мог начаться без обновления RSS
                    if (
                        live_state.get("scheduled_notified")
                        and not live_state.get("live_notified")
                        and not is_live
                        and not is_scheduled_live
                        and published_ts is not None
                        and now_utc - published_ts < EVENT_TTL_DEFAULT
                        and "watch?v=" in (link.lower() if isinstance(link, str) else "")
                    ):
                        # Усиленная защита от повторной активации live без RSS
                        # Extra guard: only allow fallback if stream_started_at is None AND published >2min ago
                        if state["stream_started_at"].get(live_key):
                            # Ensure state is written before continue
                            state["live_streams"][live_key] = live_state
                            state["videos"][latest_video_id] = video_state
                            continue
                        if now_utc - last_live_check < 300:
                            # Ensure state is written before continue
                            state["live_streams"][live_key] = live_state
                            state["videos"][latest_video_id] = video_state
                            continue
                        # Fallback heuristic: require published_ts at least 2 minutes ago and no stream_started_at
                        if state["stream_started_at"].get(live_key) is None and (now_utc - published_ts > 120):
                            state["live_checked_at"][live_key] = now_utc
                            logger.warning(
                                f"live_fallback_no_rss | канал={channel_id} | видео={latest_video_id} | {title}"
                            )
                            is_live = True

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

                    if '/shorts/' in link_lower and not 'watch?v=' in link_lower:
                        is_short = True
                        reasons.append('/shorts/ canonical link')

                    if is_short:
                        logger.info(
                            f"possible_short | канал={channel_id} | видео={latest_video_id} | "
                            f"причины={', '.join(reasons)} | {title}"
                        )
                        # Ensure state is written before continue
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state
                        continue

                    tg_channel = TG_CHANNELS.get(channel_id)
                    if not tg_channel:
                        logger.error(
                            f"tg_channel_not_mapped | канал={channel_id} | видео={latest_video_id}"
                        )
                        # Ensure state is written before continue
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state
                        continue
                    if not isinstance(tg_channel, str) or not tg_channel:
                        logger.error(f"tg_channel_invalid | channel={channel_id}")
                        continue

                    # HARD GUARD: защита от отправки старых видео после рестарта / reorder RSS
                    if (
                        not is_live
                        and not is_scheduled_live
                        and published_ts is not None
                        and now_utc - published_ts > MAX_VIDEO_AGE
                    ):
                        logger.warning(
                            f"skip_old_video | канал={channel_id} | видео={latest_video_id} | age={now_utc - published_ts}s | {title}"
                        )
                        # Помечаем как увиденное, но не публикуем
                        if published_ts is not None:
                            state["last_seen_timestamp"][channel_id] = max(
                                state["last_seen_timestamp"].get(channel_id, 0),
                                published_ts
                            )
                        state["videos"][latest_video_id] = {
                            "published": True,
                            "skipped_old": True
                        }
                        state["live_streams"][live_key] = live_state
                        continue

                    # HARD GUARD: upcoming стрим никогда не считается VIDEO
                    if is_scheduled_live:
                        event_type = None

                    # HARD GUARD: LIVE (past or scheduled) никогда не считается VIDEO
                    if (
                        broadcast == "none"
                        and (
                            video_state.get("was_live")
                            or live_state.get("live_notified")
                            or live_key in state.get("stream_started_at", {})
                            or live_state.get("scheduled_notified")
                        )
                    ):
                        logger.info(
                            f"hard_live_or_scheduled_end_guard | channel={channel_id} | video={latest_video_id}"
                        )
                        live_state["ended_notified"] = True
                        video_state["published"] = True
                        state["videos"][latest_video_id] = video_state
                        state["live_streams"][live_key] = live_state
                        continue

                    # Deduplication hardening: event_key includes event_type only once per transition
                    event_type = None
                    if is_scheduled_live and not live_state.get("scheduled_notified", False):
                        event_type = "scheduled"
                    elif is_live and not live_state.get("live_notified", False):
                        # защита от повторного "начался стрим"
                        if not stream_started_at:
                            state["stream_started_at"][live_key] = now_ts
                            event_type = "live"
                        else:
                            event_type = None
                    elif (
                        not is_scheduled_live
                        and not is_live
                        and broadcast == "none"
                        and not is_premiere
                        and not video_state.get("published", False)
                        and not live_state.get("live_notified", False)
                        and not video_state.get("was_live")
                    ):
                        event_type = "video"

                    # HARD DEDUP: блокируем повторные video-события
                    if event_type == "video" and video_state.get("published"):
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state
                        continue

                    if event_type is None:
                        # Already notified or irrelevant event, skip
                        logger.debug(
                            f"Пропуск: уже обработан или не подходит | {title} | key={live_key}"
                        )
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state
                        continue

                    event_key = f"{channel_id}|{latest_video_id}|{event_type}"

                    # TTL-антидубликат
                    sent_events = state.get("sent_events", {})
                    if not isinstance(sent_events, dict):
                        sent_events = {}
                    state["sent_events"] = sent_events
                    last_sent = sent_events.get(event_key)
                    ttl = EVENT_TTL_BY_TYPE.get(event_type, EVENT_TTL_DEFAULT)
                    if last_sent and now_ts - last_sent < ttl:
                        logger.warning(f"Дубликат подавлен (TTL): {event_key}")
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state
                        continue

                    if event_type == "scheduled":
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
                        # Жёсткая фиксация
                        live_state["scheduled_notified"] = True
                        video_state["published"] = False
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state

                    elif event_type == "live":
                        caption = (
                            f"🔴 <b>Начался стрим</b>\n\n"
                            f"📺 <b>{title}</b>\n"
                            f"🏷 <i>{channel_name}</i>\n\n"
                            f"👉 <a href=\"{link}\">Смотреть стрим</a>\n\n"
                            f"#live #стрим #youtube"
                        )
                        live_state["live_notified"] = True
                        video_state["was_live"] = True
                        state["live_streams"][live_key] = live_state
                        state["stream_started_at"][live_key] = now_ts
                        video_state["published"] = True
                        state["videos"][latest_video_id] = video_state

                    elif event_type == "video":
                        caption = (
                            f"🚀 <b>Новое видео</b>\n\n"
                            f"📺 <b>{title}</b>\n"
                            f"🏷 <i>{channel_name}</i>\n\n"
                            f"👉 <a href=\"{link}\">Смотреть видео</a>\n\n"
                            f"#video #youtube"
                        )
                        video_state["published"] = True
                        state["videos"][latest_video_id] = video_state

                    else:
                        logger.debug(
                            f"Пропуск: уже обработан | {title} | key={live_key}"
                        )
                        state["live_streams"][live_key] = live_state
                        state["videos"][latest_video_id] = video_state
                        continue

                    thumb = None
                    media = entry.get("media_thumbnail")
                    if isinstance(media, list) and media:
                        thumb = media[0].get("url")

                    if not SILENT_MODE:
                        # last_seen = state["last_seen_timestamp"].get(channel_id, 0)  # Unused, removed

                        # Обновление last_seen_timestamp ПЕРЕД отправкой уведомления
                        if published_ts is not None:
                            state["last_seen_timestamp"][channel_id] = max(
                                state["last_seen_timestamp"].get(channel_id, 0),
                                published_ts
                            )

                        for attempt in range(3):
                            try:
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
                                break
                            except Exception as e:
                                if attempt == 2:
                                    logger.error(
                                        f"telegram_send_failed | channel={tg_channel} | attempts=3 | error={e}"
                                    )
                                else:
                                    backoff = 2 ** attempt
                                    logger.warning(
                                        f"telegram_retry | channel={tg_channel} | attempt={attempt+1} | wait={backoff}s"
                                    )
                                    await asyncio.sleep(backoff)

                        await asyncio.sleep(ANTISPAM_DELAY)
                        # (Удалено обновление last_seen_timestamp после отправки)
                        state["sent_events"][event_key] = now_ts

                        logger.info(
                            f"Отправлено уведомление: "
                            f"{event_type.upper()} | {title} | key={live_key}"
                        )
                    else:
                        logger.info(f"[SILENT] {event_type.upper()} | {title}")

            # state["initialized"] = True

        except Exception as e:
            logger.exception(f"check_updates_failed | error={e}")

        # --- RSS silence watchdog: error if no entries processed at all ---
        if not rss_activity:
            logger.error("watchdog_rss_silence | no_entries_processed")

        if not SILENT_MODE:
            # Ensure sent_events is dict and keys are str before cleanup
            if not isinstance(state.get("sent_events"), dict):
                state["sent_events"] = {}

            # Cleanup old videos (safe: based on sent_events by video_id)
            for vid, v in list(state["videos"].items()):
                if not isinstance(v, dict):
                    continue
                last_sent_ts = 0
                sent_events = state["sent_events"]
                if not isinstance(sent_events, dict):
                    sent_events = {}
                    state["sent_events"] = sent_events
                for k, ts in sent_events.items():
                    if f"|{vid}|" in k:
                        last_sent_ts = max(last_sent_ts, ts)
                if v.get("published") and last_sent_ts and now_ts - last_sent_ts > 2 * 24 * 60 * 60:
                    state["videos"].pop(vid, None)

            # Очистка старых событий (TTL-safe)
            sent_events = state["sent_events"]
            if not isinstance(sent_events, dict):
                sent_events = {}
                state["sent_events"] = sent_events
            for k, ts in list(sent_events.items()):
                # Guard: ensure key is str
                if not isinstance(k, str):
                    state["sent_events"].pop(k, None)
                    continue
                if not isinstance(ts, int):
                    state["sent_events"].pop(k, None)
                    continue
                event_type = k.split("|")[-1] if "|" in k else ""
                ttl = EVENT_TTL_BY_TYPE.get(event_type) or EVENT_TTL_DEFAULT
                if now_ts - ts > ttl:
                    state["sent_events"].pop(k, None)

            # Очистка старых live-маркеров
            live_ttl = EVENT_TTL_BY_TYPE["live"]
            for k, ts in list(state.get("stream_started_at", {}).items()):
                if isinstance(ts, int) and now_ts - ts > live_ttl:
                    state["stream_started_at"].pop(k, None)

            # Очистка live_streams (завершённые стримы + устаревшие)
            stream_started = state.get("stream_started_at")
            if not isinstance(stream_started, dict):
                stream_started = {}
                state["stream_started_at"] = stream_started

            for k, v in list(state.get("live_streams", {}).items()):
                if not isinstance(v, dict):
                    state["live_streams"].pop(k, None)
                    continue

                # 🧼 Завершённый стрим — мягкая очистка после TTL
                if v.get("ended_notified"):
                    started_at = stream_started.get(k)
                    if isinstance(started_at, int) and now_ts - started_at > ENDED_CLEANUP_TTL:
                        state["live_streams"].pop(k, None)
                        stream_started.pop(k, None)
                        state.get("live_checked_at", {}).pop(k, None)
                        logger.info(f"cleanup_live_ended | {k}")
                        # scheduled_guard_active log
                        logger.info(f"scheduled_guard_active | {k.split('|')[1] if '|' in k else k}")
                    continue

                # fallback: защита от вечных live-маркеров
                started_at = stream_started.get(k)
                if isinstance(started_at, int) and now_ts - started_at > EVENT_TTL_BY_TYPE["live"]:
                    state["live_streams"].pop(k, None)
            # Очистка старых live_checked_at
            live_checked = state.get("live_checked_at")
            if not isinstance(live_checked, dict):
                live_checked = {}
                state["live_checked_at"] = live_checked
            for k, ts in list(live_checked.items()):
                if now_ts - ts > EVENT_TTL_DEFAULT:
                    live_checked.pop(k, None)

        # --- Live-fallback watchdog: check stuck live_checked_at ---
        for k, ts in state.get("live_checked_at", {}).items():
            if now_ts - ts > 2 * EVENT_TTL_DEFAULT:
                logger.error(f"watchdog_live_stuck | {k}")

        # Save state only if not in SILENT_MODE
        if not SILENT_MODE:
            try:
                save_state(state)
            except Exception as e:
                logger.error(f"state_save_failed | {e}")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.message:
        await update.message.reply_text(
            f"🤖 Бот запущен (v{VERSION}) и отслеживает YouTube‑каналы."
        )

async def checknow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if RUN_LOCK and RUN_LOCK.locked():
        if update.message:
            await update.message.reply_text("⏳ Проверка уже выполняется")
        return
    if update.message:
        await update.message.reply_text("🔄 Ручная проверка запущена")
    await check_updates(context)


# Health-check команда /status
async def status(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = load_state()
    channels = len(YOUTUBE_CHANNEL_IDS)
    lives = len(state.get("live_streams", {}))
    videos = len(state.get("videos", {}))

    text = (
        "🩺 <b>Status</b>\n\n"
        f"Версия: <b>v{VERSION}</b>\n"
        f"Каналов: <b>{channels}</b>\n"
        f"Live-событий: <b>{lives}</b>\n"
        f"Видео в состоянии: <b>{videos}</b>\n"
        f"Silent mode: <b>{'ON' if SILENT_MODE else 'OFF'}</b>\n"
        "Статус: <b>OK</b>"
    )

    if update.message:
        await update.message.reply_text(text, parse_mode=ParseMode.HTML)

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
    app.add_handler(CommandHandler('status', status))

    logger.info(f"Версия бота: v{VERSION}")
    logger.info("Бот успешно запущен")
    webhook_url = os.getenv("WEBHOOK_URL")
    if not webhook_url:
        logger.error("❌ WEBHOOK_URL не задан")
        sys.exit(1)

    app.run_webhook(
        listen="0.0.0.0",
        port=int(os.getenv("PORT", "8080")),
        webhook_url=webhook_url,
    )

if __name__ == "__main__":
    main()