import base64
import json
import os
import subprocess
import threading
from pathlib import Path


class RapidOCRError(RuntimeError):
    pass


class RapidOCREngine:
    """Persistent pipe client for Umi-OCR's RapidOCR-json plugin."""

    def __init__(self, engine_dir):
        self.engine_dir = Path(engine_dir)
        self.exe = self.engine_dir / "RapidOCR-json.exe"
        self.process = None
        self.lock = threading.Lock()

    @property
    def available(self):
        return self.exe.is_file() and (self.engine_dir / "models").is_dir()

    def _start(self):
        if self.process is not None and self.process.poll() is None:
            return
        if not self.available:
            raise RapidOCRError("RapidOCR 离线引擎文件不完整。")
        command = [
            str(self.exe),
            "--models=models",
            "--det=ch_PP-OCRv4_det_infer.onnx",
            "--cls=ch_ppocr_mobile_v2.0_cls_infer.onnx",
            "--rec=rec_ch_PP-OCRv4_infer.onnx",
            "--keys=dict_chinese.txt",
            "--doAngle=1",
            "--mostAngle=1",
            "--maxSideLen=4096",
            f"--numThread={max(1, min(8, os.cpu_count() or 4))}",
            "--ensureAscii=1",
        ]
        startupinfo = None
        creationflags = 0
        if os.name == "nt":
            startupinfo = subprocess.STARTUPINFO()
            startupinfo.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            startupinfo.wShowWindow = subprocess.SW_HIDE
            creationflags = subprocess.CREATE_NO_WINDOW
        self.process = subprocess.Popen(
            command,
            cwd=str(self.engine_dir),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            startupinfo=startupinfo,
            creationflags=creationflags,
        )
        while True:
            if self.process.poll() is not None:
                self.process = None
                raise RapidOCRError("RapidOCR 初始化失败。")
            line = self.process.stdout.readline().decode("utf-8", errors="ignore")
            if "OCR init completed." in line:
                return

    def recognize(self, image_path):
        with self.lock:
            self._start()
            payload = {
                "image_base64": base64.b64encode(Path(image_path).read_bytes()).decode("ascii")
            }
            try:
                encoded = (json.dumps(payload, ensure_ascii=True) + "\n").encode("utf-8")
                self.process.stdin.write(encoded)
                self.process.stdin.flush()
                raw = self.process.stdout.readline().decode("utf-8", errors="replace")
                result = json.loads(raw)
            except Exception as exc:
                self.close()
                raise RapidOCRError(f"OCR 进程通信失败：{exc}") from exc
            code = result.get("code")
            if code == 100:
                lines = [
                    str(item.get("text", "")).strip()
                    for item in result.get("data", [])
                    if str(item.get("text", "")).strip()
                ]
                return "\n".join(lines)
            if code == 101:
                return ""
            raise RapidOCRError(str(result.get("data") or f"识别失败（错误码 {code}）"))

    def close(self):
        process, self.process = self.process, None
        if process is None:
            return
        try:
            process.terminate()
            process.wait(timeout=2)
        except Exception:
            try:
                process.kill()
            except Exception:
                pass
