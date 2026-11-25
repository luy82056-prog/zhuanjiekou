import html
import json
import re
import subprocess
import tempfile
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


class ResolverError(RuntimeError):
    status = "暂不支持"


class BrowserCaptureRequired(ResolverError):
    status = "需要播放捕获"


class ProtectedMediaError(ResolverError):
    status = "受保护"


SHORT_HOSTS = {
    "xhslink.com", "v.douyin.com", "b23.tv", "weixin.qq.com",
    "vm.tiktok.com", "youtu.be", "t.cn",
}


def extract_share_url(value):
    text = (value or "").replace("\u200b", "").replace("\ufeff", "").strip()
    text = text.replace("\\&", "&").replace("\\_", "_")
    match = re.search(r"https?://[^\s<>\"'，。；、！!?？）)\]}〉》」』]+", text, flags=re.I)
    if not match:
        return text
    return match.group(0).rstrip("，。；;、!！?？）)]}〉》」』")


def clean_name(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "逐字稿")
    return value.strip(" .")[:160] or "逐字稿"


def redact_url(value):
    """Return a log-safe URL with secrets and query values removed."""
    parsed = urlparse(value or "")
    if not parsed.scheme or not parsed.netloc:
        return "<已隐藏链接>"
    return f"{parsed.scheme}://{parsed.netloc}{parsed.path}"


