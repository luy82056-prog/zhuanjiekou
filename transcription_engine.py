import os
import re
import subprocess
import ctypes
from pathlib import Path


class TranscriptionError(RuntimeError):
    pass


class TranscriptionCancelled(TranscriptionError):
    pass


class SenseVoiceEngine:
    """Offline SenseVoiceSmall Q8 pipeline with CUDA-to-CPU fallback."""

    MODEL_NAME = "SenseVoice Q8"
    REQUIRED = (
        "cpu/llama-funasr-sensevoice.exe",
        "models/sensevoice-small-q8.gguf",
        "models/fsmn-vad.gguf",
    )

    def __init__(self, ffmpeg_path, runtime_dir):
        self.ffmpeg_path = Path(ffmpeg_path)
        self.runtime_dir = Path(runtime_dir)
        self.cpu_binary = self.runtime_dir / "cpu" / "llama-funasr-sensevoice.exe"
        self.cuda_binary = self.runtime_dir / "cuda" / "llama-funasr-sensevoice.exe"
        self.model_path = self.runtime_dir / "models" / "sensevoice-small-q8.gguf"
        self.vad_path = self.runtime_dir / "models" / "fsmn-vad.gguf"

    def missing_files(self):
        missing = []
        if not self.ffmpeg_path.is_file():
            missing.append(str(self.ffmpeg_path))
        for relative in self.REQUIRED:
            path = self.runtime_dir / Path(relative)
            if not path.is_file():
                missing.append(str(path))
        return missing

    @property
    def available(self):
        return not self.missing_files()

    @property
    def backend_summary(self):
        if self.cuda_ready:
            return "自动（RTX CUDA 优先，失败转 CPU AVX2）"
        if self.cuda_binary.is_file():
            return "CPU AVX2（未检测到 CUDA 13 运行库）"
        return "CPU AVX2"

    @property
    def cuda_ready(self):
        if os.name != "nt" or not self.cuda_binary.is_file():
            return False
        try:
            ctypes.WinDLL("cublas64_13.dll")
            return True
        except (OSError, AttributeError):
            return False

    @staticmethod
    def load_glossary(path):
        replacements = []
        path = Path(path) if path else None
        if not path or not path.is_file():
            return replacements
        for raw in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith(("#", ";")):
                continue
            pair = None
            for marker in ("=>", "->", "→", "="):
                if marker in line:
                    pair = line.split(marker, 1)
                    break
            if not pair:
                continue
            wrong, right = (item.strip() for item in pair)
            if wrong and right and wrong != right:
                replacements.append((wrong, right))
        # Apply longer phrases first so a short rule cannot damage a long one.
        replacements.sort(key=lambda item: len(item[0]), reverse=True)
        return replacements

    @staticmethod
    def _strip_runtime_noise(text):
        text = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", text or "")
        text = re.sub(r"<\|[^|<>]+\|>", "", text)
        kept = []
        runtime_prefixes = (
            "ggml_", "llama_", "main:", "system_info:", "load_",
            "sensevoice_", "vad_", "build:", "model ", "backend ",
        )
        for raw in text.splitlines():
            line = raw.strip()
            if not line:
                continue
            lower = line.lower()
            if lower.startswith(runtime_prefixes):
                continue
            if re.fullmatch(r"[\-=*_]{3,}", line):
                continue
            kept.append(line)
        return "\n".join(kept)

    @staticmethod
    def _apply_replacements(text, replacements):
        for wrong, right in replacements:
            flags = re.IGNORECASE if wrong.isascii() else 0
            text = re.sub(re.escape(wrong), lambda _match, value=right: value, text, flags=flags)
        return text

    @staticmethod
    def _normalize_itn(text):
        # Keep the rules conservative: only normalize already-recognized Arabic
        # numbers instead of guessing the value of spoken Chinese numerals.
        text = re.sub(r"百分之\s*(\d+(?:\.\d+)?)", r"\1%", text)
        text = re.sub(r"(\d+(?:\.\d+)?)\s*[％%]", r"\1%", text)
        text = re.sub(r"(?<=\d)\s+(?=[年月日号时分秒])", "", text)
        text = re.sub(r"(?<=[年月日号时分秒])\s+(?=\d)", "", text)
        text = re.sub(r"(?<=\d)\s+(?=[:：.]\s*\d)", "", text)
        text = re.sub(r"(?<=\d)\s+(?=\d)", "", text)
        return text

    @staticmethod
    def _normalize_punctuation(text):
        text = text.replace("\r", "\n")
        text = re.sub(r"[ \t\u3000]+", " ", text)
        text = re.sub(r" *\n+ *", "\n", text)
        text = re.sub(r"([，。！？；：、,.!?;:])\1+", r"\1", text)
        text = re.sub(r"\s+([，。！？；：、,.!?;:])", r"\1", text)
        text = re.sub(r"([（【《“‘])\s+", r"\1", text)
        text = re.sub(r"\s+([）】》”’])", r"\1", text)
        return text.strip()

    @staticmethod
    def _deduplicate(lines):
        result = []
        previous_key = ""
        for line in lines:
            key = re.sub(r"[\s，。！？；：、,.!?;:]", "", line).lower()
            if not key or key == previous_key:
                continue
            result.append(line)
            previous_key = key
        return result

    @classmethod
    def _sentence_units(cls, text):
        units = []
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = re.split(r"(?<=[。！？!?；;])", line)
            for part in parts:
                part = part.strip()
                if part:
                    units.append(part)
        units = cls._deduplicate(units)
        normalized = []
        for unit in units:
            if re.search(r"[\u4e00-\u9fff]", unit) and not re.search(r"[。！？!?；;：:]$", unit):
                unit += "。"
            normalized.append(unit)
        return normalized

    @classmethod
    def _paragraphs(cls, units, target_min=120, target_max=180):
        paragraphs = []
        current = ""
        for unit in units:
            if len(unit) > target_max:
                if current:
                    paragraphs.append(current)
                    current = ""
                for start in range(0, len(unit), target_max):
                    paragraphs.append(unit[start:start + target_max])
                continue
            candidate = current + unit
            if current and len(candidate) > target_max:
                paragraphs.append(current)
                current = unit
            else:
                current = candidate
            if len(current) >= target_min and re.search(r"[。！？!?；;]$", current):
                paragraphs.append(current)
                current = ""
        if current:
            paragraphs.append(current)
        return paragraphs

    @classmethod
    def format_text(cls, raw_text, glossary=()):
        text = cls._strip_runtime_noise(raw_text)
        text = cls._apply_replacements(text, glossary)
        text = cls._normalize_itn(text)
        text = cls._normalize_punctuation(text)
        units = cls._sentence_units(text)
        paragraphs = cls._paragraphs(units)
        return "\n\n".join(paragraphs).strip() + ("\n" if paragraphs else "")

    @staticmethod
    def _run_process(command, cwd, process_callback, cancel_event, stdout_pipe=True):
        process = subprocess.Popen(
            command,
            cwd=str(cwd),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE if stdout_pipe else subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
        )
        process_callback(process)
        try:
            while True:
                if cancel_event.is_set():
                    try:
                        process.terminate()
                    except OSError:
                        pass
                    try:
                        process.communicate(timeout=5)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.communicate()
                    raise TranscriptionCancelled("逐字稿任务已取消。")
                try:
                    stdout, stderr = process.communicate(timeout=0.2)
                    return process.returncode, stdout or b"", stderr or b""
                except subprocess.TimeoutExpired:
                    continue
        finally:
            process_callback(None)

    def _recognize(self, binary, backend, wav_path, process_callback, cancel_event):
        command = [
            str(binary),
            "-m", str(self.model_path),
            "--vad", str(self.vad_path),
            "-a", str(wav_path),
            "--backend", backend,
        ]
        code, stdout, stderr = self._run_process(
            command, binary.parent, process_callback, cancel_event, stdout_pipe=True
        )
        return (
            code,
            stdout.decode("utf-8", errors="replace"),
            stderr.decode("utf-8", errors="replace"),
        )

    def transcribe(
        self,
        source,
        output_dir,
        work_dir,
        glossary_path=None,
        progress_callback=lambda *_args: None,
        process_callback=lambda _process: None,
        cancel_event=None,
    ):
        cancel_event = cancel_event or _NeverCancelled()
        source = Path(source)
        output_dir = Path(output_dir)
        work_dir = Path(work_dir)
        if not source.is_file():
            raise TranscriptionError(f"找不到待识别音频：{source}")
        missing = self.missing_files()
        if missing:
            raise TranscriptionError("SenseVoice 离线引擎不完整：\n" + "\n".join(missing))

        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)
        wav_path = work_dir / "input.wav"
        progress_callback("音频解码", 8, "正在转换为 16kHz 单声道")
        ffmpeg_command = [
            str(self.ffmpeg_path), "-y", "-hide_banner", "-loglevel", "error",
            "-i", str(source), "-vn", "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", str(wav_path),
        ]
        code, _stdout, stderr = self._run_process(
            ffmpeg_command, self.ffmpeg_path.parent, process_callback, cancel_event, stdout_pipe=False
        )
        if code:
            detail = stderr.decode("utf-8", errors="replace").strip()[-1000:]
            raise TranscriptionError("FFmpeg 无法读取该音视频文件。" + ("\n" + detail if detail else ""))

        progress_callback("检测人声", 22, "FSMN-VAD 正在过滤静音并切分长音频")
        attempts = []
        if self.cuda_ready:
            attempts.append((self.cuda_binary, "cuda", "RTX CUDA"))
        attempts.append((self.cpu_binary, "cpu", "CPU AVX2"))

        raw_text = ""
        selected_backend = ""
        errors = []
        for index, (binary, backend, label) in enumerate(attempts):
            if index:
                progress_callback("切换后端", 28, "CUDA 不可用，已自动改用 CPU AVX2")
            progress_callback("语音识别", 34, f"SenseVoice Q8 · {label}")
            code, stdout, stderr = self._recognize(
                binary, backend, wav_path, process_callback, cancel_event
            )
            candidate = self._strip_runtime_noise(stdout)
            if code == 0 and candidate.strip():
                raw_text = stdout
                selected_backend = label
                break
            detail = (stderr or stdout).strip()[-1400:]
            errors.append(f"{label}：退出码 {code}" + (f"\n{detail}" if detail else ""))
        if not raw_text:
            raise TranscriptionError("SenseVoice 转写失败。\n" + "\n\n".join(errors))

        progress_callback("文本整理", 88, "正在执行术语纠错、数字规整和智能分段")
        glossary = self.load_glossary(glossary_path)
        final_text = self.format_text(raw_text, glossary)
        if not final_text.strip():
            raise TranscriptionError("SenseVoice 已完成识别，但没有生成有效文字。")

        destination = output_dir / (source.stem + ".txt")
        suffix = 2
        while destination.exists():
            destination = output_dir / f"{source.stem}（{suffix}）.txt"
            suffix += 1
        destination.write_text(final_text, encoding="utf-8-sig")
        progress_callback("保存 TXT", 100, destination.name)
        return {
            "output": destination,
            "backend": selected_backend,
            "text_length": len(final_text.strip()),
            "glossary_rules": len(glossary),
        }


class _NeverCancelled:
    @staticmethod
    def is_set():
        return False
