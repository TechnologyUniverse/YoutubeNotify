import asyncio
import time
import logging
import random
from telegram.error import RetryAfter, TimedOut, NetworkError
from telegram.ext import ContextTypes
from .config import (
    YOUTUBE_CHANNEL_IDS,
    TG_CHANNELS,
    SILENT_MODE,
    BASELINE_MODE,
    EVENT_TTL,
    MAX_EVENTS_PER_RUN,
    MAX_FEED_ITEMS,
    STATE_VERSION,
)
from .rss import RSSClient
from .state import StateManager

logger = logging.getLogger(__name__)



class Engine:
    def __init__(self, state_manager: StateManager):
        self.state_manager = state_manager
        self.run_lock = asyncio.Lock()
        self.rss = RSSClient()
        # Metrics (v1.4.2 - no globals)
        self.safe_mode_active = False
        self.last_event_sent = 0
        self.events_sent_total = 0
        self.check_duration_total = 0
        self.check_run_count = 0

        # ---- Extended Metrics (v1.5.0) ----
        self.start_time = int(time.time())
        self.last_run_duration = 0
        self.telegram_failures_total = 0
        self.rss_failures_total = 0
        self.circuit_breaker_triggers = 0

        # Circuit Breaker (v1.4.4)
        self.channel_failures = {}
        self.channel_disabled_until = {}
        self.breaker_threshold = 5
        self.breaker_timeout = 600  # seconds

        # ---- Structured Logging (v1.4.5) ----
        def _log_system(self, level, message):
            logger.log(level, f"[SYSTEM] {message}")

        def _log_event(self, level, message):
            logger.log(level, f"[EVENT] {message}")

        def _log_rss(self, level, message):
            logger.log(level, f"[RSS] {message}")

        def _log_telegram(self, level, message):
            logger.log(level, f"[TELEGRAM] {message}")

        def _log_circuit(self, level, message):
            logger.log(level, f"[CIRCUIT] {message}")

        self._log_system = _log_system.__get__(self)
        self._log_event = _log_event.__get__(self)
        self._log_rss = _log_rss.__get__(self)
        self._log_telegram = _log_telegram.__get__(self)
        self._log_circuit = _log_circuit.__get__(self)

        # ---- Telegram Rate Limiter (v1.4.6) ----
        self.last_telegram_send = 0.0
        self.telegram_min_interval = 1.0  # seconds

    async def _safe_send(self, context, chat_id, text):
        max_attempts = 5
        base_delay = 1.0

        for attempt in range(1, max_attempts + 1):
            try:
                now_send = time.time()
                elapsed = now_send - self.last_telegram_send
                if elapsed < self.telegram_min_interval:
                    await asyncio.sleep(self.telegram_min_interval - elapsed)

                await context.bot.send_message(
                    chat_id=str(chat_id),
                    text=text,
                )

                self.last_telegram_send = time.time()
                return True

            except RetryAfter as e:
                wait_time = int(getattr(e, "retry_after", 5))
                self._log_telegram(
                    logging.WARNING,
                    f"type=telegram_rate_limit | retry_after={wait_time}s"
                )
                await asyncio.sleep(wait_time)

            except (TimedOut, NetworkError) as e:
                jitter = random.uniform(0, 1)
                delay = base_delay * (2 ** attempt) + jitter
                self._log_telegram(
                    logging.WARNING,
                    f"type=telegram_network_error | attempt={attempt} | delay={round(delay,2)}"
                )
                await asyncio.sleep(delay)

            except Exception as e:
                jitter = random.uniform(0, 1)
                delay = base_delay * (2 ** attempt) + jitter
                self._log_telegram(
                    logging.ERROR,
                    f"type=telegram_error | attempt={attempt} | error={e}"
                )
                await asyncio.sleep(delay)

        self._log_telegram(logging.CRITICAL, "type=telegram_failed_after_retries")
        return False

    async def run(self, context: ContextTypes.DEFAULT_TYPE):

        async with self.run_lock:
            return await asyncio.wait_for(
                self._run_internal(context),
                timeout=60
            )

    async def _run_internal(self, context: ContextTypes.DEFAULT_TYPE):

        run_start = time.time()
        events_sent_this_run = 0
        telegram_failures_this_run = 0
        self.safe_mode_active = False

        state = self.state_manager.load()

        # ---- STATE INIT ----
        if state.get("state_version") != STATE_VERSION:
            self._log_system(
                logging.WARNING,
                f"state_migrate | {state.get('state_version')} -> {STATE_VERSION}"
            )
            state["state_version"] = STATE_VERSION

        state.setdefault("baseline_initialized", False)
        state.setdefault("videos", {})
        state.setdefault("live_streams", {})
        state.setdefault("sent_events", {})
        state.setdefault("disabled_channels", {})

        # ---- TTL CLEANUP ----
        now_ts = int(time.time())
        expired = [
            k for k, v in state["sent_events"].items()
            if not isinstance(v, int) or now_ts - v > EVENT_TTL
        ]
        for k in expired:
            del state["sent_events"][k]

        # ---- BASELINE INIT ----
        if BASELINE_MODE and not state.get("baseline_initialized"):
            for channel_id in YOUTUBE_CHANNEL_IDS:
                try:
                    entries = await asyncio.wait_for(
                        asyncio.to_thread(self.rss.fetch, channel_id),
                        timeout=15
                    )
                except Exception as e:
                    self._log_rss(
                        logging.ERROR,
                        f"baseline_fetch_error | {channel_id} | {e}"
                    )
                    entries = []

                if not entries:
                    continue

                for entry in entries:
                    vid = entry.get("yt_videoid")
                    if not vid:
                        continue

                    state["videos"].setdefault(
                        vid, {"published": True, "was_live": False}
                    )

                    live_key = f"{channel_id}|{vid}"
                    state["live_streams"].setdefault(
                        live_key,
                        {"scheduled": False, "live": False, "ended": False},
                    )

            state["baseline_initialized"] = True
            self.state_manager.save(state)
            self._log_system(logging.INFO, "baseline_initialized")
            return

        # ---- MAIN LOOP ----
        for channel_id in YOUTUBE_CHANNEL_IDS:

            now = int(time.time())

            # Circuit breaker check
            if channel_id in self.channel_disabled_until:
                if now < self.channel_disabled_until[channel_id]:
                    continue
                else:
                    del self.channel_disabled_until[channel_id]
                    self.channel_failures[channel_id] = 0
                    self._log_circuit(
                        logging.INFO,
                        f"recovered | {channel_id}"
                    )

            try:
                entries = await asyncio.wait_for(
                    asyncio.to_thread(self.rss.fetch, channel_id),
                    timeout=15
                )
            except Exception as e:
                self._log_rss(
                    logging.ERROR,
                    f"fetch_timeout_or_error | {channel_id} | {e}"
                )
                self.rss_failures_total += 1
                entries = []

            if not isinstance(entries, list):
                self._log_rss(
                    logging.ERROR,
                    f"invalid_feed_type | {channel_id} | {type(entries)}"
                )
                entries = []

            if not entries:
                fails = self.channel_failures.get(channel_id, 0) + 1
                self.channel_failures[channel_id] = fails

                if fails >= self.breaker_threshold:
                    self.channel_disabled_until[channel_id] = now + self.breaker_timeout
                    self.circuit_breaker_triggers += 1
                    self._log_circuit(
                        logging.WARNING,
                        f"type=circuit_breaker_triggered | channel={channel_id} | timeout={self.breaker_timeout}"
                    )
                continue
            else:
                self.channel_failures[channel_id] = 0

            entries.sort(
                key=lambda e: e.get("published_parsed")
                or e.get("updated_parsed")
                or 0
            )

            entries = entries[-MAX_FEED_ITEMS:]

            for entry in entries:
                video_id = entry.get("yt_videoid")
                if not video_id:
                    continue

                raw_broadcast = entry.get("yt_livebroadcastcontent")
                broadcast = (
                    str(raw_broadcast).lower() if raw_broadcast else "none"
                )

                video_state = state["videos"].setdefault(
                    video_id, {"published": False, "was_live": False}
                )

                live_key = f"{channel_id}|{video_id}"
                live_state = state["live_streams"].setdefault(
                    live_key,
                    {"scheduled": False, "live": False, "ended": False},
                )

                event_type = None

                if broadcast == "upcoming" and not live_state["scheduled"]:
                    event_type = "scheduled"
                    live_state["scheduled"] = True

                elif broadcast == "live" and not live_state["live"]:
                    event_type = "live"
                    live_state["live"] = True
                    video_state["was_live"] = True

                elif broadcast == "none":
                    if video_state["was_live"] and not live_state["ended"]:
                        event_type = "ended"
                        live_state["ended"] = True
                    elif not video_state["published"]:
                        event_type = "video"
                        video_state["published"] = True

                if not event_type:
                    continue

                event_key = f"{channel_id}|{video_id}|{event_type}"
                if event_key in state["sent_events"]:
                    continue

                if events_sent_this_run >= MAX_EVENTS_PER_RUN:
                    self.state_manager.save(state)
                    return

                if not SILENT_MODE:
                    chat_id = TG_CHANNELS.get(channel_id)
                    if not chat_id:
                        self._log_telegram(
                            logging.ERROR,
                            f"tg_channel_missing | {channel_id}"
                        )
                        continue
                    else:
                        text = (
                            f"[{event_type.upper()}]\n"
                            f"{entry.get('title','')}\n"
                            f"{entry.get('link','')}"
                        )

                        success = await self._safe_send(context, chat_id, text)
                        if not success:
                            telegram_failures_this_run += 1
                            self.telegram_failures_total += 1
                            if telegram_failures_this_run >= 3:
                                self.safe_mode_active = True
                                self._log_system(
                                    logging.ERROR,
                                    "telegram_safe_mode_triggered"
                                )
                                self.state_manager.save(state)
                                return
                            continue

                self.last_event_sent = int(time.time())
                state["sent_events"][event_key] = self.last_event_sent
                self.events_sent_total += 1
                events_sent_this_run += 1

        # Auto recovery from safe mode
        if self.safe_mode_active and telegram_failures_this_run == 0:
            self.safe_mode_active = False
            self._log_system(logging.INFO, "telegram_safe_mode_recovered")

        duration = time.time() - run_start
        self.last_run_duration = duration
        self.check_duration_total += duration
        self.check_run_count += 1

        self._log_system(
            logging.INFO,
            f"run_complete | events={events_sent_this_run} | "
            f"duration={round(duration,2)}s | "
            f"safe_mode={self.safe_mode_active}"
        )
        self.state_manager.save(state)