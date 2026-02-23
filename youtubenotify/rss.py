import feedparser
import time
import logging
import urllib.request
import ssl
import certifi
import re
import random


logger = logging.getLogger(__name__)


class RSSClient:
    def __init__(self, max_retries: int = 3, base_delay: float = 1.0):
        self.last_success: int | None = None
        self.consecutive_failures: int = 0
        self.total_failures: int = 0

        self.max_retries = max_retries
        self.base_delay = base_delay

    def _valid_channel_id(self, channel_id: str) -> bool:
        # Basic YouTube channel ID validation
        return isinstance(channel_id, str) and re.match(r"^UC[0-9A-Za-z_-]{20,}$", channel_id) is not None

    def fetch(self, channel_id: str):
        if not self._valid_channel_id(channel_id):
            logger.error(f"[RSS] invalid_channel_id | {channel_id}")
            return []

        url = f"https://www.youtube.com/feeds/videos.xml?channel_id={channel_id}"
        attempt = 0

        while attempt < self.max_retries:
            try:
                # Use explicit request with User-Agent to avoid HTML fallback from YouTube
                request = urllib.request.Request(
                    url,
                    headers={
                        "User-Agent": "YoutubeNotifyBot/1.5 (+production)",
                        "Accept": "application/atom+xml, application/xml;q=0.9, */*;q=0.8",
                        "Connection": "close",
                    },
                )
                context = ssl.create_default_context(cafile=certifi.where())
                with urllib.request.urlopen(request, timeout=10, context=context) as response:
                    content_type = response.headers.get("Content-Type", "")
                    if not any(ct in content_type for ct in ["xml", "atom"]):
                        raise Exception(f"type=rss_unexpected_content_type | value={content_type}")

                    raw_data = response.read()
                    feed = feedparser.parse(raw_data)

                if feed.bozo:
                    raise Exception(str(feed.bozo_exception))

                if not isinstance(feed.entries, list):
                    raise Exception("invalid_feed_entries_type")

                logger.info(
                    f"type=rss_success | url={url} | entries={len(feed.entries)}"
                )

                # Success
                self.last_success = int(time.time())
                self.consecutive_failures = 0

                return feed.entries

            except Exception as e:
                attempt += 1
                self.consecutive_failures += 1
                self.total_failures += 1

                logger.warning(
                    f"type=rss_retry | attempt={attempt} | max={self.max_retries} | url={url} | error={e}"
                )

                if attempt >= self.max_retries:
                    logger.error(
                        f"type=rss_failed_after_retries | url={url}"
                    )
                    return []

                # Exponential backoff with jitter
                jitter = random.uniform(0, 1)
                delay = self.base_delay * (2 ** (attempt - 1)) + jitter
                logger.info(
                    f"type=rss_backoff | delay={round(delay,2)}"
                )
                time.sleep(delay)