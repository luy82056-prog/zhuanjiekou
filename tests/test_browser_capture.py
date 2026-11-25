import unittest

from browser_capture import BrowserCaptureProtected, BrowserMediaCapture


class BrowserCaptureTests(unittest.TestCase):
    def test_accepts_standard_hls_and_preserves_temporary_headers(self):
        item = BrowserMediaCapture._candidate(
            "https://cdn.example.com/master.m3u8?token=temporary",
            "application/vnd.apple.mpegurl",
            {"Referer": "https://example.com/post", "Cookie": "session=value"},
            "https://example.com/post",
            "示例视频",
        )
        self.assertEqual(item["media_url"], "https://cdn.example.com/master.m3u8?token=temporary")
        self.assertEqual(item["http_headers"]["Cookie"], "session=value")

    def test_rejects_individual_segments(self):
        item = BrowserMediaCapture._candidate(
            "https://cdn.example.com/segment-001.ts", "video/mp2t", {},
            "https://example.com/post", "示例视频",
        )
        self.assertIsNone(item)

    def test_rejects_protected_finder_protocol(self):
        with self.assertRaises(BrowserCaptureProtected):
            BrowserMediaCapture._candidate(
                "https://finder.video.qq.com/path/video.mp4", "video/mp4", {},
                "https://channels.weixin.qq.com/", "视频号",
            )


if __name__ == "__main__":
    unittest.main()