class MediaResolver:
    def __init__(self, yt_dlp_path):
        self.yt_dlp = Path(yt_dlp_path)

    @staticmethod
    def platform_name(url):
        host = (urlparse(url).hostname or "").lower()
        if "weixin.qq.com" in host or "wechat.com" in host:
            return "微信"
        if "xiaohongshu.com" in host or host.endswith("xhslink.com"):
            return "小红书"
        if "douyin.com" in host:
            return "抖音"
        if "bilibili.com" in host or host == "b23.tv":
            return "哔哩哔哩"
        if "youtube.com" in host or host == "youtu.be":
            return "YouTube"
        return host or "公开网页"

    @staticmethod
    def _request(url, timeout=25):
        request = Request(url, headers={
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/136 Safari/537.36"
            ),
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        })
        return urlopen(request, timeout=timeout)

    def normalize_url(self, value):
        url = extract_share_url(value)
        if not re.match(r"^https?://", url, re.I):
            raise ResolverError("没有找到完整的 http/https 链接。")
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        should_expand = host in SHORT_HOSTS or not parsed.path
        if not should_expand:
            return url
        try:
            with self._request(url, timeout=20) as response:
                final = response.geturl()
            return final if final.startswith(("http://", "https://")) else url
        except Exception:
            return url

    @staticmethod
    def _looks_auth_error(detail):
        text = (detail or "").lower()
        return any(marker in text for marker in (
            "http error 401", "http error 403", "sign in", "login", "log in",
            "cookies", "cookie", "captcha", "verification", "访问频繁", "登录",
            "验证", "风控", "forbidden", "unauthorized",
        ))

    @staticmethod
    def _looks_protected(detail):
        text = (detail or "").lower()
        return any(marker in text for marker in (
            "drm", "widevine", "fairplay", "playready", "encrypted media",
            "decodekey", "decode_key", "finder protocol",
        ))

    def resolve(self, value, cookie_arg_sets=(), playlist=False):
        url = self.normalize_url(value)
        host = (urlparse(url).hostname or "").lower()
        if host == "mp.weixin.qq.com":
            return self._resolve_wechat_article(url, cookie_arg_sets)
        return self._resolve_ytdlp(url, cookie_arg_sets, playlist)

    def _run_ytdlp(self, url, extra_args, playlist=False):
        command = [
            str(self.yt_dlp), "--no-warnings", "--dump-single-json", "--skip-download",
        ]
        if not playlist:
            command.append("--no-playlist")
        command.extend(extra_args)
        command.append(url)
        result = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            text=True, encoding="utf-8", errors="replace", timeout=180,
            creationflags=subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0,
        )
        if result.returncode:
            detail = (result.stderr or result.stdout or "无法识别这个链接").strip()
            line = next((x for x in reversed(detail.splitlines()) if x.strip()), detail)
            raise ResolverError(line.replace("ERROR: ", ""))
        return json.loads(result.stdout)

    def _resolve_ytdlp(self, url, cookie_arg_sets=(), playlist=False):
        attempts = [([], "直接支持"), (["--extractor-args", "generic:impersonate"], "浏览器仿真")]
        errors = []
        auth_failure = False
        for args, label in attempts:
            try:
                data = self._run_ytdlp(url, args, playlist)
                return self._info_from_ytdlp(data, url, args, label)
            except Exception as exc:
                detail = str(exc)
                errors.append(detail)
                auth_failure = auth_failure or self._looks_auth_error(detail)
                if self._looks_protected(detail):
                    raise ProtectedMediaError("页面使用 DRM 或加密媒体协议，当前版本不会绕过。")

        # Reading existing browser sessions is a fallback only for public pages
        # blocked by login/captcha/region checks, never the first request.
        if auth_failure:
            for source, cookie_args in cookie_arg_sets:
                if not cookie_args:
                    continue
                args = list(cookie_args) + ["--extractor-args", "generic:impersonate"]
                try:
                    data = self._run_ytdlp(url, args, playlist)
                    info = self._info_from_ytdlp(data, url, args, "浏览器登录态")
                    info["auth_source"] = source
                    return info
                except Exception as exc:
                    errors.append(f"{source}：{str(exc).splitlines()[-1]}")

        detail = errors[-1] if errors else "没有发现标准媒体或字幕。"
        if self._looks_protected(detail):
            raise ProtectedMediaError("页面使用 DRM 或加密媒体协议，当前版本不会绕过。")
        raise BrowserCaptureRequired(
            f"{self.platform_name(url)}未直接返回标准字幕或音轨，将打开专用浏览器监听页面播放。"
        )

    def _info_from_ytdlp(self, data, source_url, successful_args, label):
        entries = [entry for entry in (data.get("entries") or []) if isinstance(entry, dict)]
        if data.get("_type") == "playlist" or entries:
            items = [
                self._entry_info(entry, entry.get("webpage_url") or source_url, successful_args, label)
                for entry in entries
                if self._entry_has_media(entry)
            ]
            if not items:
                raise ResolverError("页面只包含图片或没有可处理的音视频。")
            first = dict(items[0])
            first.update({
                "title": data.get("title") or first["title"],
                "count": len(items), "items": items,
                "resolver_state": "多媒体待选择",
            })
            return first
        return self._entry_info(data, source_url, successful_args, label)

    @staticmethod
    def _entry_has_media(entry):
        if entry.get("url") or entry.get("formats"):
            formats = entry.get("formats") or []
            if not formats:
                return True
            return any(
                row.get("acodec") not in (None, "none") or row.get("vcodec") not in (None, "none")
                for row in formats if isinstance(row, dict)
            )
        return False

    def _entry_info(self, data, source_url, successful_args, label):
        subtitle = self.select_subtitle(data)
        return {
            "title": data.get("title") or data.get("id") or "逐字稿",
            "duration": data.get("duration"),
            "extractor": data.get("extractor_key") or data.get("extractor") or "通用网页",
            "count": 1,
            "source_url": data.get("webpage_url") or source_url,
            "download_url": data.get("webpage_url") or source_url,
            "cookie_args": list(successful_args),
            "http_headers": dict(data.get("http_headers") or {}),
            "subtitle": subtitle,
            "resolver_state": "可直接处理",
            "resolver_badge": label,
        }

    @staticmethod
    def select_subtitle(data):
        candidates = []
        for automatic, group in ((False, data.get("subtitles") or {}), (True, data.get("automatic_captions") or {})):
            for language, tracks in group.items():
                for track in tracks or []:
                    if not isinstance(track, dict) or not track.get("url"):
                        continue
                    lang = str(language).lower()
                    language_score = 40 if lang.startswith("zh") else (20 if lang not in {"live_chat"} else -100)
                    ext = str(track.get("ext") or "").lower()
                    ext_score = {"json3": 5, "vtt": 4, "srt": 3, "ttml": 2}.get(ext, 0)
                    candidates.append((
                        (100 if not automatic else 50) + language_score + ext_score,
                        {
                            "url": track["url"], "ext": ext, "language": language,
                            "automatic": automatic,
                            "http_headers": dict(track.get("http_headers") or data.get("http_headers") or {}),
                        },
                    ))
        return max(candidates, key=lambda item: item[0])[1] if candidates else None

    def _resolve_wechat_article(self, url, cookie_arg_sets):
        try:
            with self._request(url, timeout=30) as response:
                raw = response.read(8 * 1024 * 1024)
            page = raw.decode("utf-8", errors="replace")
        except (HTTPError, URLError, OSError) as exc:
            raise BrowserCaptureRequired("公众号文章无法直接读取，将在专用浏览器中打开。") from exc

        decoded = html.unescape(page).replace("\\/", "/").replace("\\u0026", "&")
        title_match = re.search(r'<meta[^>]+property=["\']og:title["\'][^>]+content=["\']([^"\']+)', decoded, re.I)
        if not title_match:
            title_match = re.search(r"var\s+msg_title\s*=\s*['\"](.+?)['\"]", decoded, re.S)
        title = clean_name(html.unescape(title_match.group(1)).strip()) if title_match else "微信公众号视频"

        urls = []
        patterns = (
            r"https?://mpvideo\.qpic\.cn/[^\s\"'<>]+",
            r"https?://[^\s\"'<>]*v\.qq\.com/[^\s\"'<>]+",
            r"https?://weixin\.qq\.com/sph/[^\s\"'<>]+",
            r"https?://channels\.weixin\.qq\.com/[^\s\"'<>]+",
        )
        for pattern in patterns:
            for found in re.findall(pattern, decoded, re.I):
                found = found.rstrip("\\,;)")
                if found not in urls:
                    urls.append(found)

        items = []
        for index, media_url in enumerate(urls, 1):
            host = (urlparse(media_url).hostname or "").lower()
            item_title = title if len(urls) == 1 else f"{title}（视频{index}）"
            if host == "mpvideo.qpic.cn":
                items.append({
                    "title": item_title, "duration": None,
                    "extractor": "微信公众号原生视频", "count": 1,
                    "source_url": url, "download_url": media_url, "media_url": media_url,
                    "cookie_args": [], "http_headers": {"Referer": url},
                    "subtitle": None, "resolver_state": "可直接处理", "resolver_badge": "直接支持",
                })
            elif "v.qq.com" in host:
                try:
                    info = self._resolve_ytdlp(media_url, cookie_arg_sets, False)
                    info["title"] = item_title
                    items.append(info)
                except ResolverError:
                    continue
            else:
                items.append({
                    "title": item_title, "duration": None,
                    "extractor": "微信视频号公开分享页", "count": 1,
                    "source_url": media_url, "download_url": media_url,
                    "cookie_args": [], "http_headers": {"Referer": url},
                    "subtitle": None, "resolver_state": "需要播放捕获", "resolver_badge": "浏览器辅助",
                })

        if not items:
            raise BrowserCaptureRequired("文章中的视频没有暴露标准媒体地址，需要在专用 Edge 中播放捕获。")
        if len(items) == 1:
            return items[0]
        result = dict(items[0])
        result.update({"title": title, "count": len(items), "items": items, "resolver_state": "多媒体待选择"})
        return result

    @staticmethod
    def _request_with_headers(url, headers=None, timeout=60):
        safe_headers = {
            key: value for key, value in (headers or {}).items()
            if key.lower() in {"user-agent", "referer", "origin", "authorization", "cookie"}
        }
        safe_headers.setdefault("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/136 Safari/537.36")
        return urlopen(Request(url, headers=safe_headers), timeout=timeout)

    def subtitle_to_text(self, subtitle, output_path):
        with self._request_with_headers(subtitle["url"], subtitle.get("http_headers")) as response:
            raw = response.read(50 * 1024 * 1024)
        text = raw.decode("utf-8-sig", errors="replace")
        cleaned = self.normalize_subtitle(text, subtitle.get("ext"))
        if len(cleaned.strip()) < 2:
            raise ResolverError("平台字幕为空，需回退到音频识别。")
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(cleaned, encoding="utf-8-sig")
        return output_path

    @staticmethod
    def normalize_subtitle(text, ext=""):
        lines = []
        if ext == "json3" or text.lstrip().startswith("{\"events\""):
            try:
                payload = json.loads(text)
                for event in payload.get("events") or []:
                    value = "".join(str(seg.get("utf8") or "") for seg in event.get("segs") or [])
                    if value.strip():
                        lines.append(value.strip())
            except Exception:
                lines = []
        if not lines:
            text = re.sub(r"<br\s*/?>", "\n", text, flags=re.I)
            text = re.sub(r"<[^>]+>", "", text)
            for raw in text.splitlines():
                line = html.unescape(raw).strip().lstrip("\ufeff")
                if not line or line.upper().startswith(("WEBVTT", "NOTE", "STYLE", "REGION")):
                    continue
                if re.match(r"^\d+$", line) or "-->" in line:
                    continue
                line = re.sub(r"^\[[^\]]{0,32}\]\s*", "", line)
                if line:
                    lines.append(line)
        unique = []
        for line in lines:
            line = re.sub(r"\s+", " ", line).strip()
            if line and (not unique or line != unique[-1]):
                unique.append(line)
        paragraphs, current = [], ""
        for line in unique:
            if current and len(current) + len(line) > 160:
                paragraphs.append(current.strip())
                current = ""
            current += line if not current else ("" if re.match(r"^[，。！？；：,.!?;:]", line) else " ") + line
            if len(current) >= 120 and re.search(r"[。！？.!?]$", current):
                paragraphs.append(current.strip())
                current = ""
        if current.strip():
            paragraphs.append(current.strip())
        return "\n\n".join(paragraphs).strip() + "\n"
