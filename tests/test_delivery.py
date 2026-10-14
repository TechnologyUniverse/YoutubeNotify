import os, tempfile, unittest
from unittest.mock import AsyncMock
from types import SimpleNamespace
os.environ.update(TELEGRAM_TOKEN='dummy', YOUTUBE_API_KEY='dummy',BOT_MODE='polling',YOUTUBE_CHANNEL_IDS='channel',TG_CHANNELS='channel:-1001',ADMIN_IDS='1')
from youtubenotify import engine as m
from youtubenotify.state import StateManager
from telegram.error import RetryAfter, TimedOut

class DeliveryTests(unittest.IsolatedAsyncioTestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
  self.s=StateManager(self.tmp.name+'/state.json');self.s.save({'schema_version':2,'baseline_initialized':True})
  self.e=m.Engine(self.s);self.entries=[{'video_id':'new','event_status':'video','published_timestamp':1,'title':'New','url':'https://youtube.com/watch?v=new'}]
  self.e.youtube.fetch=lambda *a:self.entries
  self.e._safe_send=AsyncMock(return_value=123)
 def state(self):return self.s.load()
 async def test_confirmed_delivery_marks_and_no_repeat(self):
  await self.e._run_internal(None);await self.e._run_internal(None)
  self.assertEqual(self.e._safe_send.await_count,1);self.assertEqual(self.state()['history'][0]['message_id'],123)
 async def test_rejected_not_marked_published(self):
  self.e._safe_send.side_effect=m.DeliveryRejected()
  await self.e._run_internal(None);await self.e._run_internal(None)
  self.assertFalse(self.state()['videos'].get('new',{}).get('published'));self.assertEqual(next(iter(self.state()['outbox'].values()))['status'],'failed')
 async def test_ambiguous_not_retried_after_restart(self):
  self.e._safe_send.side_effect=m.DeliveryUncertain()
  await self.e._run_internal(None)
  other=m.Engine(self.s);other.youtube.fetch=lambda *a:self.entries;other._safe_send=AsyncMock()
  await other._run_internal(None);other._safe_send.assert_not_awaited()
 async def test_limit_retains_remaining_event(self):
  self.entries += [{**self.entries[0],'video_id':str(i),'published_timestamp':i} for i in range(1,m.MAX_EVENTS_PER_RUN+1)]
  await self.e._run_internal(None);self.assertEqual(len(self.state()['outbox']),1)
  await self.e._run_internal(None);self.assertEqual(len(self.state()['outbox']),0);self.assertEqual(self.e._safe_send.await_count,len(self.entries))
 async def test_rate_limit_retries_persisted_queue(self):
  self.e._safe_send.side_effect=RetryAfter(5);await self.e._run_internal(None)
  d=self.state();job=next(iter(d['outbox'].values()));self.assertEqual(job['status'],'queued');job['next_attempt']=0;self.s.save(d)
  self.e._safe_send.side_effect=None;await self.e._run_internal(None);self.assertFalse(self.state()['outbox'])
 async def test_crash_in_sending_quarantines(self):
  self.e._safe_send.side_effect=RetryAfter(5);await self.e._run_internal(None)
  d=self.state();next(iter(d['outbox'].values()))['status']='sending';self.s.save(d)
  await self.e._run_internal(None);self.assertEqual(next(iter(self.state()['outbox'].values()))['status'],'uncertain')
 async def test_baseline_does_not_publish_old(self):
  self.s.save({});await self.e._run_internal(None);self.e._safe_send.assert_not_awaited();self.assertTrue(self.state()['baseline_initialized'])
 async def test_silent_preserves_queue(self):
  old=m.SILENT_MODE;m.SILENT_MODE=True
  try:await self.e._run_internal(None)
  finally:m.SILENT_MODE=old
  self.assertEqual(len(self.state()['outbox']),1);self.e._safe_send.assert_not_awaited()
 async def test_network_error_does_not_retry_send(self):
  e=m.Engine(self.s);e.last_telegram_send=0
  bot=SimpleNamespace(send_message=AsyncMock(side_effect=TimedOut()),send_photo=AsyncMock())
  with self.assertRaises(m.DeliveryUncertain):await e._safe_send(SimpleNamespace(bot=bot),-1,'x','https://youtube.com')
  bot.send_message.assert_awaited_once()
 def test_corrupt_state_stops(self):
  with open(self.s.file_path,'w') as f:f.write('{')
  with self.assertRaises(ValueError):self.s.load()
