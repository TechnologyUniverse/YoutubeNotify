import asyncio
import logging
import time
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import BadRequest, Forbidden, RetryAfter, TimedOut, NetworkError
from .config import (YOUTUBE_CHANNEL_IDS, TG_CHANNELS, SILENT_MODE, BASELINE_MODE,
                     MAX_EVENTS_PER_RUN, MAX_FEED_ITEMS, STATE_SCHEMA_VERSION, YOUTUBE_API_KEY)
from .youtube_api import YouTubeAPIClient

logger = logging.getLogger(__name__)
LABELS = {'video': 'Новое видео', 'scheduled': 'Запланирован стрим', 'live': 'Стрим начался', 'ended': 'Стрим завершён'}
FLAGS = {'video': ('videos', 'published'), 'scheduled': ('live_streams', 'scheduled'),
         'live': ('live_streams', 'live'), 'ended': ('live_streams', 'ended')}

class DeliveryUncertain(Exception): pass
class DeliveryRejected(Exception): pass

class Engine:
    def __init__(self, state_manager):
        self.state_manager = state_manager
        self.run_lock = asyncio.Lock()
        self.youtube = YouTubeAPIClient(YOUTUBE_API_KEY)
        self.safe_mode_active = False
        self.last_event_sent = self.events_sent_total = 0
        self.check_duration_total = self.check_run_count = 0
        self.start_time = int(time.time())
        self.last_run_duration = 0
        self.telegram_failures_total = self.youtube_failures_total = 0
        self.circuit_breaker_triggers = 0
        self.last_telegram_send = 0.0
        self.last_check = self.last_successful_check = 0
        self.last_error = None
        self.queue_counts = {}
        self.channel_status = {}

    def _state(self):
        state = self.state_manager.load()
        schema = state.get('schema_version', STATE_SCHEMA_VERSION)
        if schema not in (2, STATE_SCHEMA_VERSION):
            raise ValueError('unsupported_state_schema')
        state['schema_version'] = STATE_SCHEMA_VERSION
        state.setdefault('baseline_initialized', False)
        for key in ('videos', 'live_streams', 'sent_events', 'outbox', 'channel_baselines', 'premieres'):
            state.setdefault(key, {})
            if not isinstance(state[key], dict): raise ValueError('invalid_state_structure')
        state.setdefault('history', [])
        for job in state['outbox'].values():
            if job['status'] == 'sending':
                job['status'] = 'uncertain'
                job['error'] = 'interrupted_delivery'
        self.last_successful_check = state.get('last_successful_check', 0)
        self.last_event_sent = state.get('last_event_sent', 0)
        return state

    def _save(self, state):
        self.state_manager.save(state)
        self.queue_counts = {s: sum(j['status'] == s for j in state['outbox'].values())
                             for s in ('queued', 'uncertain', 'failed')}
        self.safe_mode_active = bool(self.queue_counts['uncertain'] or self.queue_counts['failed'])

    @staticmethod
    def _mark(state, job):
        table, flag = FLAGS[job['type']]
        key = job['video_id'] if table == 'videos' else job['channel_id'] + '|' + job['video_id']
        state[table].setdefault(key, {})[flag] = True
        if job['type'] in ('live', 'ended'):
            state['videos'].setdefault(job['video_id'], {})['was_live'] = True

    async def _safe_send(self, context, chat_id, text, video_url, thumbnail_url=None):
        await asyncio.sleep(max(0, 1 - (time.time() - self.last_telegram_send)))
        markup = InlineKeyboardMarkup([[InlineKeyboardButton('Смотреть на YouTube', url=video_url)]])
        try:
            if thumbnail_url:
                try:
                    message = await context.bot.send_photo(chat_id=str(chat_id), photo=thumbnail_url,
                                                           caption=text, reply_markup=markup)
                except BadRequest:
                    # A definite rejection of the photo permits one text fallback.
                    message = await context.bot.send_message(chat_id=str(chat_id), text=text, reply_markup=markup)
            else:
                message = await context.bot.send_message(chat_id=str(chat_id), text=text, reply_markup=markup)
            self.last_telegram_send = time.time()
            return message.message_id
        except RetryAfter:
            raise
        except (TimedOut, NetworkError):
            # Telegram may have accepted the request. Never automatically repeat it.
            raise DeliveryUncertain('telegram_response_unknown') from None
        except (BadRequest, Forbidden):
            raise DeliveryRejected('telegram_rejected') from None

    async def run(self, context):
        async with self.run_lock:
            started = time.time()
            self.last_check = started
            try:
                await asyncio.wait_for(self._run_internal(context), timeout=120)
            except asyncio.CancelledError:
                self.last_error = 'interrupted'; raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                logger.error('monitor_check_failed type=%s', self.last_error)
            finally:
                self.last_run_duration = time.time() - started
                self.check_duration_total += self.last_run_duration
                self.check_run_count += 1

    async def _deliver(self, context, state, budget=MAX_EVENTS_PER_RUN, only_keys=None):
        count = 0
        for key, job in list(state['outbox'].items()):
            if only_keys is not None and key not in only_keys: continue
            if count >= budget: break
            if job['status'] != 'queued' or job.get('next_attempt', 0) > time.time(): continue
            if SILENT_MODE: continue  # Pending events remain queued while paused.
            chat = TG_CHANNELS.get(job['channel_id'])
            if not chat:
                job.update(status='failed', error='destination_missing'); self._save(state); continue
            job['status'] = 'sending'; job['attempts'] = job.get('attempts', 0) + 1
            self._save(state)  # Durable before the network request.
            count += 1
            try:
                message_id = await self._safe_send(context, chat, job['text'], job['url'], job.get('thumbnail_url'))
            except RetryAfter as exc:
                delay = exc.retry_after
                if hasattr(delay, 'total_seconds'): delay = delay.total_seconds()
                job.update(status='queued', next_attempt=time.time() + float(delay) + 1, error='rate_limit')
                self.telegram_failures_total += 1
            except DeliveryRejected:
                job.update(status='failed', error='telegram_rejected'); self.telegram_failures_total += 1
            except BaseException as exc:
                job.update(status='uncertain', error='telegram_response_unknown')
                self.telegram_failures_total += 1
                self._save(state)
                if isinstance(exc, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)): raise
            else:
                self._mark(state, job)
                now = int(time.time())
                state['sent_events'][key] = now
                state['last_event_sent'] = self.last_event_sent = now
                state['history'] = (state['history'] + [{'event': key, 'message_id': message_id, 'sent_at': now}])[-500:]
                del state['outbox'][key]
                self.events_sent_total += 1
            self._save(state)

        return count

    async def run_premieres(self, context):
        async with self.run_lock:
            try:
                await asyncio.wait_for(self._run_internal(context, premiere_only=True), timeout=60)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.error('premiere_check_failed type=%s', type(exc).__name__)

    async def _run_internal(self, context, premiere_only=False):
        state = self._state()
        watched = [p for vid, p in state['premieres'].items()
                   if not state['videos'].get(vid, {}).get('published')
                   and p.get('scheduled_timestamp', 0) <= time.time() + 60]
        if premiere_only and not watched: return
        only_keys = ({p['channel_id'] + '|' + p['video_id'] + '|video' for p in watched}
                     if premiere_only else None)
        self._save(state)
        # Deliver persisted events even if YouTube is temporarily unavailable.
        used = await self._deliver(context, state, only_keys=only_keys)
        success = True
        channels = sorted({p['channel_id'] for p in watched}) if premiere_only else YOUTUBE_CHANNEL_IDS
        for channel in channels:
            try:
                if premiere_only:
                    ids = [p['video_id'] for p in watched if p['channel_id'] == channel]
                    entries = await asyncio.wait_for(asyncio.to_thread(self.youtube.fetch_ids, ids), timeout=50)
                else:
                    entries = await asyncio.wait_for(asyncio.to_thread(self.youtube.fetch, channel, MAX_FEED_ITEMS), timeout=50)
                if not isinstance(entries, list): raise ValueError('invalid_feed')
            except Exception as exc:
                success = False; self.youtube_failures_total += 1
                self.channel_status[channel] = {'ok': False, 'error': type(exc).__name__}
                logger.error('youtube_check_failed type=%s', type(exc).__name__)
                continue
            self.channel_status[channel] = {'ok': True, 'checked_at': int(time.time()), 'items': len(entries)}
            baseline = not premiere_only and BASELINE_MODE and not state['baseline_initialized'] and not state['channel_baselines'].get(channel)
            for entry in sorted(entries, key=lambda x: x.get('published_timestamp', 0)):
                vid = entry.get('video_id')
                if not vid: continue
                status = entry.get('event_status', 'video')
                if entry.get('is_premiere') or vid in state['premieres']:
                    state['premieres'][vid] = {
                        'channel_id': channel, 'video_id': vid,
                        'scheduled_timestamp': entry.get('scheduled_timestamp', 0),
                    }
                    # Cancel only definitely unsent legacy stream jobs for this premiere.
                    # Sent history and ambiguous deliveries are never rewritten/retried.
                    for stream_event in ('scheduled', 'live', 'ended'):
                        old_key = channel + '|' + vid + '|' + stream_event
                        if state['outbox'].get(old_key, {}).get('status') == 'queued':
                            del state['outbox'][old_key]
                    if status == 'upcoming':
                        continue  # Not published: keep watching, do not mark it sent.
                    event = 'video'
                else:
                    event = 'scheduled' if status == 'upcoming' else status
                if event not in FLAGS: continue
                job = {'channel_id': channel, 'video_id': vid, 'type': event}
                key = channel + '|' + vid + '|' + event
                if baseline:
                    for kind in ({'scheduled', 'live', 'ended'} if event == 'ended' else {'scheduled', 'live'} if event == 'live' else {event}):
                        self._mark(state, {**job, 'type': kind})
                    continue
                table, flag = FLAGS[event]
                legacy_key = vid if table == 'videos' else channel + '|' + vid
                if state[table].get(legacy_key, {}).get(flag) or key in state['sent_events'] or key in state['outbox']: continue
                state['outbox'][key] = {**job, 'status': 'queued', 'created': int(time.time()), 'attempts': 0,
                                       'text': LABELS[event] + '\n\n' + entry.get('title', ''),
                                       'url': entry.get('url', ''), 'thumbnail_url': entry.get('thumbnail_url')}
            if baseline: state['channel_baselines'][channel] = True
            self._save(state)
        if success and not premiere_only:
            state['baseline_initialized'] = True
            state['last_successful_check'] = self.last_successful_check = int(time.time())
            self.last_error = None
        elif not premiere_only: self.last_error = 'youtube_check_failed'
        self._save(state)
        await self._deliver(context, state, MAX_EVENTS_PER_RUN - used, only_keys=only_keys)
        logger.info('run_complete queue=%s monitoring_ok=%s', self.queue_counts, success)
