import json
import os
import re
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse
from urllib.request import urlopen


class BrowserCaptureError(RuntimeError):
    pass


class BrowserCaptureProtected(BrowserCaptureError):
    pass


class BrowserMediaCapture:
    MEDIA_EXTENSIONS = {".m3u8", ".mpd", ".mp4", ".m4a", ".mp3", ".aac", ".webm", ".wav", ".ogg", ".opus"}
    SEGMENT_EXTENSIONS = {".ts", ".m4s", ".cmfv", ".cmfa"}

    def __init__(self, profile_dir):
        self.profile_dir = Path(profile_dir)
        self.process = None

    @staticmethod
    def _edge_path():
        candidates = (
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        )
        return next((path for path in candidates if path.is_file()), None)

    @staticmethod
    def _free_port():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]

    @staticmethod
    def _send(ws, counter, method, params=None):
        ws.send(json.dumps({"id": counter, "method": method, "params": params or {}}))

    @staticmethod
    def _candidate(url, mime, request_headers, page_url, title):
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return None
        ext = Path(parsed.path).suffix.lower()
        lower_mime = (mime or "").lower()
        if ext in BrowserMediaCapture.SEGMENT_EXTENSIONS:
            return None
        is_manifest = ext in {".m3u8", ".mpd"} or "mpegurl" in lower_mime or "dash+xml" in lower_mime
        is_media = ext in BrowserMediaCapture.MEDIA_EXTENSIONS or lower_mime.startswith(("audio/", "video/"))
        if not (is_manifest or is_media):
            return None
        lower_url = url.lower()
        if "finder.video.qq.com" in lower_url or "decodekey=" in lower_url or "decode_key=" in lower_url:
            raise BrowserCaptureProtected("该视频号使用受保护的 Finder 媒体协议，当前版本不会解密。")
        headers = {
            key: value for key, value in (request_headers or {}).items()
            if key.lower() in {"user-agent", "referer", "origin", "cookie", "authorization"}
        }
        score = 100 if is_manifest else (90 if lower_mime.startswith("audio/") or ext in {".m4a", ".mp3", ".aac", ".wav", ".ogg", ".opus"} else 80)
        return {
            "title": title or "浏览器捕获媒体", "duration": None,
            "extractor": "浏览器公开媒体监听", "count": 1,
            "source_url": page_url, "download_url": url, "media_url": url,
            "cookie_args": [], "http_headers": headers, "subtitle": None,
            "resolver_state": "可直接处理", "resolver_badge": "浏览器辅助", "_score": score,
        }

    def capture(self, url, cancel_event, progress_callback=None, timeout=90):
        try:
            import websocket
        except ImportError as exc:
            raise BrowserCaptureError("缺少浏览器监听组件 websocket-client，请重新安装或修复转接口。") from exc
        edge = self._edge_path()
        if edge is None:
            raise BrowserCaptureError("没有找到 Microsoft Edge，无法启动浏览器辅助解析。")
        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port = self._free_port()
        command = [
            str(edge), f"--remote-debugging-port={port}",
            "--remote-allow-origins=*", f"--user-data-dir={self.profile_dir}",
            "--no-first-run", "--disable-background-mode", "--new-window", url,
        ]
        self.process = subprocess.Popen(command)
        deadline = time.time() + timeout
        target = None
        if progress_callback:
            progress_callback("等待播放捕获", 15, "请在打开的 Edge 中播放目标视频")
        while time.time() < deadline and not cancel_event.is_set():
            try:
                with urlopen(f"http://127.0.0.1:{port}/json/list", timeout=2) as response:
                    targets = json.loads(response.read().decode("utf-8"))
                pages = [row for row in targets if row.get("type") == "page" and row.get("webSocketDebuggerUrl")]
                target = next((row for row in pages if (urlparse(url).hostname or "") in row.get("url", "")), None)
                target = target or (pages[0] if pages else None)
                if target:
                    break
            except Exception:
                pass
            time.sleep(0.4)
        if cancel_event.is_set():
            raise BrowserCaptureError("浏览器辅助解析已取消。")
        if not target:
            raise BrowserCaptureError("无法连接专用 Edge 的网络监听端口。")

        ws = websocket.create_connection(target["webSocketDebuggerUrl"], timeout=1)
        self._send(ws, 1, "Network.enable", {"maxTotalBufferSize": 1048576})
        requests, candidates, first_found = {}, {}, None
        try:
            while time.time() < deadline and not cancel_event.is_set():
                try:
                    message = json.loads(ws.recv())
                except Exception as exc:
                    if "timed out" in str(exc).lower():
                        continue
                    raise
                method = message.get("method")
                params = message.get("params") or {}
                if method == "Network.requestWillBeSent":
                    request = params.get("request") or {}
                    requests[params.get("requestId")] = request.get("headers") or {}
                elif method == "Network.responseReceived":
                    response = params.get("response") or {}
                    try:
                        item = self._candidate(
                            response.get("url") or "", response.get("mimeType") or "",
                            requests.get(params.get("requestId"), {}), url, target.get("title") or "",
                        )
                    except BrowserCaptureProtected:
                        raise
                    if item:
                        key = item["media_url"].split("?", 1)[0]
                        if key not in candidates or item["_score"] > candidates[key]["_score"]:
                            candidates[key] = item
                        first_found = first_found or time.time()
                        if progress_callback:
                            progress_callback("发现媒体", 80, "已捕获标准音视频请求，正在确认")
                if first_found and time.time() - first_found >= 3:
                    break
        finally:
            try:
                ws.close()
            except Exception:
                pass
        if cancel_event.is_set():
            raise BrowserCaptureError("浏览器辅助解析已取消。")
        if not candidates:
            raise BrowserCaptureError("90秒内没有捕获标准媒体。请确认视频已播放；受保护内容无法处理。")
        result = sorted(candidates.values(), key=lambda row: row["_score"], reverse=True)
        for row in result:
            row.pop("_score", None)
        return result

    def close(self):
        process = self.process
        self.process = None
        if process is None or process.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.terminate()
        except Exception:
            pass

