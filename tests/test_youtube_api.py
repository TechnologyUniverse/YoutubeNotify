import unittest

from youtubenotify.youtube_api import YouTubeAPIClient


class FakeYouTubeAPIClient(YouTubeAPIClient):
    def __init__(self, responses):
        super().__init__("test-key")
        self.responses = responses
        self.requests = []

    def _request(self, resource, params):
        self.requests.append((resource, params))
        return self.responses[resource]


class YouTubeAPIClientTests(unittest.TestCase):
    def test_fetch_normalizes_video_and_live_statuses(self):
        client = FakeYouTubeAPIClient(
            {
                "channels": {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}
                    ]
                },
                "playlistItems": {
                    "items": [
                        {"contentDetails": {"videoId": "video-1"}},
                        {"contentDetails": {"videoId": "video-2"}},
                    ]
                },
                "videos": {
                    "items": [
                        {
                            "id": "video-1",
                            "snippet": {
                                "title": "Upload",
                                "publishedAt": "2026-07-07T10:00:00Z",
                                "liveBroadcastContent": "none",
                            },
                        },
                        {
                            "id": "video-2",
                            "snippet": {
                                "title": "Stream",
                                "publishedAt": "2026-07-07T11:00:00Z",
                                "liveBroadcastContent": "none",
                            },
                            "liveStreamingDetails": {"actualEndTime": "2026-07-07T12:00:00Z"},
                        },
                    ]
                },
            }
        )

        videos = client.fetch("UC123", 20)

        self.assertEqual([video["event_status"] for video in videos], ["video", "ended"])
        self.assertEqual(videos[0]["url"], "https://www.youtube.com/watch?v=video-1")
        self.assertGreater(videos[0]["published_timestamp"], 0)

    def test_uploads_playlist_is_cached(self):
        client = FakeYouTubeAPIClient(
            {
                "channels": {
                    "items": [
                        {"contentDetails": {"relatedPlaylists": {"uploads": "UU123"}}}
                    ]
                },
                "playlistItems": {"items": []},
            }
        )

        client.fetch("UC123")
        client.fetch("UC123")

        channel_requests = [request for request in client.requests if request[0] == "channels"]
        self.assertEqual(len(channel_requests), 1)

    def test_fetch_propagates_api_errors_and_counts_them(self):
        client = FakeYouTubeAPIClient({})

        with self.assertRaises(KeyError):
            client.fetch("UC123")

        self.assertEqual(client.total_failures, 1)
        self.assertEqual(client.consecutive_failures, 1)


if __name__ == "__main__":
    unittest.main()
