import unittest

from media_resolver import MediaResolver, extract_share_url, redact_url


class MediaResolverTests(unittest.TestCase):
    def setUp(self):
        self.resolver = MediaResolver("yt-dlp.exe")

    def test_extract_url_from_chinese_share_text(self):
        text = "复制这段分享文案 https://example.com/share/abc123，打开后观看。"
        self.assertEqual(extract_share_url(text), "https://example.com/share/abc123")

    def test_redact_url_removes_signed_query(self):
        value = "https://cdn.example.com/a/video.m3u8?token=secret&sign=private"
        self.assertEqual(redact_url(value), "https://cdn.example.com/a/video.m3u8")

    def test_manual_chinese_subtitle_is_preferred(self):
        subtitle = self.resolver.select_subtitle({
            "subtitles": {"zh-Hans": [{"url": "https://example.com/manual.vtt", "ext": "vtt"}]},
            "automatic_captions": {"zh": [{"url": "https://example.com/auto.json3", "ext": "json3"}]},
        })
        self.assertEqual(subtitle["url"], "https://example.com/manual.vtt")
        self.assertFalse(subtitle["automatic"])

    def test_normalize_vtt_removes_timestamps_and_duplicates(self):
        source = "WEBVTT\n\n00:00:00.000 --> 00:00:01.000\n你好世界。\n\n00:00:01.000 --> 00:00:02.000\n你好世界。\n继续讲解。\n"
        result = self.resolver.normalize_subtitle(source, "vtt")
        self.assertNotIn("-->", result)
        self.assertEqual(result.count("你好世界"), 1)
        self.assertIn("继续讲解", result)

    def test_normalize_json3(self):
        source = '{"events":[{"segs":[{"utf8":"第一句。"}]},{"segs":[{"utf8":"第二句。"}]}]}'
        result = self.resolver.normalize_subtitle(source, "json3")
        self.assertIn("第一句", result)
        self.assertIn("第二句", result)


if __name__ == "__main__":
    unittest.main()
