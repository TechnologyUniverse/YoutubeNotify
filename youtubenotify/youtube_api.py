import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime


logger = logging.getLogger(__name__)


class YouTubeAPIClient:
    BASE_URL = "https://www.googleapis.com/youtube/v3"

    def __init__(self, api_key: str, max_retries: int = 3, base_delay: float = 1.0):
        self.api_key = api_key
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.uploads_playlists: dict[str, str] = {}
        self.last_success: int | None = None
        self.consecutive_failures = 0
        self.total_failures = 0

    def _request(self, resource: str, params: dict) -> dict:
        query = urllib.parse.urlencode({**params, "key": self.api_key})
        url = f"{self.BASE_URL}/{resource}?{query}"

        for attempt in range(1, self.max_retries + 1):
            try:
                request = urllib.request.Request(
                    url,
                    headers={"Accept": "application/json", "User-Agent": "YoutubeNotifyBot/2.0"},
                )
                with urllib.request.urlopen(request, timeout=15) as response:
                    payload = json.load(response)
                if not isinstance(payload, dict):
                    raise ValueError("invalid_api_response")
                return payload
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 500, 502, 503, 504):
                    raise RuntimeError(f"youtube_api_http_error:{exc.code}") from exc
                error = f"http_{exc.code}"
            except (urllib.error.URLError, TimeoutError, ValueError) as exc:
                error = str(exc)

            if attempt == self.max_retries:
                raise RuntimeError(f"youtube_api_failed:{error}")

            delay = self.base_delay * (2 ** (attempt - 1)) + random.uniform(0, 1)
            logger.warning(
                "type=youtube_api_retry | resource=%s | attempt=%s | delay=%.2f | error=%s",
                resource,
                attempt,
                delay,
                error,
            )
            time.sleep(delay)

        raise RuntimeError("youtube_api_failed")

    def _uploads_playlist(self, channel_id: str) -> str:
        cached = self.uploads_playlists.get(channel_id)
        if cached:
            return cached

        payload = self._request(
            "channels",
            {"part": "contentDetails", "id": channel_id, "maxResults": 1},
        )
        items = payload.get("items", [])
        if not items:
            raise RuntimeError(f"youtube_channel_not_found:{channel_id}")

        try:
            playlist_id = items[0]["contentDetails"]["relatedPlaylists"]["uploads"]
        except (KeyError, TypeError) as exc:
            raise RuntimeError(f"youtube_uploads_playlist_missing:{channel_id}") from exc

        self.uploads_playlists[channel_id] = playlist_id
        return playlist_id

    @staticmethod
    def _timestamp(value: str | None) -> float:
        if not value:
            return 0
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            return 0

    def fetch(self, channel_id: str, max_results: int = 20) -> list[dict]:
        try:
            playlist_id = self._uploads_playlist(channel_id)
            playlist = self._request(
                "playlistItems",
                {
                    "part": "contentDetails,snippet",
                    "playlistId": playlist_id,
                    "maxResults": min(max(max_results, 1), 50),
                },
            )
            video_ids = [
                item.get("contentDetails", {}).get("videoId")
                for item in playlist.get("items", [])
            ]
            video_ids = [video_id for video_id in video_ids if video_id]
            if not video_ids:
                self.last_success = int(time.time())
                self.consecutive_failures = 0
                return []

            videos = self._request(
                "videos",
                {
                    "part": "snippet,liveStreamingDetails",
                    "id": ",".join(video_ids),
                    "maxResults": len(video_ids),
                },
            )

            result = []
            for item in videos.get("items", []):
                snippet = item.get("snippet", {})
                live_details = item.get("liveStreamingDetails", {})
                broadcast = snippet.get("liveBroadcastContent", "none")
                if live_details.get("actualEndTime"):
                    event_status = "ended"
                elif broadcast == "live":
                    event_status = "live"
                elif broadcast == "upcoming":
                    event_status = "upcoming"
                else:
                    event_status = "video"

                video_id = item.get("id")
                if not video_id:
                    continue
                published_at = snippet.get("publishedAt")
                thumbnails = snippet.get("thumbnails", {})
                thumbnail_url = next(
                    (
                        thumbnails[size].get("url")
                        for size in ("maxres", "standard", "high", "medium", "default")
                        if thumbnails.get(size, {}).get("url")
                    ),
                    None,
                )
                result.append(
                    {
                        "video_id": video_id,
                        "title": snippet.get("title", ""),
                        "url": f"https://www.youtube.com/watch?v={video_id}",
                        "published_at": published_at,
                        "published_timestamp": self._timestamp(published_at),
                        "event_status": event_status,
                        "thumbnail_url": thumbnail_url,
                    }
                )

            self.last_success = int(time.time())
            self.consecutive_failures = 0
            logger.info(
                "type=youtube_api_success | channel=%s | videos=%s",
                channel_id,
                len(result),
            )
            return result
        except Exception:
            self.consecutive_failures += 1
            self.total_failures += 1
            logger.exception("type=youtube_api_fetch_failed | channel=%s", channel_id)
            raise
