import tempfile
import unittest
from pathlib import Path

from transcription_engine import SenseVoiceEngine


class SenseVoiceTextTests(unittest.TestCase):
    def test_glossary_and_text_cleanup(self):
        with tempfile.TemporaryDirectory() as folder:
            glossary = Path(folder) / "terms.txt"
            glossary.write_text(
                "# 课程术语\n生事维斯 → SenseVoice\n小鹅通课成 = 小鹅通课程\n",
                encoding="utf-8-sig",
            )
            rules = SenseVoiceEngine.load_glossary(glossary)
            raw = (
                "<|zh|><|NEUTRAL|><|Speech|>今天使用生事维斯识别小鹅通课成 百分之 50。。\n"
                "今天使用生事维斯识别小鹅通课成 百分之 50。。\n"
                "下一部分继续讲解"
            )
            result = SenseVoiceEngine.format_text(raw, rules)
            self.assertNotIn("<|", result)
            self.assertIn("SenseVoice", result)
            self.assertIn("小鹅通课程", result)
            self.assertIn("50%", result)
            self.assertEqual(result.count("今天使用"), 1)
            self.assertTrue(result.endswith("。\n"))

    def test_missing_runtime_is_reported(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            engine = SenseVoiceEngine(root / "ffmpeg.exe", root / "funasr")
            missing = engine.missing_files()
            self.assertEqual(len(missing), 4)
            self.assertFalse(engine.available)


if __name__ == "__main__":
    unittest.main()
