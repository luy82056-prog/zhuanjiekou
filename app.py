import json
import os
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import tkinter as tk
import uuid
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, unquote, urlencode, urlparse
from urllib.request import Request, urlopen

from conversion_engine import (
    FORMAT_LABELS,
    SUPPORTED_EXTENSIONS,
    ConversionCancelled,
    ConversionRouter,
    detect_engines,
    engine_summary,
    source_kind,
)
from rapid_ocr_engine import RapidOCREngine, RapidOCRError
from browser_capture import (
    BrowserCaptureError,
    BrowserCaptureProtected,
    BrowserMediaCapture,
)
from media_resolver import (
    BrowserCaptureRequired,
    MediaResolver,
    ProtectedMediaError,
    ResolverError,
    extract_share_url,
    redact_url,
)
from transcription_engine import (
    SenseVoiceEngine,
    TranscriptionCancelled,
)

try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    DND_AVAILABLE = True
    AppTk = TkinterDnD.Tk
except ImportError:
    DND_FILES = None
    DND_AVAILABLE = False
    AppTk = tk.Tk


APP_NAME = "转接口"
APP_VERSION = "1.11.0"
BG = "#f7f6f3"
CARD = "#fffdfa"
BLUE = "#c15f3c"
GREEN = "#3f7d5a"
TEXT = "#2f2a26"
MUTED = "#7a746e"
LINE = "#ded9d2"
ACCENT_SOFT = "#f5ebe5"
SUCCESS_SOFT = "#edf4ef"


class XiaoeLoginRequired(RuntimeError):
    pass


def extract_http_url(value):
    """Extract one clean web URL from browser address text or shared text."""
    text = (value or "").replace("\u200b", "").replace("\ufeff", "").strip()
    match = re.search(r"https?://[^\s<>\"']+", text, flags=re.I)
    if not match:
        return text
    return match.group(0).rstrip("，。；;、!！?？）)]}〉》」』")


def app_data_dir():
    root = os.environ.get("LOCALAPPDATA") or str(Path.home())
    path = Path(root) / "AudioXhl"
    path.mkdir(parents=True, exist_ok=True)
    return path


def resource_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bundled = base / "tools" / name
    if bundled.exists():
        return bundled
    beside = Path(sys.executable).resolve().parent / "tools" / name
    if beside.exists():
        return beside
    return Path(__file__).resolve().parent / "tools" / name


def asset_path(name):
    base = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))
    bundled = base / "assets" / name
    if bundled.exists():
        return bundled
    beside = Path(sys.executable).resolve().parent / "assets" / name
    if beside.exists():
        return beside
    return Path(__file__).resolve().parent / "assets" / name


def clean_filename(value):
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value or "音频")
    value = value.strip(" .")
    return value[:160] or "音频"


def seconds_text(value):
    try:
        total = int(float(value))
    except (TypeError, ValueError):
        return "未知"
    hours, rest = divmod(total, 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}" if hours else f"{minutes:02d}:{seconds:02d}"


def unwrap_redirect_url(value):
    """Turn a login/auth wrapper URL into the actual course URL."""
    current = extract_http_url(value)
    for _ in range(3):
        try:
            parsed = urlparse(current)
            values = parse_qs(parsed.query).get("redirect_url")
            if not values:
                break
            target = unquote(values[0]).strip()
            if not target.startswith(("http://", "https://")):
                break
            current = target
        except Exception:
            break
    return current


def is_xiaoe_url(value):
    parsed = urlparse(value)
    host = parsed.netloc.lower()
    path = parsed.path.lower()
    return (
        any(name in host for name in (
            "xiaoeknow.com", "xiaoe-tech.com", "xetlk.com", "xetslk.com",
            "xiaoecloud.com", "xet.tech", "xet.pomoho.com",
        ))
        or re.search(r"/v\d+/course/alive/l_", path)
        or "/p/t_pc/live_pc/" in path
        or "/p/course/" in path
    )


class Store:
    def __init__(self):
        self.path = app_data_dir() / "state.json"
        self.data = {
            "output_dir": str(Path.home() / "Downloads" / "音频小滑轮"),
            "cookie_source": "不使用",
            "cookie_file": "",
            "auto_transcribe": True,
            "conversion_output_dir": str(Path.home() / "Downloads" / "转接口文件转换"),
            "ocr_output_dir": str(Path.home() / "Downloads" / "转接口图片OCR"),
            "terminology_file": str(app_data_dir() / "sensevoice_terminology.txt"),
            # Remember only which local browser worked for each Xiaoe shop.
            # Cookie values themselves are never persisted by the application.
            "xiaoe_source_by_shop": {},
            "history": [],
        }
        try:
            saved = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(saved, dict):
                self.data.update(saved)
        except Exception:
            pass
        if not isinstance(self.data.get("xiaoe_source_by_shop"), dict):
            self.data["xiaoe_source_by_shop"] = {}
        Path(self.data["output_dir"]).mkdir(parents=True, exist_ok=True)
        Path(self.data["conversion_output_dir"]).mkdir(parents=True, exist_ok=True)
        Path(self.data["ocr_output_dir"]).mkdir(parents=True, exist_ok=True)

    def save(self):
        self.path.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")


class AudioXhl(AppTk):
    def __init__(self):
        super().__init__()
        self.store = Store()
        self.events = queue.Queue()
        self.current_process = None
        self.download_busy = False
        self.max_parallel_downloads = 3
        self.active_downloads = {}
        self.download_processes = {}
        self.pending_downloads = []
        self.transcribe_process = None
        self.transcription_cancel_event = threading.Event()
        self.transcription_engine = SenseVoiceEngine(
            resource_path("ffmpeg.exe"), resource_path("funasr")
        )
        self.media_resolver = MediaResolver(resource_path("yt-dlp.exe"))
        self.browser_capture = BrowserMediaCapture(app_data_dir() / "browser-capture-profile")
        self.browser_capture_cancel = threading.Event()
        self.browser_capture_active = False
        self.pending_browser_captures = []
        self.pending_transcriptions = []
        self.current_transcription_task = None
        self.conversion_engines = detect_engines()
        self.conversion_router = ConversionRouter(self.conversion_engines)
        self.conversion_tasks = []
        self.conversion_process = None
        self.current_conversion_task = None
        self.ocr_tasks = []
        self.ocr_process = None
        self.current_ocr_task = None
        self.ocr_engine = RapidOCREngine(resource_path("rapidocr"))
        self.parsed = None
        self.active_task = None
        self.login_browser_process = None
        self.login_opened_at = 0.0
        self.login_retry_url = ""
        self.login_retry_pending = False
        self.auth_verified_host = ""
        self.manual_cookie = ""
        self.auto_workflow_pending = False
        self._cleanup_stale_media_jobs()
        if self.store.data.get("cookie_source") in ("自动读取（本次）", "手动 Cookie（本次）"):
            profile = app_data_dir() / "edge-login-profile"
            self.store.data["cookie_source"] = "专用 Edge 登录窗口" if profile.is_dir() else "不使用"
            self.store.save()
        self.cookie_file_var = tk.StringVar(value=self.store.data.get("cookie_file", ""))
        self.protocol("WM_DELETE_WINDOW", self.on_close)
        self.title(f"{APP_NAME}  {APP_VERSION}")
        try:
            self.iconbitmap(str(asset_path("zhuanjiekou.ico")))
        except tk.TclError:
            pass
        self.geometry("1180x760")
        self.minsize(980, 650)
        self.configure(bg=BG)
        self._setup_style()
        self._build()
        self.after(100, self._drain_events)
        self._check_tools()

    def _setup_style(self):
        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure("TFrame", background=BG)
        style.configure("Card.TFrame", background=CARD)
        style.configure("TLabel", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Card.TLabel", background=CARD, foreground=TEXT, font=("Microsoft YaHei UI", 10))
        style.configure("Muted.Card.TLabel", background=CARD, foreground=MUTED, font=("Microsoft YaHei UI", 9))
        style.configure("Title.TLabel", background=BG, foreground=TEXT, font=("Microsoft YaHei UI", 15, "bold"))
        style.configure("Success.Card.TLabel", background=CARD, foreground=GREEN, font=("Microsoft YaHei UI", 10, "bold"))
        style.configure(
            "TButton", font=("Microsoft YaHei UI", 10), padding=(15, 8),
            borderwidth=1, background="#f8f5f1", foreground=TEXT,
            bordercolor=LINE,
        )
        style.map("TButton", background=[("active", "#efe9e3")])
        style.configure("Primary.TButton", background=BLUE, foreground="white", bordercolor=BLUE)
        style.map("Primary.TButton", background=[("active", "#a94f31"), ("disabled", "#d6b2a5")])
        style.configure("Danger.TButton", foreground="#a54b42")
        style.configure("TEntry", padding=8, fieldbackground="white", bordercolor=LINE)
        style.configure(
            "TCombobox", padding=8, fieldbackground="#fffdfa",
            background="#fffdfa", bordercolor=LINE, arrowcolor=TEXT,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", "#fffdfa")],
            selectbackground=[("readonly", "#fffdfa")],
            selectforeground=[("readonly", TEXT)],
        )
        style.configure(
            "Treeview", rowheight=40, font=("Microsoft YaHei UI", 9),
            background="#fffdfa", fieldbackground="#fffdfa",
            bordercolor=LINE, lightcolor=LINE, darkcolor=LINE,
        )
        style.map(
            "Treeview",
            background=[("selected", ACCENT_SOFT)],
            foreground=[("selected", TEXT)],
        )
        style.configure(
            "Treeview.Heading", font=("Microsoft YaHei UI", 9, "bold"),
            padding=9, background="#f1eee9", foreground=TEXT,
            bordercolor=LINE,
        )
        style.map("Treeview.Heading", background=[("active", "#e9e4dd")])
        style.configure("Horizontal.TProgressbar", troughcolor="#ebe7e2", background=BLUE)

    def _build(self):
        header = tk.Frame(self, bg="white", height=66, highlightbackground=LINE, highlightthickness=1)
        header.pack(fill="x")
        header.pack_propagate(False)
        tk.Label(header, text="◉", fg=BLUE, bg="white", font=("Arial", 24, "bold")).pack(side="left", padx=(25, 8))
        tk.Label(
            header, text=f"{APP_NAME}  v{APP_VERSION}", fg=TEXT, bg="white",
            font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        tk.Label(
            header,
            text="音视频处理工具  ·  文档转换工具  ·  图片文字识别",
            fg=MUTED,
            bg="white",
            font=("Microsoft YaHei UI", 9),
        ).pack(side="right", padx=25)

        nav = tk.Frame(self, bg="white", height=54)
        nav.pack(fill="x")
        nav.pack_propagate(False)
        self.nav_buttons = {}
        for key, text in (
            ("home", "音频下载"),
            ("transcript", "音视频转文字"),
            ("converter", "文件转换  NEW"),
            ("ocr", "图片 OCR  NEW"),
            ("active", "下载中"),
            ("done", "已完成"),
        ):
            if key == "active":
                tk.Frame(nav, width=1, bg=LINE).pack(
                    side="left", fill="y", padx=(8, 10), pady=12
                )
            button = tk.Button(nav, text=text, relief="flat", bd=0, bg="white", fg=MUTED,
                               activebackground=ACCENT_SOFT, font=("Microsoft YaHei UI", 11, "bold"),
                               padx=22, command=lambda k=key: self.show_page(k))
            button.pack(side="left", fill="y")
            self.nav_buttons[key] = button
        ttk.Button(nav, text="使用说明", command=self.show_help).pack(side="right", padx=14, pady=8)
        ttk.Button(nav, text="更新解析组件", command=self.update_ytdlp).pack(side="right", pady=8)
        ttk.Button(nav, text="下载设置", command=self.choose_dir).pack(side="right", pady=8)

        self.body = ttk.Frame(self, padding=28)
        self.body.pack(fill="both", expand=True)
        self.pages = {}
        self._build_home()
        self._build_transcript()
        self._build_converter()
        self._build_ocr()
        self._build_active()
        self._build_done()
        self.show_page("home")

    def _page(self, key):
        page = ttk.Frame(self.body)
        page.grid(row=0, column=0, sticky="nsew")
        self.body.rowconfigure(0, weight=1)
        self.body.columnconfigure(0, weight=1)
        self.pages[key] = page
        return page

    def _build_home(self):
        page = self._page("home")
        card = ttk.Frame(page, style="Card.TFrame", padding=28)
        card.pack(fill="both", expand=True)
        card.columnconfigure(1, weight=1)

        self.notice = ttk.Label(card, text="ⓘ  粘贴任意公开音视频链接或分享文案：优先取字幕，无字幕再离线识别。",
                                style="Success.Card.TLabel")
        self.notice.grid(row=0, column=0, columnspan=4, sticky="ew", pady=(0, 25))

        ttk.Label(card, text="* 网址", style="Card.TLabel").grid(row=1, column=0, sticky="w", padx=(0, 18), pady=8)
        self.url_var = tk.StringVar()
        self.url_entry = ttk.Entry(card, textvariable=self.url_var)
        self.url_entry.grid(row=1, column=1, columnspan=2, sticky="ew", pady=8)
        ttk.Button(card, text="粘贴", command=self.paste_url).grid(row=1, column=3, padx=(12, 0), pady=8)

        ttk.Label(card, text="解析结果", style="Card.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 18), pady=8)
        self.result_var = tk.StringVar(value="尚未解析")
        ttk.Label(card, textvariable=self.result_var, style="Muted.Card.TLabel", anchor="w", wraplength=620).grid(
            row=2, column=1, sticky="ew", pady=8)
        self.resolver_badge_var = tk.StringVar(value="等待链接")
        self.resolver_badge = tk.Label(
            card, textvariable=self.resolver_badge_var, bg="#f1eee9", fg=MUTED,
            font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4,
        )
        self.resolver_badge.grid(row=2, column=2, sticky="e", padx=(10, 0))
        self.parse_button = ttk.Button(
            card, text="粘贴链接生成逐字稿",
            style="Primary.TButton", command=self.one_click_full_workflow,
        )
        self.parse_button.grid(row=2, column=3, padx=(12, 0), pady=8)

        ttk.Label(card, text="* 文件名称", style="Card.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 18), pady=8)
        self.name_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.name_var).grid(row=3, column=1, columnspan=3, sticky="ew", pady=8)

        ttk.Label(card, text="音频格式", style="Card.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 18), pady=8)
        format_row = ttk.Frame(card, style="Card.TFrame")
        format_row.grid(row=4, column=1, columnspan=3, sticky="w", pady=8)
        self.format_var = tk.StringVar(value="mp3")
        self.format_box = ttk.Combobox(format_row, textvariable=self.format_var, state="readonly", width=12,
                                       values=("mp3", "m4a", "flac", "wav", "opus"))
        self.format_box.pack(side="left")
        ttk.Label(format_row, text="  质量", style="Card.TLabel").pack(side="left", padx=(22, 6))
        self.quality_var = tk.StringVar(value="320K")
        ttk.Combobox(format_row, textvariable=self.quality_var, state="readonly", width=12,
                     values=("320K", "256K", "192K", "128K", "最佳原始质量")).pack(side="left")
        self.playlist_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(format_row, text="下载整个播放列表", variable=self.playlist_var).pack(side="left", padx=25)
        self.auto_transcribe_var = tk.BooleanVar(value=bool(self.store.data.get("auto_transcribe", True)))
        ttk.Checkbutton(
            format_row, text="下载后自动生成逐字稿（TXT）",
            variable=self.auto_transcribe_var, command=self._save_auto_transcribe,
        ).pack(side="left", padx=(0, 10))

        ttk.Label(card, text="登录状态", style="Card.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 18), pady=8)
        auth_row = ttk.Frame(card, style="Card.TFrame")
        auth_row.grid(row=5, column=1, columnspan=3, sticky="ew", pady=8)
        self.cookie_source_var = tk.StringVar(value=self.store.data.get("cookie_source", "不使用"))
        self.cookie_source_box = ttk.Combobox(
            auth_row, textvariable=self.cookie_source_var, state="readonly", width=18,
            values=(
                "不使用", "自动读取（本次）", "手动 Cookie（本次）", "专用 Edge 登录窗口", "Edge 登录状态",
                "Chrome 登录状态", "Firefox 登录状态", "cookies.txt 文件",
            ),
        )
        self.cookie_source_box.pack(side="left")
        self.cookie_source_box.bind("<<ComboboxSelected>>", self.cookie_source_changed)
        self.cookie_file_label = ttk.Label(auth_row, text="", style="Muted.Card.TLabel")
        self.cookie_file_label.pack(side="left", padx=12)
        self.cookie_file_button = ttk.Button(auth_row, text="选择 cookies.txt", command=self.choose_cookie_file)
        self.cookie_file_button.pack(side="left")
        self.login_browser_button = ttk.Button(auth_row, text="打开登录窗口", command=self.open_dedicated_login)
        self.login_browser_button.pack(side="left", padx=(0, 8))
        self.manual_cookie_button = ttk.Button(auth_row, text="读取剪贴板 Cookie", command=self.read_manual_cookie)
        self.manual_cookie_button.pack(side="left", padx=(0, 8))
        self.auto_cookie_button = ttk.Button(
            auth_row, text="一键读取并解析", style="Primary.TButton",
            command=self.one_click_cookie_parse,
        )
        self.auto_cookie_button.pack(side="left", padx=(0, 8))
        ttk.Label(auth_row, text="仅用于你已获授权的登录内容", style="Muted.Card.TLabel").pack(side="left", padx=12)
        self._refresh_cookie_controls()

        ttk.Label(card, text="存储地址", style="Card.TLabel").grid(row=6, column=0, sticky="w", padx=(0, 18), pady=8)
        self.dir_var = tk.StringVar(value=self.store.data["output_dir"])
        ttk.Entry(card, textvariable=self.dir_var, state="readonly").grid(row=6, column=1, columnspan=2, sticky="ew", pady=8)
        ttk.Button(card, text="选择目录", command=self.choose_dir).grid(row=6, column=3, padx=(12, 0), pady=8)

        button_row = ttk.Frame(card, style="Card.TFrame")
        button_row.grid(row=7, column=1, columnspan=3, sticky="w", pady=(28, 5))
        self.download_button = ttk.Button(
            button_row,
            text="下载音频并生成逐字稿" if self.auto_transcribe_var.get() else "仅下载音频",
            style="Primary.TButton", command=self.start_download,
        )
        self.download_button.pack(side="left")
        ttk.Button(button_row, text="清空", command=self.clear_form).pack(side="left", padx=12)
        ttk.Button(button_row, text="打开目录", command=lambda: self.open_path(Path(self.dir_var.get()))).pack(side="left")
        self.batch_button = ttk.Button(
            button_row, text="批量选择本课程视频", command=self.load_course_batch,
        )
        self.batch_button.pack(side="left", padx=12)

        self.status_var = tk.StringVar(value="准备就绪")
        ttk.Label(card, textvariable=self.status_var, style="Muted.Card.TLabel").grid(row=8, column=0, columnspan=4, sticky="w", pady=(24, 4))

    def _build_transcript(self):
        page = self._page("transcript")
        card = ttk.Frame(page, style="Card.TFrame", padding=28)
        card.pack(fill="both", expand=True)
        card.columnconfigure(1, weight=1)

        transcript_title = ttk.Frame(card, style="Card.TFrame")
        transcript_title.grid(row=0, column=0, columnspan=4, sticky="ew")
        tk.Label(
            transcript_title,
            text="音视频工具",
            bg=ACCENT_SOFT,
            fg=BLUE,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=4,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(
            transcript_title, text="音视频一键转逐字稿",
            style="Card.TLabel", font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        tk.Label(
            transcript_title,
            text="SenseVoice Q8 · FSMN-VAD · TXT",
            bg=SUCCESS_SOFT,
            fg=GREEN,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=4,
        ).pack(side="right")
        ttk.Label(
            card,
            text="极速中文识别、自动过滤静音、术语纠错与智能分段；全程本地离线，只输出 TXT。",
            style="Muted.Card.TLabel",
        ).grid(row=1, column=0, columnspan=4, sticky="w", pady=(6, 18))

        self.drop_zone = tk.Frame(
            card, bg=ACCENT_SOFT, height=150,
            highlightbackground="#d9b7a9", highlightthickness=1,
        )
        self.drop_zone.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(0, 18))
        self.drop_zone.grid_propagate(False)
        self.drop_zone_label = tk.Label(
            self.drop_zone,
            text="把音频或视频拖到这里\n\n也可以点击选择文件",
            bg=ACCENT_SOFT, fg=BLUE, cursor="hand2",
            font=("Microsoft YaHei UI", 12, "bold"),
        )
        self.drop_zone_label.place(relx=0.5, rely=0.5, anchor="center")
        self.drop_zone_label.bind("<Button-1>", lambda _event: self.choose_transcript_file())
        self.drop_zone.bind("<Button-1>", lambda _event: self.choose_transcript_file())
        if DND_AVAILABLE:
            for widget in (self.drop_zone, self.drop_zone_label):
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_file_drop)

        ttk.Label(card, text="源文件", style="Card.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 18), pady=8)
        self.transcript_file_var = tk.StringVar()
        ttk.Entry(card, textvariable=self.transcript_file_var, state="readonly").grid(
            row=3, column=1, columnspan=2, sticky="ew", pady=8
        )
        ttk.Button(card, text="选择文件", command=self.choose_transcript_file).grid(row=3, column=3, padx=(12, 0), pady=8)

        ttk.Label(card, text="识别设置", style="Card.TLabel").grid(row=4, column=0, sticky="w", padx=(0, 18), pady=8)
        options = ttk.Frame(card, style="Card.TFrame")
        options.grid(row=4, column=1, columnspan=3, sticky="ew", pady=8)
        ttk.Label(options, text="模型", style="Card.TLabel").pack(side="left")
        self.transcript_model_var = tk.StringVar(value="SenseVoice Q8（极速精准·离线）")
        tk.Label(
            options, textvariable=self.transcript_model_var,
            bg=SUCCESS_SOFT, fg=GREEN, font=("Microsoft YaHei UI", 9, "bold"),
            padx=9, pady=4,
        ).pack(side="left", padx=(8, 20))
        self.transcript_language_var = tk.StringVar(value="自动识别（中文优先）")
        ttk.Label(options, text="后端", style="Card.TLabel").pack(side="left")
        self.transcript_backend_var = tk.StringVar(value=self.transcription_engine.backend_summary)
        ttk.Label(
            options, textvariable=self.transcript_backend_var, style="Muted.Card.TLabel"
        ).pack(side="left", padx=(8, 18))
        ttk.Button(
            options, text="术语纠错词库", command=self.open_transcript_glossary
        ).pack(side="left")
        self.word_timestamps_var = tk.BooleanVar(value=False)

        ttk.Label(card, text="输出目录", style="Card.TLabel").grid(row=5, column=0, sticky="w", padx=(0, 18), pady=8)
        self.transcript_output_var = tk.StringVar(value=str(Path.home() / "Desktop" / "转接口逐字稿"))
        ttk.Entry(card, textvariable=self.transcript_output_var, state="readonly").grid(
            row=5, column=1, columnspan=2, sticky="ew", pady=8
        )
        ttk.Button(card, text="选择目录", command=self.choose_transcript_output).grid(row=5, column=3, padx=(12, 0), pady=8)

        action_row = ttk.Frame(card, style="Card.TFrame")
        action_row.grid(row=6, column=1, columnspan=3, sticky="w", pady=(20, 8))
        self.transcribe_button = ttk.Button(
            action_row, text="一键生成逐字稿", style="Primary.TButton", command=self.start_transcription
        )
        self.transcribe_button.pack(side="left")
        self.transcribe_cancel_button = ttk.Button(
            action_row, text="取消", style="Danger.TButton", command=self.cancel_transcription, state="disabled"
        )
        self.transcribe_cancel_button.pack(side="left", padx=12)
        ttk.Button(action_row, text="打开输出目录", command=self.open_transcript_output).pack(side="left")

        self.transcript_progress = ttk.Progressbar(
            card, orient="horizontal", mode="determinate", maximum=100
        )
        self.transcript_progress.grid(row=7, column=0, columnspan=4, sticky="ew", pady=(16, 8))
        self.transcript_status_var = tk.StringVar(value="等待拖入文件")
        ttk.Label(card, textvariable=self.transcript_status_var, style="Muted.Card.TLabel").grid(
            row=8, column=0, columnspan=4, sticky="w"
        )

    def _build_converter(self):
        page = self._page("converter")
        card = ttk.Frame(page, style="Card.TFrame", padding=24)
        card.pack(fill="both", expand=True)

        title_row = ttk.Frame(card, style="Card.TFrame")
        title_row.pack(fill="x")
        tk.Label(
            title_row,
            text="文档转换工具",
            bg=ACCENT_SOFT,
            fg=BLUE,
            font=("Microsoft YaHei UI", 9, "bold"),
            padx=10,
            pady=4,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(
            title_row, text="离线批量文件转换",
            style="Card.TLabel", font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        self.conversion_engine_var = tk.StringVar(
            value="已检测：" + engine_summary(self.conversion_engines)
        )
        ttk.Label(
            title_row, textvariable=self.conversion_engine_var,
            style="Muted.Card.TLabel",
        ).pack(side="right")
        ttk.Label(
            card,
            text="PDF · Word · Excel · CSV · EPUB｜多文件批处理｜文件不上传",
            style="Muted.Card.TLabel",
        ).pack(fill="x", pady=(6, 0))

        self.conversion_drop_zone = tk.Frame(
            card, bg="#faf6f2", height=92,
            highlightbackground="#d9b7a9", highlightthickness=1,
        )
        self.conversion_drop_zone.pack(fill="x", pady=(14, 12))
        self.conversion_drop_zone.pack_propagate(False)
        self.conversion_drop_label = tk.Label(
            self.conversion_drop_zone,
            text="①  拖入文件，或点击这里选择多个文件\nPDF · Word · Excel · CSV · EPUB",
            bg="#faf6f2", fg=BLUE, cursor="hand2",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.conversion_drop_label.place(relx=0.5, rely=0.5, anchor="center")
        for widget in (self.conversion_drop_zone, self.conversion_drop_label):
            widget.bind("<Button-1>", lambda _event: self.choose_conversion_files())
            if DND_AVAILABLE:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_conversion_drop)

        format_panel = tk.Frame(
            card, bg="#f5f1ec", highlightbackground=LINE,
            highlightthickness=1, padx=14, pady=10,
        )
        format_panel.pack(fill="x", pady=(0, 10))
        tk.Label(
            format_panel, text="②  选择目标格式", bg="#f5f1ec", fg=TEXT,
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        self.conversion_selection_hint_var = tk.StringVar(
            value="请先选择下方任务"
        )
        tk.Label(
            format_panel, textvariable=self.conversion_selection_hint_var,
            bg="#f5f1ec", fg=MUTED, font=("Microsoft YaHei UI", 9),
        ).pack(side="left", padx=(12, 18))
        self.conversion_target_var = tk.StringVar(value="请先选择文件")
        self.conversion_target_box = ttk.Combobox(
            format_panel, textvariable=self.conversion_target_var,
            state="disabled", width=20, values=(),
        )
        self.conversion_target_box.pack(side="left", padx=(0, 8))
        self.conversion_target_box.bind(
            "<<ComboboxSelected>>", self.apply_conversion_target
        )
        self.conversion_apply_button = ttk.Button(
            format_panel, text="应用到所选", command=self.apply_conversion_target,
            state="disabled",
        )
        self.conversion_apply_button.pack(side="left")
        tk.Label(
            format_panel, text="支持 Ctrl / Shift 多选后批量修改",
            bg="#f5f1ec", fg=MUTED, font=("Microsoft YaHei UI", 8),
        ).pack(side="right")

        output_row = ttk.Frame(card, style="Card.TFrame")
        output_row.pack(fill="x", pady=(0, 10))
        ttk.Label(
            output_row, text="③  输出目录", style="Card.TLabel",
            font=("Microsoft YaHei UI", 10, "bold"),
        ).pack(side="left")
        self.conversion_output_var = tk.StringVar(
            value=self.store.data["conversion_output_dir"]
        )
        ttk.Entry(
            output_row, textvariable=self.conversion_output_var,
            state="readonly", width=55,
        ).pack(side="left", fill="x", expand=True, padx=8)
        ttk.Button(
            output_row, text="更改目录", command=self.choose_conversion_output
        ).pack(side="left")

        columns = ("name", "source", "target", "status", "progress", "output")
        self.conversion_tree = ttk.Treeview(
            card, columns=columns, show="headings", selectmode="extended",
            height=6,
        )
        headings = {
            "name": "文件名称", "source": "源格式", "target": "目标格式",
            "status": "状态", "progress": "阶段进度", "output": "输出位置",
        }
        widths = {
            "name": 245, "source": 75, "target": 115,
            "status": 90, "progress": 100, "output": 360,
        }
        for key in columns:
            self.conversion_tree.heading(key, text=headings[key])
            self.conversion_tree.column(
                key, width=widths[key],
                anchor="center" if key in {"source", "target", "status", "progress"} else "w",
            )
        self.conversion_tree.tag_configure("waiting", foreground=TEXT)
        self.conversion_tree.tag_configure("running", foreground=BLUE)
        self.conversion_tree.tag_configure("done", foreground=GREEN)
        self.conversion_tree.tag_configure("failed", foreground="#a54b42")
        self.conversion_tree.bind(
            "<<TreeviewSelect>>", self.conversion_selection_changed
        )
        self.conversion_tree.bind(
            "<Double-1>", self.open_conversion_result_from_event
        )

        actions = ttk.Frame(card, style="Card.TFrame")
        self.conversion_start_button = ttk.Button(
            actions, text="④  开始转换全部", style="Primary.TButton",
            command=self.start_conversion_queue,
        )
        self.conversion_start_button.pack(side="left")
        self.conversion_cancel_button = ttk.Button(
            actions, text="取消当前", style="Danger.TButton",
            state="disabled", command=self.cancel_current_conversion,
        )
        self.conversion_cancel_button.pack(side="left", padx=8)
        ttk.Button(
            actions, text="重试失败", command=self.retry_failed_conversions
        ).pack(side="left")
        ttk.Button(
            actions, text="移除所选", command=self.remove_selected_conversions
        ).pack(side="left", padx=8)
        ttk.Button(
            actions, text="清空完成", command=self.clear_completed_conversions
        ).pack(side="left")
        ttk.Button(
            actions, text="打开输出目录", command=self.open_conversion_output
        ).pack(side="right")

        self.conversion_status_var = tk.StringVar(
            value="等待添加文件；扫描版 PDF 会提示需要 OCR。"
        )
        conversion_status_label = ttk.Label(
            card, textvariable=self.conversion_status_var,
            style="Muted.Card.TLabel",
        )
        actions.pack(side="bottom", fill="x", pady=(12, 0))
        conversion_status_label.pack(side="bottom", fill="x", pady=(10, 0))
        self.conversion_tree.pack(fill="both", expand=True)

    def _build_ocr(self):
        page = self._page("ocr")
        card = ttk.Frame(page, style="Card.TFrame", padding=24)
        card.pack(fill="both", expand=True)

        title_row = ttk.Frame(card, style="Card.TFrame")
        title_row.pack(fill="x")
        tk.Label(
            title_row, text="图片文字工具", bg=ACCENT_SOFT, fg=BLUE,
            font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4,
        ).pack(side="left", padx=(0, 12))
        ttk.Label(
            title_row, text="离线批量图片 OCR",
            style="Card.TLabel", font=("Microsoft YaHei UI", 14, "bold"),
        ).pack(side="left")
        engine_text = "RapidOCR · PP-OCRv4 · 本地离线" if self.ocr_engine.available else "OCR 引擎缺失"
        tk.Label(
            title_row, text=engine_text,
            bg=SUCCESS_SOFT if self.ocr_engine.available else "#f8e8e5",
            fg=GREEN if self.ocr_engine.available else "#a54b42",
            font=("Microsoft YaHei UI", 9, "bold"), padx=10, pady=4,
        ).pack(side="right")
        ttk.Label(
            card,
            text="一次拖入多张图片，自动识别中文、英文和数字；每张图片输出一个 UTF-8 TXT，文件不上传。",
            style="Muted.Card.TLabel",
        ).pack(fill="x", pady=(6, 0))

        self.ocr_drop_zone = tk.Frame(
            card, bg="#faf6f2", height=92,
            highlightbackground="#d9b7a9", highlightthickness=1,
        )
        self.ocr_drop_zone.pack(fill="x", pady=(14, 12))
        self.ocr_drop_zone.pack_propagate(False)
        self.ocr_drop_label = tk.Label(
            self.ocr_drop_zone,
            text="把多张图片拖到这里，或点击选择图片\nPNG · JPG · JPEG · BMP · WEBP · TIFF",
            bg="#faf6f2", fg=BLUE, cursor="hand2",
            font=("Microsoft YaHei UI", 11, "bold"),
        )
        self.ocr_drop_label.place(relx=0.5, rely=0.5, anchor="center")
        for widget in (self.ocr_drop_zone, self.ocr_drop_label):
            widget.bind("<Button-1>", lambda _event: self.choose_ocr_files())
            if DND_AVAILABLE:
                widget.drop_target_register(DND_FILES)
                widget.dnd_bind("<<Drop>>", self.on_ocr_drop)

        options = ttk.Frame(card, style="Card.TFrame")
        options.pack(fill="x", pady=(0, 10))
        ttk.Label(options, text="输出目录", style="Card.TLabel").pack(side="left")
        self.ocr_output_var = tk.StringVar(value=self.store.data["ocr_output_dir"])
        ttk.Entry(options, textvariable=self.ocr_output_var, state="readonly").pack(
            side="left", fill="x", expand=True, padx=8
        )
        ttk.Button(options, text="选择目录", command=self.choose_ocr_output).pack(side="left")
        self.ocr_combine_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            options, text="同时生成一个合并总 TXT", variable=self.ocr_combine_var
        ).pack(side="left", padx=(18, 0))

        columns = ("name", "size", "status", "progress", "output")
        self.ocr_tree = ttk.Treeview(
            card, columns=columns, show="headings", selectmode="extended", height=7
        )
        headings = {
            "name": "图片名称", "size": "大小", "status": "状态",
            "progress": "阶段进度", "output": "TXT 输出位置",
        }
        widths = {"name": 300, "size": 80, "status": 90, "progress": 110, "output": 420}
        for key in columns:
            self.ocr_tree.heading(key, text=headings[key])
            self.ocr_tree.column(
                key, width=widths[key],
                anchor="center" if key in {"size", "status", "progress"} else "w",
            )
        self.ocr_tree.tag_configure("waiting", foreground=TEXT)
        self.ocr_tree.tag_configure("running", foreground=BLUE)
        self.ocr_tree.tag_configure("done", foreground=GREEN)
        self.ocr_tree.tag_configure("failed", foreground="#a54b42")
        self.ocr_tree.bind("<Double-1>", self.open_ocr_result_from_event)

        actions = ttk.Frame(card, style="Card.TFrame")
        self.ocr_start_button = ttk.Button(
            actions, text="开始识别全部", style="Primary.TButton", command=self.start_ocr_queue
        )
        self.ocr_start_button.pack(side="left")
        self.ocr_cancel_button = ttk.Button(
            actions, text="取消当前", style="Danger.TButton", state="disabled",
            command=self.cancel_current_ocr,
        )
        self.ocr_cancel_button.pack(side="left", padx=8)
        ttk.Button(actions, text="重试失败", command=self.retry_failed_ocr).pack(side="left")
        ttk.Button(actions, text="移除所选", command=self.remove_selected_ocr).pack(side="left", padx=8)
        ttk.Button(actions, text="清空完成", command=self.clear_completed_ocr).pack(side="left")
        ttk.Button(actions, text="打开输出目录", command=self.open_ocr_output).pack(side="right")

        self.ocr_status_var = tk.StringVar(value="等待添加图片。识别全程离线，不上传文件。")
        ocr_status_label = ttk.Label(
            card, textvariable=self.ocr_status_var, style="Muted.Card.TLabel"
        )
        actions.pack(side="bottom", fill="x", pady=(12, 0))
        ocr_status_label.pack(side="bottom", fill="x", pady=(10, 0))
        self.ocr_tree.pack(fill="both", expand=True)

    def choose_ocr_files(self):
        paths = filedialog.askopenfilenames(
            title="选择要识别的图片",
            filetypes=(("图片文件", "*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff"), ("所有文件", "*.*")),
        )
        if paths:
            self.add_ocr_files(paths)

    def on_ocr_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except tk.TclError:
            paths = (event.data,)
        self.add_ocr_files(paths)

    def add_ocr_files(self, paths):
        supported = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
        existing = {str(Path(task["source"]).resolve()).lower() for task in self.ocr_tasks}
        added = 0
        rejected = []
        for value in paths:
            path = Path(value)
            if not path.is_file() or path.suffix.lower() not in supported:
                rejected.append(path.name or str(path))
                continue
            resolved = str(path.resolve())
            if resolved.lower() in existing:
                continue
            task = {
                "id": "ocr-" + uuid.uuid4().hex,
                "source": resolved,
                "status": "等待", "progress": "0%", "output": "", "error": "",
                "cancel_requested": False,
            }
            self.ocr_tasks.append(task)
            existing.add(resolved.lower())
            self._refresh_ocr_task(task)
            added += 1
        if added:
            self.ocr_status_var.set(f"已添加 {added} 张图片，点击“开始识别全部”。")
        if rejected:
            messagebox.showwarning(APP_NAME, "以下文件不是支持的图片格式：\n\n" + "\n".join(rejected[:15]))

    def _ocr_task(self, task_id):
        return next((task for task in self.ocr_tasks if task["id"] == task_id), None)

    def _refresh_ocr_task(self, task):
        source = Path(task["source"])
        try:
            size = source.stat().st_size
            size_text = f"{size / 1024 / 1024:.1f} MB" if size >= 1024 * 1024 else f"{max(1, size // 1024)} KB"
        except OSError:
            size_text = "未知"
        values = (source.name, size_text, task["status"], task["progress"], task["output"])
        if task["status"] in {"准备", "识别", "保存"}:
            tag = "running"
        elif task["status"] == "完成":
            tag = "done"
        elif task["status"] in {"失败", "已取消"}:
            tag = "failed"
        else:
            tag = "waiting"
        if self.ocr_tree.exists(task["id"]):
            self.ocr_tree.item(task["id"], values=values, tags=(tag,))
        else:
            self.ocr_tree.insert("", "end", iid=task["id"], values=values, tags=(tag,))

    def choose_ocr_output(self):
        path = filedialog.askdirectory(initialdir=self.ocr_output_var.get())
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
            self.ocr_output_var.set(path)
            self.store.data["ocr_output_dir"] = path
            self.store.save()

    def start_ocr_queue(self):
        if self.current_ocr_task is not None:
            self.ocr_status_var.set("图片识别队列正在运行。")
            return
        if not self.ocr_engine.available:
            messagebox.showerror(APP_NAME, "RapidOCR 离线引擎不完整，请重新部署工具。")
            return
        if not any(task["status"] == "等待" for task in self.ocr_tasks):
            messagebox.showinfo(APP_NAME, "没有等待识别的图片。")
            return
        try:
            Path(self.ocr_output_var.get()).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法创建输出目录：\n\n{exc}")
            return
        self.ocr_start_button.configure(state="disabled")
        self._start_next_ocr()

    def _start_next_ocr(self):
        task = next((item for item in self.ocr_tasks if item["status"] == "等待"), None)
        if task is None:
            self.current_ocr_task = None
            self.ocr_process = None
            self.ocr_start_button.configure(state="normal")
            self.ocr_cancel_button.configure(state="disabled")
            completed = [item for item in self.ocr_tasks if item["status"] == "完成" and item["output"]]
            failed = sum(item["status"] == "失败" for item in self.ocr_tasks)
            combined = ""
            if completed and self.ocr_combine_var.get():
                try:
                    output_dir = Path(self.ocr_output_var.get())
                    combined_path = self._unique_output_path(output_dir, "批量OCR汇总", ".txt")
                    sections = []
                    for item in completed:
                        text = Path(item["output"]).read_text(encoding="utf-8")
                        sections.append(f"===== {Path(item['source']).name} =====\n{text.strip()}")
                    combined_path.write_text("\n\n".join(sections) + "\n", encoding="utf-8")
                    combined = f"，合并文件：{combined_path.name}"
                except Exception as exc:
                    combined = f"，合并 TXT 失败：{exc}"
            self.ocr_status_var.set(f"批量识别结束：完成 {len(completed)}，失败 {failed}{combined}。")
            return
        task["cancel_requested"] = False
        task["status"] = "准备"
        task["progress"] = "10%"
        task["error"] = ""
        task["output_dir"] = self.ocr_output_var.get()
        self.current_ocr_task = task
        self.ocr_cancel_button.configure(state="normal")
        self._refresh_ocr_task(task)
        self.ocr_status_var.set("正在识别：" + Path(task["source"]).name)
        threading.Thread(target=self._ocr_worker, args=(task,), daemon=True).start()

    def _ocr_worker(self, task):
        try:
            self.events.put(("ocr_stage", task["id"], "识别", "45%"))
            text = self.ocr_engine.recognize(task["source"])
            self.ocr_process = self.ocr_engine.process
            if task.get("cancel_requested"):
                self.events.put(("ocr_cancelled", task["id"]))
                return
            self.events.put(("ocr_stage", task["id"], "保存", "85%"))
            output_dir = Path(task["output_dir"])
            destination = self._unique_output_path(output_dir, clean_filename(Path(task["source"]).stem), ".txt")
            destination.write_text(text if text else "[未识别到文字]\n", encoding="utf-8")
            self.events.put(("ocr_done", task["id"], str(destination), len(text)))
        except RapidOCRError as exc:
            if task.get("cancel_requested"):
                self.events.put(("ocr_cancelled", task["id"]))
            else:
                self.events.put(("ocr_error", task["id"], str(exc)))
        except Exception as exc:
            self.events.put(("ocr_error", task["id"], str(exc)))

    def cancel_current_ocr(self):
        task = self.current_ocr_task
        if task is None:
            return
        task["cancel_requested"] = True
        self.ocr_status_var.set("正在取消当前图片识别……")
        self.ocr_engine.close()

    def retry_failed_ocr(self):
        count = 0
        for task in self.ocr_tasks:
            if task["status"] in {"失败", "已取消"}:
                task["status"], task["progress"], task["error"], task["output"] = "等待", "0%", "", ""
                self._refresh_ocr_task(task)
                count += 1
        self.ocr_status_var.set(f"已将 {count} 个任务放回等待队列。")

    def remove_selected_ocr(self):
        current_id = self.current_ocr_task["id"] if self.current_ocr_task else ""
        for task_id in list(self.ocr_tree.selection()):
            if task_id == current_id:
                continue
            self.ocr_tree.delete(task_id)
            self.ocr_tasks = [task for task in self.ocr_tasks if task["id"] != task_id]

    def clear_completed_ocr(self):
        for task in list(self.ocr_tasks):
            if task["status"] == "完成":
                if self.ocr_tree.exists(task["id"]):
                    self.ocr_tree.delete(task["id"])
                self.ocr_tasks.remove(task)

    def open_ocr_output(self):
        self.open_path(Path(self.ocr_output_var.get()))

    def open_ocr_result_from_event(self, _event=None):
        selected = self.ocr_tree.selection()
        if not selected:
            return
        task = self._ocr_task(selected[0])
        if task and task["output"] and Path(task["output"]).is_file():
            self.open_path(Path(task["output"]))


    def _build_active(self):
        page = self._page("active")
        card = ttk.Frame(page, style="Card.TFrame", padding=28)
        card.pack(fill="both", expand=True)
        ttk.Label(card, text="并行下载任务", style="Card.TLabel", font=("Microsoft YaHei UI", 13, "bold")).pack(anchor="w")
        self.active_title = ttk.Label(card, text="暂无下载任务", style="Muted.Card.TLabel")
        self.active_title.pack(anchor="w", pady=(35, 8))
        self.active_detail = ttk.Label(card, text="", style="Muted.Card.TLabel")
        self.active_detail.pack(anchor="w", pady=4)
        self.progress = ttk.Progressbar(card, orient="horizontal", mode="determinate", maximum=100)
        self.progress.pack(fill="x", pady=18)
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(anchor="w")
        self.cancel_button = ttk.Button(row, text="取消全部下载", style="Danger.TButton", command=self.cancel_download, state="disabled")
        self.cancel_button.pack(side="left")
        ttk.Button(row, text="打开存储目录", command=lambda: self.open_path(Path(self.dir_var.get()))).pack(side="left", padx=12)
        ttk.Button(row, text="清空等待队列", command=self.clear_pending_downloads).pack(side="left")
        self.download_queue_var = tk.StringVar(value="等待队列：0 个任务")
        ttk.Label(card, textvariable=self.download_queue_var, style="Muted.Card.TLabel").pack(anchor="w", pady=(16, 0))
        self.log = tk.Text(card, height=13, bg="#faf8f5", fg=MUTED, relief="flat", padx=12, pady=10,
                           font=("Consolas", 9), state="disabled")
        self.log.pack(fill="both", expand=True, pady=(12, 0))

    def _build_done(self):
        page = self._page("done")
        card = ttk.Frame(page, style="Card.TFrame", padding=20)
        card.pack(fill="both", expand=True)
        columns = ("title", "format", "date", "path")
        self.history = ttk.Treeview(card, columns=columns, show="headings", selectmode="browse")
        self.history.heading("title", text="文件名称")
        self.history.heading("format", text="格式")
        self.history.heading("date", text="完成时间")
        self.history.heading("path", text="存储位置")
        self.history.column("title", width=260)
        self.history.column("format", width=80, anchor="center")
        self.history.column("date", width=155, anchor="center")
        self.history.column("path", width=450)
        self.history.pack(fill="both", expand=True)
        self.history.bind("<Double-1>", lambda _e: self.open_selected())
        row = ttk.Frame(card, style="Card.TFrame")
        row.pack(fill="x", pady=(15, 0))
        ttk.Button(row, text="打开文件", command=self.open_selected).pack(side="left")
        ttk.Button(row, text="打开所在目录", command=self.open_selected_folder).pack(side="left", padx=10)
        ttk.Button(row, text="清除记录", style="Danger.TButton", command=self.clear_history).pack(side="right")
        self.refresh_history()

    def show_page(self, key):
        self.pages[key].tkraise()
        for name, button in self.nav_buttons.items():
            active = name == key
            button.configure(bg=ACCENT_SOFT if active else "white", fg=BLUE if active else MUTED)
        if key == "done":
            self.refresh_history()

    def refresh_conversion_engines(self):
        self.conversion_engines = detect_engines()
        self.conversion_router = ConversionRouter(self.conversion_engines)
        if hasattr(self, "conversion_engine_var"):
            self.conversion_engine_var.set(
                "已检测：" + engine_summary(self.conversion_engines)
            )

    def choose_conversion_files(self):
        paths = filedialog.askopenfilenames(
            title="选择要转换的文件",
            filetypes=(
                ("支持的文档", "*.pdf *.doc *.docx *.odt *.xls *.xlsx *.ods *.csv *.epub"),
                ("所有文件", "*.*"),
            ),
        )
        if paths:
            self.add_conversion_files(paths)

    def on_conversion_drop(self, event):
        try:
            paths = self.tk.splitlist(event.data)
        except tk.TclError:
            paths = (event.data,)
        self.add_conversion_files(paths)

    def add_conversion_files(self, paths):
        self.refresh_conversion_engines()
        existing = {
            str(Path(task["source"]).resolve()).lower()
            for task in self.conversion_tasks
        }
        added = 0
        added_ids = []
        rejected = []
        for value in paths:
            path = Path(value)
            if not path.is_file() or path.suffix.lower() not in SUPPORTED_EXTENSIONS:
                rejected.append(path.name or str(path))
                continue
            resolved = str(path.resolve())
            if resolved.lower() in existing:
                continue
            targets = self.conversion_router.targets_for(path)
            task = {
                "id": "convert-" + uuid.uuid4().hex,
                "source": resolved,
                "source_kind": source_kind(path),
                "targets": targets,
                "target": targets[0] if targets else "",
                "status": "等待" if targets else "缺少引擎",
                "progress": "0%" if targets else "不可用",
                "output": "",
                "error": "",
                "cancel_requested": False,
            }
            self.conversion_tasks.append(task)
            existing.add(resolved.lower())
            self._refresh_conversion_task(task)
            added_ids.append(task["id"])
            added += 1
        if added:
            # Select the first newly added file so its available target formats
            # are visible immediately instead of leaving an empty dropdown.
            selected_id = added_ids[0]
            self.conversion_tree.selection_set(selected_id)
            self.conversion_tree.focus(selected_id)
            self.conversion_tree.see(selected_id)
            self.conversion_selection_changed()
            self.conversion_status_var.set(
                f"已添加 {added} 个文件。已选中第一个任务，请确认目标格式后开始转换。"
            )
        if rejected:
            messagebox.showwarning(
                APP_NAME,
                "以下文件格式不受支持：\n\n" + "\n".join(rejected[:15]),
            )

    def _conversion_task(self, task_id):
        return next(
            (task for task in self.conversion_tasks if task["id"] == task_id),
            None,
        )

    def _refresh_conversion_task(self, task):
        source = Path(task["source"])
        values = (
            source.name,
            source.suffix.lower().lstrip(".").upper(),
            FORMAT_LABELS.get(task["target"], task["target"] or "不可用"),
            task["status"],
            task["progress"],
            task["output"] or "",
        )
        if task["status"] in {"准备", "转换", "保存"}:
            tag = "running"
        elif task["status"] == "完成":
            tag = "done"
        elif task["status"] in {"失败", "缺少引擎", "已取消"}:
            tag = "failed"
        else:
            tag = "waiting"
        if self.conversion_tree.exists(task["id"]):
            self.conversion_tree.item(task["id"], values=values, tags=(tag,))
        else:
            self.conversion_tree.insert(
                "", "end", iid=task["id"], values=values, tags=(tag,)
            )

    def conversion_selection_changed(self, _event=None):
        selected = self.conversion_tree.selection()
        if not selected:
            self.conversion_target_box.configure(values=())
            self.conversion_target_box.configure(state="disabled")
            self.conversion_apply_button.configure(state="disabled")
            self.conversion_target_var.set("请先选择文件")
            self.conversion_selection_hint_var.set("请先选择下方任务")
            return
        tasks = [
            self._conversion_task(task_id) for task_id in selected
            if self._conversion_task(task_id)
        ]
        common = set(tasks[0]["targets"]) if tasks else set()
        for task in tasks[1:]:
            common.intersection_update(task["targets"])
        ordered = [
            code for code in ("docx", "pdf", "epub", "xlsx", "csv")
            if code in common
        ]
        labels = tuple(FORMAT_LABELS[code] for code in ordered)
        self.conversion_target_box.configure(values=labels)
        if labels:
            self.conversion_target_box.configure(state="readonly")
            self.conversion_apply_button.configure(state="normal")
        else:
            self.conversion_target_box.configure(state="disabled")
            self.conversion_apply_button.configure(state="disabled")
        current = {task["target"] for task in tasks}
        if len(current) == 1 and next(iter(current), "") in common:
            self.conversion_target_var.set(
                FORMAT_LABELS.get(next(iter(current)), "")
            )
        else:
            self.conversion_target_var.set("无共同目标格式" if not labels else "请选择目标格式")
        source_labels = sorted({
            Path(task["source"]).suffix.lstrip(".").upper() for task in tasks
        })
        if len(tasks) == 1:
            available = "、".join(labels) if labels else "当前没有可用转换引擎"
            self.conversion_selection_hint_var.set(
                f"{source_labels[0]} 可转换为：{available}"
            )
        else:
            available = "、".join(labels) if labels else "没有共同目标格式"
            self.conversion_selection_hint_var.set(
                f"已选 {len(tasks)} 个文件｜共同可选：{available}"
            )

    def apply_conversion_target(self, _event=None):
        label = self.conversion_target_var.get()
        target = next(
            (code for code, text in FORMAT_LABELS.items() if text == label),
            "",
        )
        if not target:
            return
        changed = 0
        for task_id in self.conversion_tree.selection():
            task = self._conversion_task(task_id)
            if (
                task and target in task["targets"]
                and task["status"] not in {"转换", "准备", "保存", "完成"}
            ):
                task["target"] = target
                if task["status"] == "缺少引擎":
                    task["status"] = "等待"
                    task["progress"] = "0%"
                self._refresh_conversion_task(task)
                changed += 1
        if changed:
            self.conversion_status_var.set(
                f"已把 {changed} 个任务设置为 {label}。"
            )

    def choose_conversion_output(self):
        path = filedialog.askdirectory(
            initialdir=self.conversion_output_var.get()
        )
        if path:
            Path(path).mkdir(parents=True, exist_ok=True)
            self.conversion_output_var.set(path)
            self.store.data["conversion_output_dir"] = path
            self.store.save()

    @staticmethod
    def _unique_output_path(directory, stem, suffix):
        candidate = directory / (stem + suffix)
        index = 1
        while candidate.exists():
            candidate = directory / f"{stem} ({index}){suffix}"
            index += 1
        return candidate

    def start_conversion_queue(self):
        if self.current_conversion_task is not None:
            self.conversion_status_var.set("转换队列正在运行。")
            return
        waiting = [
            task for task in self.conversion_tasks
            if task["status"] == "等待" and task["target"]
        ]
        if not waiting:
            messagebox.showinfo(APP_NAME, "没有等待转换的任务。")
            return
        output = Path(self.conversion_output_var.get())
        try:
            output.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            messagebox.showerror(APP_NAME, f"无法创建输出目录：\n\n{exc}")
            return
        self.conversion_start_button.configure(state="disabled")
        self._start_next_conversion()

    def _start_next_conversion(self):
        next_task = next(
            (
                task for task in self.conversion_tasks
                if task["status"] == "等待" and task["target"]
            ),
            None,
        )
        if next_task is None:
            self.current_conversion_task = None
            self.conversion_process = None
            self.conversion_start_button.configure(state="normal")
            self.conversion_cancel_button.configure(state="disabled")
            completed = sum(
                task["status"] == "完成" for task in self.conversion_tasks
            )
            failed = sum(
                task["status"] == "失败" for task in self.conversion_tasks
            )
            self.conversion_status_var.set(
                f"批量转换结束：完成 {completed}，失败 {failed}。"
            )
            return
        next_task["cancel_requested"] = False
        next_task["status"] = "准备"
        next_task["progress"] = "10%"
        next_task["error"] = ""
        next_task["output_dir"] = self.conversion_output_var.get()
        self.current_conversion_task = next_task
        self.conversion_cancel_button.configure(state="normal")
        self._refresh_conversion_task(next_task)
        self.conversion_status_var.set(
            "正在转换：" + Path(next_task["source"]).name
        )
        threading.Thread(
            target=self._conversion_worker,
            args=(next_task,),
            daemon=True,
        ).start()

    def _conversion_worker(self, task):
        job_id = re.sub(r"[^A-Za-z0-9-]", "", task["id"]) or str(time.time_ns())
        job_dir = app_data_dir() / "convert_jobs" / ("job-" + job_id)
        log_dir = app_data_dir() / "conversion_logs"
        log_path = log_dir / ("convert-" + job_id + ".log")

        def on_stage(label, percent):
            self.events.put((
                "convert_stage", task["id"], label, int(percent)
            ))

        def set_process(process):
            self.conversion_process = process

        try:
            staged = self.conversion_router.convert(
                source=task["source"],
                target=task["target"],
                job_dir=job_dir,
                log_path=log_path,
                on_stage=on_stage,
                set_process=set_process,
                is_cancelled=lambda: bool(task.get("cancel_requested")),
            )
            if task.get("cancel_requested"):
                raise ConversionCancelled("任务已取消。")
            output_dir = Path(task["output_dir"])
            destination = self._unique_output_path(
                output_dir,
                clean_filename(Path(task["source"]).stem),
                "." + task["target"],
            )
            shutil.copy2(staged, destination)
            self.events.put((
                "convert_done", task["id"], str(destination), str(log_path)
            ))
        except ConversionCancelled:
            self.events.put(("convert_cancelled", task["id"], str(log_path)))
        except Exception as exc:
            if task.get("cancel_requested"):
                self.events.put(("convert_cancelled", task["id"], str(log_path)))
            else:
                self.events.put((
                    "convert_error", task["id"], str(exc), str(log_path)
                ))
        finally:
            self.conversion_process = None
            try:
                shutil.rmtree(job_dir)
            except OSError:
                pass

    def cancel_current_conversion(self):
        task = self.current_conversion_task
        if task is None:
            return
        task["cancel_requested"] = True
        process = self.conversion_process
        if process:
            self._terminate_conversion_process(process)
        self.conversion_status_var.set("正在取消当前转换……")

    @staticmethod
    def _terminate_conversion_process(process):
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            else:
                process.terminate()
        except OSError:
            pass

    def retry_failed_conversions(self):
        count = 0
        self.refresh_conversion_engines()
        for task in self.conversion_tasks:
            if task["status"] in {"失败", "已取消", "缺少引擎"}:
                targets = self.conversion_router.targets_for(task["source"])
                task["targets"] = targets
                if task["target"] not in targets:
                    task["target"] = targets[0] if targets else ""
                task["status"] = "等待" if task["target"] else "缺少引擎"
                task["progress"] = "0%" if task["target"] else "不可用"
                task["error"] = ""
                task["output"] = ""
                task["cancel_requested"] = False
                self._refresh_conversion_task(task)
                count += 1
        if count:
            self.start_conversion_queue()
        else:
            messagebox.showinfo(APP_NAME, "没有可重试的任务。")

    def remove_selected_conversions(self):
        selected = set(self.conversion_tree.selection())
        if not selected:
            return
        running_id = (
            self.current_conversion_task["id"]
            if self.current_conversion_task else ""
        )
        removable = selected - {running_id}
        self.conversion_tasks = [
            task for task in self.conversion_tasks
            if task["id"] not in removable
        ]
        for task_id in removable:
            if self.conversion_tree.exists(task_id):
                self.conversion_tree.delete(task_id)
        if running_id in selected:
            self.conversion_status_var.set("正在运行的任务不能直接移除。")

    def clear_completed_conversions(self):
        completed = {
            task["id"] for task in self.conversion_tasks
            if task["status"] == "完成"
        }
        self.conversion_tasks = [
            task for task in self.conversion_tasks
            if task["id"] not in completed
        ]
        for task_id in completed:
            if self.conversion_tree.exists(task_id):
                self.conversion_tree.delete(task_id)

    def open_conversion_output(self):
        path = Path(self.conversion_output_var.get())
        path.mkdir(parents=True, exist_ok=True)
        self.open_path(path)

    def open_conversion_result_from_event(self, event):
        row_id = self.conversion_tree.identify_row(event.y)
        task = self._conversion_task(row_id) if row_id else None
        if task and task["status"] == "完成" and task["output"]:
            self.open_path(Path(task["output"]))

    def paste_url(self):
        try:
            self.url_var.set(unwrap_redirect_url(extract_share_url(self.clipboard_get())))
        except tk.TclError:
            messagebox.showinfo(APP_NAME, "剪贴板中没有可用文本。")

    def _check_tools(self):
        required = (
            "yt-dlp.exe",
            "ffmpeg.exe",
            "funasr/cpu/llama-funasr-sensevoice.exe",
            "funasr/models/sensevoice-small-q8.gguf",
            "funasr/models/fsmn-vad.gguf",
        )
        missing = [name for name in required if not resource_path(name).exists()]
        if missing:
            self.status_var.set("缺少工具文件：" + "、".join(missing))
            self.parse_button.configure(state="disabled")
            self.download_button.configure(state="disabled")
            self.transcribe_button.configure(state="disabled")

    def parse_url(self):
        url = unwrap_redirect_url(
            self.login_retry_url or self.url_var.get().strip()
        )
        self.url_var.set(url)
        if not re.match(r"^https?://", url, re.I):
            messagebox.showwarning(APP_NAME, "请输入以 http:// 或 https:// 开头的媒体链接。")
            return
        self.parsed = None
        self.result_var.set("正在解析页面，请稍候……")
        self.status_var.set("正在解析")
        self.parse_button.configure(state="disabled")
        auth = {
            "source": self.cookie_source_var.get(),
            "cookie_file": self.cookie_file_var.get().strip(),
            "manual_cookie": self.manual_cookie,
        }
        cookie_arg_sets = self._browser_cookie_arg_sets()
        threading.Thread(
            target=self._parse_worker,
            args=(url, auth, cookie_arg_sets, bool(self.playlist_var.get())),
            daemon=True,
        ).start()

    def _browser_cookie_arg_sets(self):
        """Ordered browser sessions used only after a public extraction is blocked."""
        selected = self.cookie_source_var.get()
        candidates = [selected, "Edge 登录状态", "Chrome 登录状态", "Firefox 登录状态", "专用 Edge 登录窗口"]
        if self.cookie_file_var.get().strip():
            candidates.append("cookies.txt 文件")
        result = []
        for source in candidates:
            if source in {"不使用", "自动读取（本次）", "手动 Cookie（本次）"}:
                continue
            args = self._cookie_args_for(source, self.cookie_file_var.get().strip())
            if args and all(source != row[0] for row in result):
                result.append((source, args))
        return result

    def _cookie_args(self):
        return self._cookie_args_for(
            self.cookie_source_var.get(),
            self.cookie_file_var.get().strip(),
        )

    @staticmethod
    def _cookie_args_for(source, cookie_file=""):
        if source == "专用 Edge 登录窗口":
            profile = app_data_dir() / "edge-login-profile"
            return ["--cookies-from-browser", f"edge:{profile}"]
        browser_map = {
            "Edge 登录状态": "edge",
            "Chrome 登录状态": "chrome",
            "Firefox 登录状态": "firefox",
        }
        if source in browser_map:
            return ["--cookies-from-browser", browser_map[source]]
        if source == "cookies.txt 文件":
            path = cookie_file.strip()
            if path and Path(path).is_file():
                return ["--cookies", path]
        return []

    def _base_command(self, cookie_args=None):
        return [
            str(resource_path("yt-dlp.exe")), "--no-warnings",
            "--ffmpeg-location", str(resource_path("ffmpeg.exe")),
        ] + (self._cookie_args() if cookie_args is None else cookie_args)

    def _parse_worker(self, url, auth=None, cookie_args=None, playlist=False):
        if is_xiaoe_url(url):
            try:
                info = self._resolve_xiaoe(url, auth=auth)
                self.events.put(("parsed", info))
            except XiaoeLoginRequired as exc:
                self.events.put(("xiaoe_login_required", str(exc)))
            except Exception as exc:
                self.events.put(("parse_error", "小鹅通专用解析失败：" + str(exc)))
            return
        try:
            info = self.media_resolver.resolve(url, cookie_args or [], playlist)
            self.events.put(("parsed", info))
        except BrowserCaptureRequired as exc:
            self.events.put(("browser_required", url, str(exc)))
        except ProtectedMediaError as exc:
            self.events.put(("protected_media", str(exc)))
        except Exception as exc:
            self.events.put(("parse_error", str(exc)))

    def _close_dedicated_edge_processes(self):
        """Close only the Edge instance owned by the dedicated login profile."""
        profile = str((app_data_dir() / "edge-login-profile").resolve())
        process = self.login_browser_process
        if process is not None and process.poll() is None:
            try:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process.pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=15,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        if os.name == "nt":
            escaped = profile.replace("'", "''")
            script = (
                f"$profile='{escaped}'; "
                "Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
                "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } "
                "| ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue }"
            )
            try:
                subprocess.run(
                    ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=20,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
            except (OSError, subprocess.TimeoutExpired):
                pass
        self.login_browser_process = None

    @staticmethod
    def _dedicated_edge_running():
        """Return True while any Edge process still owns our login profile."""
        if os.name != "nt":
            return False
        profile = str((app_data_dir() / "edge-login-profile").resolve())
        escaped = profile.replace("'", "''")
        script = (
            f"$profile='{escaped}'; "
            "$match = Get-CimInstance Win32_Process -Filter \"Name='msedge.exe'\" "
            "| Where-Object { $_.CommandLine -and $_.CommandLine.Contains($profile) } "
            "| Select-Object -First 1; if ($match) { '1' } else { '0' }"
        )
        try:
            result = subprocess.run(
                ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            return result.stdout.strip().endswith("1")
        except (OSError, subprocess.TimeoutExpired):
            return False

    def _export_browser_cookies(self, url, source=None, cookie_file=""):
        if source is None:
            source = self.cookie_source_var.get()
            cookie_file = self.cookie_file_var.get().strip()
        args = self._cookie_args_for(source, cookie_file)
        if not args or args[0] != "--cookies-from-browser":
            return None
        fd, cookie_path = tempfile.mkstemp(prefix="audio-xhl-", suffix=".txt")
        os.close(fd)
        Path(cookie_path).unlink(missing_ok=True)
        cmd = [
            str(resource_path("yt-dlp.exe")), "--no-warnings",
            *args, "--cookies", cookie_path, "--skip-download", "--no-playlist", url,
        ]

        def export_once():
            try:
                Path(cookie_path).unlink(missing_ok=True)
            except OSError:
                pass
            return subprocess.run(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                encoding="utf-8", errors="replace", timeout=90,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )

        result = export_once()
        detail = (result.stderr or result.stdout or "").strip()
        if "Could not copy Chrome cookie database" in detail:
            if source == "专用 Edge 登录窗口":
                self._close_dedicated_edge_processes()
                for delay in (0.8, 1.5, 2.5):
                    time.sleep(delay)
                    result = export_once()
                    detail = (result.stderr or result.stdout or "").strip()
                    if Path(cookie_path).is_file() and Path(cookie_path).stat().st_size > 0:
                        return Path(cookie_path)
                    if "Could not copy Chrome cookie database" not in detail:
                        break
                raise RuntimeError(
                    "专用登录窗口已自动关闭，但登录数据仍未释放。请等待几秒后再点一次。"
                )
            raise RuntimeError(
                "浏览器正在占用 Cookie 数据库。建议改选“专用 Edge 登录窗口”；"
                "也可以彻底退出当前浏览器后重试。"
            )
        if Path(cookie_path).is_file() and Path(cookie_path).stat().st_size > 0:
            return Path(cookie_path)
        if "could not find" in detail.lower() and "cookies" in detail.lower():
            raise RuntimeError("没有找到登录数据。请先点击“打开登录窗口”并完成登录。")
        last_line = detail.splitlines()[-1] if detail else "未知错误"
        raise RuntimeError("无法读取浏览器登录状态：" + last_line.replace("ERROR: ", ""))
    @staticmethod
    def _xiaoe_shop_prefix(host):
        host = (host or "").lower().split(":", 1)[0]
        first = host.split(".", 1)[0]
        return first if first.startswith("app") else ""

    @staticmethod
    def _has_xiaoe_login_cookie(cookie_header):
        names = {
            part.split("=", 1)[0].strip().lower()
            for part in (cookie_header or "").split(";")
            if "=" in part
        }
        known = {
            "ko_token", "pc_user_key",
            "access_token", "user_token", "login_token",
            # Newer Xiaoe H5 shops complete cross-domain sign-in with
            # anony_token.  It may represent an authorized H5 session even
            # though the legacy ko_token is not written for this host.
            "anony_token",
        }
        return bool(names & known)

    @classmethod
    def _cookie_header_from_file(cls, cookie_path, host):
        host = (host or "").lower().split(":", 1)[0]
        shop_prefix = cls._xiaoe_shop_prefix(host)
        # Keep one value per cookie name. Exact-host cookies take precedence,
        # then equivalent domains used by the same Xiaoe shop.
        selected = {}
        now = int(time.time())
        for raw in Path(cookie_path).read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw[10:] if raw.startswith("#HttpOnly_") else raw
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 7:
                continue
            domain, _flag, _path, _secure, expiry, name, value = fields[:7]
            domain = domain.lstrip(".").lower()
            exact = host == domain or host.endswith("." + domain)
            same_shop = (
                bool(shop_prefix)
                and domain.split(".", 1)[0] == shop_prefix
                and any(marker in domain for marker in (
                    ".xet.pomoho.com", ".xiaoeknow.com",
                    ".xiaoecloud.com", ".xiaoe-tech.com",
                    ".xet-pc.citv.cn",
                ))
            )
            shared_platform = domain in {"pomoho.com", "xiaoe-tech.com"}
            if not (exact or same_shop or shared_platform):
                continue
            try:
                if expiry and int(expiry) not in (0,) and int(expiry) < now:
                    continue
            except ValueError:
                pass
            priority = 3 if exact else (2 if same_shop else 1)
            current = selected.get(name)
            if current is None or priority > current[0]:
                selected[name] = (priority, value)
        return "; ".join(f"{name}={item[1]}" for name, item in selected.items())

    def _xiaoe_cookie_header(self, url, source=None, cookie_file="", manual_cookie=None):
        if source is None:
            source = self.cookie_source_var.get()
            cookie_file = self.cookie_file_var.get().strip()
            manual_cookie = self.manual_cookie
        if source == "不使用":
            raise RuntimeError("该小鹅通页面需要登录。请在“登录状态”选择你已登录小鹅通的浏览器。")
        if source in ("自动读取（本次）", "手动 Cookie（本次）"):
            if not manual_cookie:
                raise RuntimeError("尚未读取 Cookie。请先从已登录课程页面复制 Cookie，再点击“读取剪贴板 Cookie”。")
            return manual_cookie
        temporary = None
        try:
            if source == "cookies.txt 文件":
                cookie_path = Path(cookie_file.strip())
                if not cookie_path.is_file():
                    raise RuntimeError("请先选择有效的 cookies.txt 文件。")
            else:
                temporary = self._export_browser_cookies(url, source, cookie_file)
                cookie_path = temporary
            header = self._cookie_header_from_file(cookie_path, urlparse(url).hostname or "")
            if not header:
                raise RuntimeError("没有找到该小鹅通站点的 Cookie。请确认所选浏览器已经登录并能播放此课程。")
            if not self._has_xiaoe_login_cookie(header):
                host = urlparse(url).hostname or "当前课程站点"
                raise XiaoeLoginRequired(
                    f"读取到了 {host} 的浏览记录，但没有读取到该店铺的有效登录令牌。\n\n"
                    "这通常表示：你登录的是另一个小鹅通店铺，或登录发生在其他浏览器中。"
                )
            return header
        finally:
            if temporary:
                try:
                    temporary.unlink()
                except OSError:
                    pass

    @staticmethod
    def _json_get(url, cookie_header):
        request = Request(url, headers={
            "Cookie": cookie_header,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Referer": url,
        })
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            raise RuntimeError(f"小鹅通接口返回 HTTP {exc.code}，登录状态可能已过期。")
        except URLError as exc:
            raise RuntimeError("无法连接小鹅通接口：" + str(exc.reason))

    @staticmethod
    def _json_post(url, params, cookie_header, referer):
        body = urlencode({f"bizData[{key}]": value for key, value in params.items()}).encode("utf-8")
        parsed = urlparse(url)
        request = Request(url, data=body, headers={
            "Cookie": cookie_header,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131 Safari/537.36",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
            "Origin": f"{parsed.scheme}://{parsed.netloc}",
            "Referer": referer,
        })
        try:
            with urlopen(request, timeout=30) as response:
                return json.loads(response.read().decode("utf-8", errors="replace"))
        except HTTPError as exc:
            raise RuntimeError(f"小鹅通接口返回 HTTP {exc.code}，登录状态可能已过期。")
        except URLError as exc:
            raise RuntimeError("无法连接小鹅通接口：" + str(exc.reason))

    @staticmethod
    def _validate_xiaoe_response(payload):
        if not isinstance(payload, dict):
            raise RuntimeError("小鹅通接口返回了无法识别的数据。")
        code = payload.get("code")
        message = str(payload.get("message") or payload.get("msg") or "").strip()
        if code in (None, 0, "0", 200, "200"):
            return
        login_or_permission = (
            str(code) in {"1", "11302"}
            and any(text in message.lower() for text in (
                "权限", "登录", "login", "permission", "unauthorized",
            ))
        )
        if (
            str(code) == "11302"
            or "auth.login" in message.lower()
            or "login" in message.lower()
            or login_or_permission
        ):
            raise XiaoeLoginRequired(
                "当前读取到的账号没有获得这个课程店铺的访问状态。\n\n"
                "请在即将打开的专用 Edge 中直接打开这一节课程，"
                "按页面提供的方式登录，并确认视频能够正常播放。"
            )
        raise RuntimeError(f"小鹅通接口拒绝请求（{code}）：{message or '未知原因'}")

    @classmethod
    def _find_line_sharpness(cls, value):
        if isinstance(value, dict):
            sharpness = value.get("line_sharpness")
            if isinstance(sharpness, list) and sharpness:
                return sharpness
            for child in value.values():
                found = cls._find_line_sharpness(child)
                if found:
                    return found
        elif isinstance(value, list):
            for child in value:
                found = cls._find_line_sharpness(child)
                if found:
                    return found
        return []

    @staticmethod
    def _xiaoe_resource_id(parsed, prefix):
        parts = [part for part in parsed.path.split("/") if part]
        return next((part for part in reversed(parts) if part.startswith(prefix)), "")

    def _resolve_xiaoe(self, url, auth=None):
        url = unwrap_redirect_url(url)
        cookie = self._xiaoe_cookie_header(url, **auth) if auth is not None else self._xiaoe_cookie_header(url)
        return self._resolve_xiaoe_with_cookie(url, cookie)

    def _resolve_xiaoe_with_cookie(self, url, cookie):
        """Validate one exact session against the requested chapter and reuse its result."""
        url = unwrap_redirect_url(url)
        parsed = urlparse(url)
        if self._xiaoe_resource_id(parsed, "v_") or "/p/course/video/" in parsed.path.lower():
            return self._resolve_xiaoe_video(url, cookie)
        if self._xiaoe_resource_id(parsed, "l_"):
            return self._resolve_xiaoe_live(url, cookie)
        raise RuntimeError(
            "这是小鹅通课程目录页，不是具体音视频章节。"
            "请先在浏览器中打开要播放的那一节，再复制地址栏里的完整链接。"
        )

    def _resolve_xiaoe_live(self, url, cookie=None):
        url = unwrap_redirect_url(url)
        parsed = urlparse(url)
        resource_id = self._xiaoe_resource_id(parsed, "l_")
        if not resource_id:
            raise RuntimeError("没有从小鹅通地址中识别出直播或回放编号。")
        query = parse_qs(parsed.query)
        app_id = (query.get("app_id") or [""])[0]
        if not app_id:
            first_host = (parsed.hostname or "").split(".")[0]
            if first_host.startswith("app"):
                app_id = first_host
        if not app_id:
            raise RuntimeError("没有从小鹅通地址中识别出 app_id。")
        if cookie is None:
            cookie = self._xiaoe_cookie_header(url)
        origin = f"{parsed.scheme}://{parsed.netloc}"
        base_url = (
            f"{origin}/_alive/v3/base_info?resource_id={resource_id}"
            "&type=12&is_direct=1&file_tag=1"
        )
        lookback_url = f"{origin}/_alive/v3/get_lookback_list?app_id={app_id}&alive_id={resource_id}"
        base = self._json_get(base_url, cookie)
        lookback = self._json_get(lookback_url, cookie)
        self._validate_xiaoe_response(base)
        self._validate_xiaoe_response(lookback)
        base_data = base.get("data") or {}
        title = (
            ((base_data.get("alive_info") or {}).get("title") if isinstance(base_data, dict) else None)
            or resource_id
        )
        sharpness = self._find_line_sharpness(lookback.get("data"))
        media_url = next(
            (item.get("url") for item in sharpness if isinstance(item, dict) and item.get("url")),
            None,
        )
        if not media_url:
            raise RuntimeError("登录成功，但该课程没有可用回放；直播可能尚未结束或回放未开放。")
        if not str(media_url).startswith(("http://", "https://")):
            raise RuntimeError("该回放地址使用了当前版本尚不支持的加密封装。")
        return {
            "title": title,
            "duration": None,
            "extractor": "小鹅通回放",
            "count": 1,
            "media_url": media_url,
            "source_url": url,
            # Keep the authorized session only in memory so queued jobs can
            # refresh short-lived signed playback URLs immediately before use.
            "xiaoe_refresh": {
                "kind": "live",
                "origin": origin,
                "app_id": app_id,
                "resource_id": resource_id,
                "cookie": cookie,
                "referer": url,
            },
        }

    def _xiaoe_video_detail(self, refresh):
        # Xiaoe currently serves both endpoint families.  Newer H5 shops use
        # business.video, while older shops still use business_go.video.
        # Try both before reporting an authentication failure.
        endpoints = (
            "xe.course.business.video.detail_info.get/2.0.0",
            "xe.course.business_go.video.detail_info.get/2.0.0",
        )
        errors = []
        for endpoint in endpoints:
            try:
                payload = self._json_post(
                    f"{refresh['origin']}/{endpoint}",
                    refresh["params"],
                    refresh["cookie"],
                    refresh["referer"],
                )
                self._validate_xiaoe_response(payload)
                data = payload.get("data") or {}
                if not isinstance(data, dict):
                    raise RuntimeError("视频章节接口没有返回有效数据。")
                if data.get("is_available") in (0, False):
                    raise XiaoeLoginRequired(
                        "当前账号没有获得这一节的播放信息。请在专用 Edge 中打开该章节，"
                        "确认能够正常播放后，关闭整个专用窗口再重新解析。"
                    )
                return data
            except (XiaoeLoginRequired, RuntimeError) as exc:
                errors.append(exc)
        if errors:
            raise errors[-1]
        raise RuntimeError("小鹅通视频接口没有返回有效数据。")

    def _resolve_xiaoe_video(self, url, cookie=None):
        parsed = urlparse(url)
        resource_id = self._xiaoe_resource_id(parsed, "v_")
        if not resource_id:
            raise RuntimeError("没有从小鹅通地址中识别出视频章节编号。")
        query = parse_qs(parsed.query, keep_blank_values=True)
        params = {
            "resource_id": resource_id,
            "product_id": (query.get("product_id") or [""])[0],
            "opr_sys": "Win32",
        }
        course_id = (query.get("course_id") or [""])[0]
        sub_course_id = (query.get("sub_course_id") or [""])[0]
        if course_id:
            params["course_id"] = course_id
        if sub_course_id:
            params["sub_course_id"] = sub_course_id
        origin = f"{parsed.scheme}://{parsed.netloc}"
        refresh = {
            "kind": "video",
            "origin": origin,
            "resource_id": resource_id,
            "cookie": cookie if cookie is not None else self._xiaoe_cookie_header(url),
            "referer": url,
            "params": params,
        }
        data = self._xiaoe_video_detail(refresh)
        video_info = data.get("video_info") or {}
        if not isinstance(video_info, dict):
            video_info = {}
        media_url = video_info.get("video_audio_url")
        if not media_url:
            if video_info.get("is_drm"):
                raise RuntimeError(
                    "该章节的视频使用 DRM，且官方接口没有提供可下载的授权音频轨。"
                    "当前版本不会绕过 DRM。"
                )
            raise RuntimeError("登录成功，但该视频章节没有返回可用音频轨。")
        if not str(media_url).startswith(("http://", "https://")):
            raise RuntimeError("该章节的音频地址使用了当前版本尚不支持的封装。")
        return {
            "title": video_info.get("title") or video_info.get("file_name") or resource_id,
            "duration": video_info.get("video_length"),
            "extractor": "小鹅通视频音轨",
            "count": 1,
            "media_url": media_url,
            "source_url": url,
            "xiaoe_refresh": refresh,
        }

    def start_download(self):
        source_url = unwrap_redirect_url(self.url_var.get().strip())
        url = source_url
        if self.parsed and self.parsed.get("media_url"):
            url = self.parsed["media_url"]
        if not re.match(r"^https?://", url, re.I):
            messagebox.showwarning(APP_NAME, "请先输入有效链接。")
            return
        title = clean_filename(self.name_var.get() or (self.parsed or {}).get("title") or "音频")
        fmt = self.format_var.get()
        out_dir = Path(self.dir_var.get())
        out_dir.mkdir(parents=True, exist_ok=True)
        original_title = title
        suffix = 2
        reserved = {
            (item["dir"], item["title"])
            for item in list(self.active_downloads.values()) + self.pending_downloads
        }
        while (
            (str(out_dir), title) in reserved
            or (out_dir / f"{title}.{fmt}").exists()
            or (self.playlist_var.get() and (out_dir / title).exists())
        ):
            title = f"{original_title}（{suffix}）"
            suffix += 1
        task = {
            "id": str(time.time_ns()),
            "title": title, "format": fmt, "url": url,
            "source_url": source_url, "dir": str(out_dir),
            "quality": self.quality_var.get(),
            "playlist": self.playlist_var.get(),
            "cookie_args": list((self.parsed or {}).get("cookie_args") or self._cookie_args()),
            "http_headers": dict((self.parsed or {}).get("http_headers") or {}),
            "xiaoe_refresh": dict((self.parsed or {}).get("xiaoe_refresh") or {}),
            "auto_transcribe": bool(self.auto_transcribe_var.get()),
            "transcript_model_label": self.transcript_model_var.get(),
            "transcript_language_label": self.transcript_language_var.get(),
            "word_timestamps": bool(self.word_timestamps_var.get()),
        }
        # The dedicated parser has already obtained an authorized, short-lived
        # CDN URL. Re-exporting browser cookies here is unnecessary and can fail
        # merely because the login window is still open.
        if task["xiaoe_refresh"]:
            task["cookie_args"] = []
        if len(self.active_downloads) >= self.max_parallel_downloads:
            self.pending_downloads.append(task)
            self._refresh_download_queue()
            self.clear_form()
            self.status_var.set(
                f"已加入队列：{title}（当前已有 {len(self.active_downloads)} 个任务并行下载）"
            )
            return
        self._launch_download_task(task)
        self.clear_form()
        self.status_var.set(
            f"已开始下载：{title}（并行 {len(self.active_downloads)}/{self.max_parallel_downloads}）"
        )

    def _launch_download_task(self, task):
        self.active_downloads[task["id"]] = task
        self.download_busy = True
        self.active_task = task
        self.progress["value"] = 0
        self.active_title.configure(
            text=f"正在下载 {len(self.active_downloads)} 个任务：{task['title']}"
        )
        self.active_detail.configure(
            text=f"{task['format'].upper()} · 并行 {len(self.active_downloads)}/{self.max_parallel_downloads} · 正在准备"
        )
        self.cancel_button.configure(state="normal")
        self._append_log(f"\n===== 开始任务：{task['title']} =====")
        self.show_page("active")
        self._refresh_download_queue()
        threading.Thread(target=self._download_worker, args=(task.copy(),), daemon=True).start()

    def _start_next_download(self, finished_task_id):
        self.active_downloads.pop(finished_task_id, None)
        self.download_processes.pop(finished_task_id, None)
        while self.pending_downloads and len(self.active_downloads) < self.max_parallel_downloads:
            next_task = self.pending_downloads.pop(0)
            self._launch_download_task(next_task)
            self.status_var.set(f"自动开始下一个任务：{next_task['title']}")
        self.download_busy = bool(self.active_downloads)
        if self.active_downloads:
            self.active_task = next(reversed(self.active_downloads.values()))
            self.active_title.configure(text=f"正在并行下载 {len(self.active_downloads)} 个任务")
            self.cancel_button.configure(state="normal")
        else:
            self.active_task = None
            self.active_title.configure(text="暂无下载任务")
            self.active_detail.configure(text="")
            self.cancel_button.configure(state="disabled")
        self._refresh_download_queue()

    def _refresh_download_queue(self):
        if not hasattr(self, "download_queue_var"):
            return
        count = len(self.pending_downloads)
        if count:
            preview = " → ".join(task["title"] for task in self.pending_downloads[:3])
            extra = f" 等 {count} 个任务" if count > 3 else ""
            self.download_queue_var.set(
                f"并行下载：{len(self.active_downloads)}/{self.max_parallel_downloads} · "
                f"等待队列：{count} 个任务 · {preview}{extra}"
            )
        else:
            self.download_queue_var.set(
                f"并行下载：{len(self.active_downloads)}/{self.max_parallel_downloads} · 等待队列：0 个任务"
            )

    def clear_pending_downloads(self):
        if not self.pending_downloads:
            messagebox.showinfo(APP_NAME, "等待队列已经是空的。")
            return
        if messagebox.askyesno(APP_NAME, f"确定移除等待队列中的 {len(self.pending_downloads)} 个任务吗？\n当前下载不会受到影响。"):
            self.pending_downloads.clear()
            self._refresh_download_queue()

    @staticmethod
    def _sanitize_log_line(value):
        text = str(value or "")
        text = re.sub(
            r"https?://[^\s\"']+",
            lambda match: redact_url(match.group(0)),
            text,
        )
        text = re.sub(
            r"(?i)\b(cookie|authorization|x-token|token|signature|sig|key)\s*[:=]\s*[^\s,;]+",
            lambda match: match.group(1) + "=<已隐藏>",
            text,
        )
        return text

    def _download_worker(self, task):
        refresh = task.get("xiaoe_refresh") or {}
        if refresh:
            try:
                if refresh.get("kind") == "video":
                    detail = self._xiaoe_video_detail(refresh)
                    video_info = detail.get("video_info") or {}
                    fresh_url = video_info.get("video_audio_url") if isinstance(video_info, dict) else None
                    media_name = "视频音频轨"
                else:
                    lookback_url = (
                        f"{refresh['origin']}/_alive/v3/get_lookback_list"
                        f"?app_id={refresh['app_id']}&alive_id={refresh['resource_id']}"
                    )
                    lookback = self._json_get(lookback_url, refresh["cookie"])
                    self._validate_xiaoe_response(lookback)
                    sharpness = self._find_line_sharpness(lookback.get("data"))
                    fresh_url = next(
                        (item.get("url") for item in sharpness if isinstance(item, dict) and item.get("url")),
                        None,
                    )
                    media_name = "回放地址"
                if fresh_url and str(fresh_url).startswith(("http://", "https://")):
                    task["url"] = fresh_url
                else:
                    raise RuntimeError(f"未获得新的{media_name}")
            except Exception as exc:
                self.events.put((
                    "download_error", task,
                    "小鹅通播放地址刷新失败：" + str(exc)
                    + "\n请在专用 Edge 登录窗口确认课程仍可播放后重试。",
                ))
                return
        quality = task["quality"]
        log_dir = app_data_dir() / "logs"
        log_dir.mkdir(parents=True, exist_ok=True)
        log_path = log_dir / (
            "download-" + datetime.now().strftime("%Y%m%d-%H%M%S-%f") + "-"
            + clean_filename(task["title"])[:48] + ".log"
        )
        output_template = str(Path(task["dir"]) / (task["title"] + ".%(ext)s"))
        if task["playlist"]:
            output_template = str(Path(task["dir"]) / task["title"] / "%(playlist_index)03d - %(title)s.%(ext)s")
        cmd = [
            str(resource_path("yt-dlp.exe")), "--no-warnings",
            "--ffmpeg-location", str(resource_path("ffmpeg.exe")),
            *task["cookie_args"],
            "--newline", "--windows-filenames", "--embed-metadata",
            "--retries", "20", "--fragment-retries", "20",
            "--retry-sleep", "fragment:2",
            "--extract-audio", "--audio-format", task["format"],
            "--print", "after_move:__FINAL_FILE__:%(filepath)s",
            "-o", output_template,
        ]
        if task.get("source_url") and task["source_url"] != task["url"]:
            cmd += ["--referer", task["source_url"]]
        allowed_headers = {
            "accept", "accept-language", "origin", "referer", "user-agent",
            "range", "sec-fetch-dest", "sec-fetch-mode", "sec-fetch-site",
            "cookie", "authorization",
        }
        for key, value in (task.get("http_headers") or {}).items():
            key = str(key).strip()
            value = str(value).strip()
            if key.lower() in allowed_headers and value and "\n" not in value and "\r" not in value:
                cmd += ["--add-header", f"{key}:{value}"]
        if quality != "最佳原始质量" and task["format"] in ("mp3", "m4a", "opus"):
            cmd += ["--audio-quality", quality]
        if not task["playlist"]:
            cmd.append("--no-playlist")
        cmd.append(task["url"])
        final_path = None
        recent_lines = []
        try:
            with log_path.open("w", encoding="utf-8", errors="replace") as log_file:
                log_file.write(f"{APP_NAME} {APP_VERSION}\n")
                log_file.write(f"任务：{task['title']}\n")
                log_file.write(
                    "来源：" + redact_url(task.get("source_url") or task["url"]) + "\n\n"
                )
                log_file.flush()
                process = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                           text=True, encoding="utf-8", errors="replace", bufsize=1,
                                           creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
                self.download_processes[task["id"]] = process
                for raw in process.stdout:
                    line = raw.strip()
                    if not line:
                        continue
                    safe_line = self._sanitize_log_line(line)
                    log_file.write(safe_line + "\n")
                    log_file.flush()
                    recent_lines.append(safe_line)
                    recent_lines = recent_lines[-12:]
                    if line.startswith("__FINAL_FILE__:"):
                        final_path = line.split(":", 1)[1]
                    match = re.search(r"\[download\]\s+([0-9.]+)%", line)
                    if match:
                        self.events.put(("progress", task["id"], float(match.group(1)), line))
                    elif "ExtractAudio" in line or "Post-process" in line:
                        self.events.put(("progress", task["id"], 98.0, line))
                    self.events.put(("log", f"[{task['title']}] {safe_line}"))
            code = process.wait()
            if code != 0:
                meaningful = next(
                    (line for line in reversed(recent_lines)
                     if "ERROR:" in line or "HTTP Error" in line or "failed" in line.lower()),
                    recent_lines[-1] if recent_lines else f"下载器退出代码 {code}",
                )
                raise RuntimeError(
                    "下载或音频转换失败：\n"
                    + meaningful
                    + f"\n\n详细日志：{log_path}"
                )
            # yt-dlp writes the correct Unicode filename on disk, but its
            # console marker can be decoded with the wrong Windows code page.
            # Never pass that mojibake path to the transcription engine.
            if not final_path or not Path(final_path).is_file():
                candidates = sorted(Path(task["dir"]).glob(task["title"] + ".*"), key=lambda p: p.stat().st_mtime, reverse=True)
                final_path = str(candidates[0]) if candidates else str(Path(task["dir"]) / (task["title"] + "." + task["format"]))
            self.events.put(("done", task, final_path))
        except Exception as exc:
            self.events.put(("download_error", task, str(exc)))
        finally:
            self.download_processes.pop(task["id"], None)

    def cancel_download(self):
        processes = list(self.download_processes.values())
        if processes and messagebox.askyesno(APP_NAME, f"确定取消全部 {len(processes)} 个并行下载吗？"):
            self.pending_downloads.clear()
            for process in processes:
                try:
                    process.terminate()
                except Exception:
                    pass
            self.status_var.set("已请求取消全部下载")

    def _drain_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "ocr_stage":
                    task = self._ocr_task(event[1])
                    if task:
                        task["status"] = event[2]
                        task["progress"] = event[3]
                        self._refresh_ocr_task(task)
                elif kind == "ocr_done":
                    task = self._ocr_task(event[1])
                    if task:
                        task["status"] = "完成"
                        task["progress"] = "100%"
                        task["output"] = event[2]
                        self._refresh_ocr_task(task)
                        self.ocr_status_var.set(
                            f"识别完成：{Path(task['source']).name}（{event[3]} 个字符）"
                        )
                    self.current_ocr_task = None
                    self._start_next_ocr()
                elif kind == "ocr_error":
                    task = self._ocr_task(event[1])
                    if task:
                        task["status"] = "失败"
                        task["progress"] = "失败"
                        task["error"] = event[2]
                        task["output"] = "错误：" + event[2].replace("\n", " ")[:180]
                        self._refresh_ocr_task(task)
                        self.ocr_status_var.set(
                            f"{Path(task['source']).name} 识别失败，已继续下一张。"
                        )
                    self.current_ocr_task = None
                    self._start_next_ocr()
                elif kind == "ocr_cancelled":
                    task = self._ocr_task(event[1])
                    if task:
                        task["status"] = "已取消"
                        task["progress"] = "已取消"
                        self._refresh_ocr_task(task)
                    self.current_ocr_task = None
                    self._start_next_ocr()
                elif kind == "convert_stage":
                    task = self._conversion_task(event[1])
                    if task:
                        task["status"] = event[2]
                        task["progress"] = f"{event[2]} {event[3]}%"
                        self._refresh_conversion_task(task)
                elif kind == "convert_done":
                    task = self._conversion_task(event[1])
                    if task:
                        task["status"] = "完成"
                        task["progress"] = "100%"
                        task["output"] = event[2]
                        task["log"] = event[3]
                        self._refresh_conversion_task(task)
                        self.conversion_status_var.set(
                            "转换完成：" + Path(task["source"]).name
                        )
                    self.current_conversion_task = None
                    self._start_next_conversion()
                elif kind == "convert_error":
                    task = self._conversion_task(event[1])
                    if task:
                        task["status"] = "失败"
                        task["progress"] = "失败"
                        task["error"] = event[2]
                        task["log"] = event[3]
                        task["output"] = "错误：" + event[2].replace("\n", " ")[:180]
                        self._refresh_conversion_task(task)
                        self.conversion_status_var.set(
                            f"{Path(task['source']).name} 转换失败，已继续下一项。"
                        )
                    self.current_conversion_task = None
                    self._start_next_conversion()
                elif kind == "convert_cancelled":
                    task = self._conversion_task(event[1])
                    if task:
                        task["status"] = "已取消"
                        task["progress"] = "已取消"
                        task["log"] = event[2]
                        self._refresh_conversion_task(task)
                    self.current_conversion_task = None
                    self._start_next_conversion()
                elif kind == "xiaoe_authorized":
                    cookie, url, matched_source, info = event[1:5]
                    self.manual_cookie = cookie
                    self.url_var.set(url)
                    self.auth_verified_host = urlparse(url).hostname or ""
                    shop = self._xiaoe_shop_prefix(self.auth_verified_host)
                    if shop:
                        source_map = self.store.data.setdefault("xiaoe_source_by_shop", {})
                        source_map[shop] = matched_source
                    self.cookie_source_var.set("自动读取（本次）")
                    self.store.data["cookie_source"] = "自动读取（本次）"
                    self.store.save()
                    self._refresh_cookie_controls()
                    self.auto_cookie_button.configure(state="normal")
                    self.parse_button.configure(state="normal")
                    self._apply_parsed_info(info, matched_source)
                elif kind == "cookie_ready":
                    # Compatibility with an older queued event. New Xiaoe flows
                    # validate access inside the worker before reaching the UI.
                    self.manual_cookie = event[1]
                    self.url_var.set(event[2])
                    matched_source = event[3] if len(event) > 3 else "现有登录状态"
                    self.cookie_source_var.set("自动读取（本次）")
                    self.store.data["cookie_source"] = "自动读取（本次）"
                    self.store.save()
                    self._refresh_cookie_controls()
                    self.auto_cookie_button.configure(state="normal")
                    self.parse_button.configure(state="normal")
                    self.status_var.set(
                        f"已从{matched_source}找到当前店铺登录状态，正在验证访问权限……"
                    )
                    self.parse_url()
                elif kind == "xiaoe_cookie_login_required":
                    self.auto_workflow_pending = False
                    self.manual_cookie = ""
                    self.auth_verified_host = ""
                    self.auto_cookie_button.configure(state="normal")
                    self.parse_button.configure(state="normal")
                    self.result_var.set("需要登录当前课程店铺")
                    self.status_var.set("没有找到与当前课程匹配的登录状态。")
                    self.cookie_source_var.set("专用 Edge 登录窗口")
                    self.store.data["cookie_source"] = "专用 Edge 登录窗口"
                    self.store.save()
                    self._refresh_cookie_controls()
                    prompt = (
                        event[1]
                        + "\n\n现在打开当前课程的专用登录窗口吗？\n"
                        "登录并确认视频可播放后，只需关闭窗口，软件会自动重新解析。"
                    )
                    if messagebox.askyesno(APP_NAME, prompt):
                        self.login_retry_url = event[2]
                        self.login_retry_pending = True
                        self.open_dedicated_login()
                elif kind == "cookie_read_error":
                    self.auto_workflow_pending = False
                    self.auto_cookie_button.configure(state="normal")
                    self.parse_button.configure(state="normal")
                    self.status_var.set("自动读取 Cookie 失败：" + event[1])
                    messagebox.showerror(
                        APP_NAME,
                        "自动读取网页 Cookie 失败：\n\n" + event[1]
                        + "\n\n请确认所选浏览器能播放该课程；如果使用专用 Edge，"
                        "请先关闭整个专用登录窗口再重试。",
                    )
                elif kind == "batch_catalog_ready":
                    cookie, items = event[1], event[2]
                    self.manual_cookie = cookie
                    self.cookie_source_var.set("自动读取（本次）")
                    self.store.data["cookie_source"] = "自动读取（本次）"
                    self.store.save()
                    self._refresh_cookie_controls()
                    self.batch_button.configure(state="normal")
                    self.status_var.set(f"已读取本课程 {len(items)} 个可选视频。")
                    self._show_batch_selector(items, cookie)
                elif kind == "batch_catalog_error":
                    self.batch_button.configure(state="normal")
                    self.status_var.set("读取课程视频列表失败：" + event[1])
                    messagebox.showerror(APP_NAME, "读取课程视频列表失败：\n\n" + event[1])
                elif kind == "batch_resolved":
                    infos, failed = event[1], event[2]
                    self.batch_button.configure(state="normal")
                    self.transcript_model_var.set("SenseVoice Q8（极速精准·离线）")
                    self.word_timestamps_var.set(False)
                    self.auto_transcribe_var.set(True)
                    self._save_auto_transcribe()
                    for info in infos:
                        self.url_var.set(info["source_url"])
                        self.parsed = info
                        self.name_var.set(clean_filename(info["title"]))
                        self.start_download()
                    message = f"已加入 {len(infos)} 个下载任务，最多 {self.max_parallel_downloads} 个并行。"
                    if failed:
                        message += f" 另有 {len(failed)} 个章节解析失败。"
                    self.status_var.set(message)
                    self.show_page("active")
                    if failed:
                        messagebox.showwarning(
                            APP_NAME,
                            message + "\n\n失败章节：\n" + "\n".join(failed[:10]),
                        )
                elif kind == "batch_resolve_error":
                    self.batch_button.configure(state="normal")
                    self.status_var.set("批量任务准备失败：" + event[1])
                    messagebox.showerror(APP_NAME, "批量任务准备失败：\n\n" + event[1])
                elif kind == "parsed":
                    self._apply_parsed_info(event[1])
                elif kind == "browser_required":
                    url, detail = event[1], event[2]
                    self.auto_workflow_pending = False
                    self.result_var.set("需要浏览器辅助 · 请播放视频")
                    self._set_resolver_badge("浏览器辅助", "#9a631c", "#fbf1df")
                    self.status_var.set(detail)
                    self._enqueue_browser_capture({
                        "title": self.media_resolver.platform_name(url) + "逐字稿",
                        "source_url": url, "download_url": url,
                        "resolver_state": "需要播放捕获", "subtitle": None,
                    })
                elif kind == "protected_media":
                    self.auto_workflow_pending = False
                    self.result_var.set("平台保护 · 当前版本不处理")
                    self._set_resolver_badge("受保护", "#a54b42", "#f8e9e7")
                    self.status_var.set(event[1])
                    self.parse_button.configure(state="normal")
                    messagebox.showwarning(APP_NAME, event[1])
                elif kind == "browser_progress":
                    self.result_var.set(f"{event[1]} · {event[3]}")
                    self.status_var.set(event[3])
                elif kind == "browser_capture_done":
                    _original, candidates = event[1], event[2]
                    self._finish_browser_capture()
                    if len(candidates) > 1:
                        self._show_media_selector(candidates, "浏览器发现多个音视频请求")
                    else:
                        self._start_smart_transcript(candidates[0])
                elif kind == "browser_capture_error":
                    self._finish_browser_capture()
                    self.result_var.set("浏览器辅助失败")
                    self.status_var.set(event[1])
                    messagebox.showerror(APP_NAME, event[1])
                elif kind == "subtitle_done":
                    info, output = event[1], Path(event[2])
                    self._record_txt_history(info.get("title") or output.stem, output)
                    self.result_var.set(f"平台字幕 · 已生成 {output.name}")
                    self._set_resolver_badge("字幕直出", GREEN, SUCCESS_SOFT)
                    self.status_var.set(f"逐字稿已完成：{output}")
                elif kind == "subtitle_error":
                    info, error_text = event[1], event[2]
                    self.status_var.set("平台字幕不可用，已自动回退 SenseVoice：" + error_text)
                    self._start_transcript_audio_download(info)
                elif kind == "updater_done":
                    self.status_var.set("解析组件已更新：yt-dlp " + event[1])
                    messagebox.showinfo(APP_NAME, "解析组件更新完成：yt-dlp " + event[1])
                elif kind == "updater_error":
                    self.status_var.set("解析组件更新失败，已保留原版本。")
                    messagebox.showerror(APP_NAME, "解析组件更新失败：\n\n" + event[1])
                elif kind == "xiaoe_login_required":
                    self.auto_workflow_pending = False
                    self.manual_cookie = ""
                    self.auth_verified_host = ""
                    self.result_var.set("需要重新登录")
                    self.status_var.set("当前课程店铺的登录状态无效或没有访问权限。")
                    self.auto_cookie_button.configure(state="normal")
                    self.parse_button.configure(state="normal")
                    self.cookie_source_var.set("专用 Edge 登录窗口")
                    self.store.data["cookie_source"] = "专用 Edge 登录窗口"
                    self.store.save()
                    self._refresh_cookie_controls()
                    if messagebox.askyesno(
                        APP_NAME,
                        event[1]
                        + "\n\n现在打开当前课程的专用 Edge 登录窗口吗？\n"
                        "登录并确认视频可播放后，关闭窗口即可自动重试。",
                    ):
                        self.login_retry_url = self.url_var.get().strip()
                        self.login_retry_pending = True
                        self.open_dedicated_login()
                elif kind == "parse_error":
                    self.auto_workflow_pending = False
                    self.result_var.set("解析失败")
                    self.status_var.set("解析失败：" + event[1])
                    self.parse_button.configure(state="normal")
                    messagebox.showerror(APP_NAME, "解析失败：\n\n" + event[1] + "\n\n该页面可能需要登录、使用 DRM，或暂不受支持。")
                elif kind == "progress":
                    task_id, percent = event[1], event[2]
                    if self.active_task and self.active_task["id"] == task_id:
                        self.progress["value"] = percent
                        self.active_detail.configure(
                            text=f"{self.active_task['format'].upper()} · 已完成 {percent:.1f}% · "
                            f"并行 {len(self.active_downloads)}/{self.max_parallel_downloads}"
                        )
                elif kind == "log":
                    self._append_log(event[1])
                elif kind == "done":
                    task, path = event[1], event[2]
                    self.progress["value"] = 100
                    self.active_detail.configure(text="下载与转换完成")
                    self.cancel_button.configure(state="disabled")
                    if task.get("transcript_only"):
                        self.status_var.set("临时音频已获取，正在离线生成逐字稿。")
                    else:
                        self.status_var.set("音频文件已保存。")
                        self.store.data["history"].insert(0, {
                            "title": task["title"], "format": task["format"],
                            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "path": path,
                        })
                        self.store.data["history"] = self.store.data["history"][:500]
                        self.store.save()
                        self.refresh_history()
                    if task.get("auto_transcribe"):
                        transcript_task = self._make_transcription_task(
                            source=Path(path),
                            output=(
                                Path(task["transcript_output_dir"])
                                if task.get("transcript_only")
                                else Path(path).parent / "逐字稿"
                            ),
                            model_label=task.get("transcript_model_label", "SenseVoice Q8（极速精准·离线）"),
                            language_label=task.get("transcript_language_label", "自动识别（中文优先）"),
                            word_timestamps=task.get("word_timestamps", False),
                            automatic=True,
                        )
                        if task.get("transcript_only"):
                            transcript_task.update({
                                "delete_source_on_success": True,
                                "cleanup_dir": task.get("cleanup_dir") or task.get("dir"),
                                "display_title": task.get("title") or Path(path).stem,
                            })
                        self._enqueue_transcription(transcript_task)
                        self.status_var.set(
                            "临时音频已获取，正在自动生成逐字稿。"
                            if task.get("transcript_only")
                            else "音频已保存，正在自动生成逐字稿。"
                        )
                    had_more = bool(self.pending_downloads)
                    self._start_next_download(task["id"])
                    if (
                        not self.active_downloads and not self.pending_downloads
                        and not task.get("auto_transcribe")
                    ):
                        messagebox.showinfo(APP_NAME, "全部下载任务已完成。")
                elif kind == "download_error":
                    task, error_text = event[1], event[2]
                    self.active_detail.configure(text=f"任务失败：{task['title']}")
                    self.status_var.set(error_text)
                    had_more = bool(self.pending_downloads)
                    self._start_next_download(task["id"])
                    messagebox.showerror(
                        APP_NAME,
                        f"{task['title']}\n\n{error_text}"
                        + ("\n\n已自动继续下一个等待任务。" if had_more else ""),
                    )
                elif kind == "transcribe_progress":
                    task, stage, percent, detail = event[1], event[2], event[3], event[4]
                    if self.current_transcription_task and self.current_transcription_task["id"] == task["id"]:
                        self.transcript_progress["value"] = percent
                        self.transcript_status_var.set(f"{stage} · {detail}")
                elif kind == "transcribe_done":
                    task, outputs, result = event[1], event[2], event[3]
                    if task.get("delete_source_on_success"):
                        try:
                            Path(task["source"]).unlink(missing_ok=True)
                        except OSError:
                            pass
                        cleanup_dir = task.get("cleanup_dir")
                        if cleanup_dir:
                            shutil.rmtree(cleanup_dir, ignore_errors=True)
                        if outputs:
                            self._record_txt_history(
                                task.get("display_title") or Path(outputs[0]).stem,
                                outputs[0],
                            )
                    self.transcript_progress["value"] = 100
                    names = "、".join(path.name for path in outputs)
                    self.transcript_backend_var.set(result.get("backend") or self.transcription_engine.backend_summary)
                    self.transcript_status_var.set(
                        f"逐字稿已完成：{names} · {result.get('backend', 'SenseVoice Q8')}"
                    )
                    self.status_var.set(f"逐字稿已生成：{Path(task['source']).name}")
                    had_more_transcriptions = bool(self.pending_transcriptions)
                    self._start_next_transcription()
                    if not task.get("automatic") or not had_more_transcriptions:
                        messagebox.showinfo(
                            APP_NAME,
                            "逐字稿生成完成：\n\n" + "\n".join(str(path) for path in outputs),
                        )
                elif kind == "transcribe_error":
                    task, error_text = event[1], event[2]
                    self.transcript_progress["value"] = 0
                    self.transcript_status_var.set("逐字稿生成失败")
                    self._start_next_transcription()
                    retained = (
                        "\n\n临时音频已保留 24 小时，可直接重试。"
                        if task.get("delete_source_on_success") else ""
                    )
                    messagebox.showerror(APP_NAME, f"{Path(task['source']).name}\n\n{error_text}{retained}")
                elif kind == "transcribe_cancelled":
                    task = event[1]
                    self.transcript_progress["value"] = 0
                    self.transcript_status_var.set("逐字稿任务已取消")
                    self._start_next_transcription()
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _set_log(self, text):
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        if text:
            self.log.insert("end", text)
        self.log.configure(state="disabled")

    def _append_log(self, text):
        self.log.configure(state="normal")
        self.log.insert("end", text + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def on_file_drop(self, event):
        try:
            files = list(self.tk.splitlist(event.data))
        except tk.TclError:
            files = [event.data.strip("{}")]
        if files:
            self.set_transcript_file(files[0])

    def choose_transcript_file(self):
        path = filedialog.askopenfilename(
            title="选择要转写的音频或视频",
            filetypes=(
                ("音频和视频", "*.mp3 *.wav *.m4a *.aac *.flac *.ogg *.opus *.mp4 *.mov *.mkv *.avi *.webm *.m4v"),
                ("所有文件", "*.*"),
            ),
        )
        if path:
            self.set_transcript_file(path)

    def set_transcript_file(self, value):
        path = Path(value)
        allowed = {
            ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus",
            ".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v",
        }
        if not path.is_file():
            messagebox.showwarning(APP_NAME, "拖入的文件不存在。")
            return
        if path.suffix.lower() not in allowed:
            messagebox.showwarning(APP_NAME, "暂不支持该文件格式。请拖入常见音频或视频文件。")
            return
        self.transcript_file_var.set(str(path))
        output = path.parent / "逐字稿"
        self.transcript_output_var.set(str(output))
        self.drop_zone_label.configure(text=f"已选择\n{path.name}\n\n再次拖入可替换")
        self.transcript_status_var.set("文件已就绪，点击“一键生成逐字稿”")

    def choose_transcript_output(self):
        path = filedialog.askdirectory(initialdir=self.transcript_output_var.get())
        if path:
            self.transcript_output_var.set(path)

    def open_transcript_glossary(self):
        path = Path(self.store.data.get("terminology_file") or (app_data_dir() / "sensevoice_terminology.txt"))
        if not path.is_file():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                "# 每行一条：错词 → 正词\n"
                "# 示例：生事维斯 → SenseVoice\n"
                "# 示例：小鹅通课成 → 小鹅通课程\n",
                encoding="utf-8-sig",
            )
        self.store.data["terminology_file"] = str(path)
        self.store.save()
        self.open_path(path)

    def start_transcription(self):
        source = Path(self.transcript_file_var.get())
        if not source.is_file():
            messagebox.showwarning(APP_NAME, "请先拖入或选择一个音频/视频文件。")
            return
        task = self._make_transcription_task(
            source=source,
            output=Path(self.transcript_output_var.get()),
            model_label=self.transcript_model_var.get(),
            language_label=self.transcript_language_var.get(),
            word_timestamps=self.word_timestamps_var.get(),
            automatic=False,
        )
        self._enqueue_transcription(task)

    @staticmethod
    def _make_transcription_task(source, output, model_label, language_label, word_timestamps, automatic):
        return {
            "id": str(time.time_ns()),
            "source": str(source),
            "output": str(output),
            "model": "sensevoice-q8",
            "language": "auto",
            "word_timestamps": bool(word_timestamps),
            "automatic": bool(automatic),
        }

    def _enqueue_transcription(self, task):
        if self.transcribe_process is not None or self.current_transcription_task is not None:
            self.pending_transcriptions.append(task)
            self.transcript_status_var.set(
                f"逐字稿队列：正在识别 1 个，等待 {len(self.pending_transcriptions)} 个"
            )
            if not task["automatic"]:
                messagebox.showinfo(APP_NAME, "当前正在识别其他文件，本文件已加入逐字稿队列。")
            return
        self._launch_transcription(task)

    def _launch_transcription(self, task):
        if not self.transcription_engine.available:
            self.events.put((
                "transcribe_error", task,
                "SenseVoice 离线引擎不完整，请重新运行转接口安装包进行修复。\n"
                + "\n".join(self.transcription_engine.missing_files()),
            ))
            return
        output = Path(task["output"])
        output.mkdir(parents=True, exist_ok=True)
        self.current_transcription_task = task
        self.transcription_cancel_event.clear()
        self.transcribe_button.configure(state="disabled")
        self.transcribe_cancel_button.configure(state="normal")
        self.transcript_progress["value"] = 2
        self.transcript_status_var.set(
            f"准备转写：{Path(task['source']).name} · SenseVoice Q8"
        )
        threading.Thread(
            target=self._transcribe_worker,
            args=(task,),
            daemon=True,
        ).start()

    def _transcribe_worker(self, task):
        output = Path(task["output"])
        source = Path(task["source"])
        job_dir = app_data_dir() / "transcribe_jobs" / ("job-" + re.sub(r"\D", "", task["id"]))
        try:
            result = self.transcription_engine.transcribe(
                source=source,
                output_dir=output,
                work_dir=job_dir,
                glossary_path=self.store.data.get("terminology_file"),
                progress_callback=lambda stage, percent, detail: self.events.put(
                    ("transcribe_progress", task, stage, percent, detail)
                ),
                process_callback=lambda process: setattr(self, "transcribe_process", process),
                cancel_event=self.transcription_cancel_event,
            )
            self.events.put(("transcribe_done", task, [Path(result["output"])], result))
        except TranscriptionCancelled:
            self.events.put(("transcribe_cancelled", task))
        except Exception as exc:
            self.events.put(("transcribe_error", task, str(exc)))
        finally:
            self.transcribe_process = None
            try:
                shutil.rmtree(job_dir)
            except OSError:
                pass

    def _start_next_transcription(self):
        self.current_transcription_task = None
        self.transcription_cancel_event.clear()
        if self.pending_transcriptions:
            self._launch_transcription(self.pending_transcriptions.pop(0))
        else:
            self.transcribe_button.configure(state="normal")
            self.transcribe_cancel_button.configure(state="disabled")

    def _apply_parsed_info(self, info, verified_source=""):
        """Apply one already-resolved result without exporting cookies twice."""
        self.parsed = info
        current_url = self.url_var.get().strip()
        self.auth_verified_host = (
            (urlparse(current_url).hostname or "") if is_xiaoe_url(current_url) else ""
        )
        self._refresh_cookie_controls()
        extra = f" · {info['count']} 项" if info.get("count", 1) > 1 else ""
        self.result_var.set(
            f"{info['extractor']} · 时长 {seconds_text(info['duration'])}{extra}"
        )
        self.name_var.set(clean_filename(info["title"]))
        badge = info.get("resolver_badge") or ("已验证登录" if verified_source else "直接支持")
        self._set_resolver_badge(badge, GREEN, SUCCESS_SOFT)
        if verified_source:
            self.status_var.set(
                f"已通过{verified_source}验证当前课程权限，并取得可下载音频。"
            )
        else:
            self.status_var.set("解析完成，发现可下载的媒体。")
        self.parse_button.configure(state="normal")
        if self.auto_workflow_pending:
            self.auto_workflow_pending = False
            if info.get("items") and len(info["items"]) > 1:
                self.after(100, lambda: self._show_media_selector(info["items"], info.get("title") or "多个媒体"))
            else:
                self.status_var.set("解析完成，正在按“字幕优先、音频回退”生成逐字稿……")
                self.after(100, lambda: self._start_smart_transcript(info))

    def _set_resolver_badge(self, text, foreground, background):
        if hasattr(self, "resolver_badge_var"):
            self.resolver_badge_var.set(text)
            self.resolver_badge.configure(fg=foreground, bg=background)

    def _show_media_selector(self, items, title="检测到多个媒体"):
        window = tk.Toplevel(self)
        window.title("选择要生成逐字稿的媒体")
        window.geometry("900x590")
        window.minsize(700, 430)
        window.transient(self)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame, text=f"{title} · 已检测到 {len(items)} 项，默认全部选中",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 12))
        tree = ttk.Treeview(frame, columns=("title", "source", "path"), show="headings", selectmode="extended")
        tree.heading("title", text="标题")
        tree.heading("source", text="解析方式")
        tree.heading("path", text="处理路径")
        tree.column("title", width=470)
        tree.column("source", width=170, anchor="center")
        tree.column("path", width=170, anchor="center")
        tree.pack(fill="both", expand=True)
        for index, item in enumerate(items):
            path_label = "平台字幕" if item.get("subtitle") else (
                "浏览器播放捕获" if item.get("resolver_state") == "需要播放捕获" else "音频 + SenseVoice"
            )
            tree.insert("", "end", iid=str(index), values=(
                item.get("title") or f"媒体 {index + 1}", item.get("extractor") or "公开网页", path_label,
            ))
        tree.selection_set(tree.get_children())
        controls = ttk.Frame(frame)
        controls.pack(fill="x", pady=(12, 0))
        ttk.Button(controls, text="全选", command=lambda: tree.selection_set(tree.get_children())).pack(side="left")
        ttk.Button(controls, text="清除选择", command=lambda: tree.selection_remove(tree.selection())).pack(side="left", padx=8)

        def submit():
            selected = [items[int(iid)] for iid in tree.selection()]
            if not selected:
                messagebox.showinfo(APP_NAME, "请至少选择一个媒体。", parent=window)
                return
            window.destroy()
            for item in selected:
                self._start_smart_transcript(item)
            self.show_page("active")

        ttk.Button(controls, text="生成所选逐字稿", style="Primary.TButton", command=submit).pack(side="right")

    def _start_smart_transcript(self, info):
        if info.get("resolver_state") == "需要播放捕获":
            self._enqueue_browser_capture(info)
            return
        if info.get("subtitle"):
            self.status_var.set(f"正在下载并整理平台字幕：{info.get('title') or '逐字稿'}")
            threading.Thread(target=self._subtitle_worker, args=(dict(info),), daemon=True).start()
            return
        self._start_transcript_audio_download(info)

    @staticmethod
    def _unique_txt_path(folder, stem):
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        destination = folder / (clean_filename(stem) + ".txt")
        suffix = 2
        while destination.exists():
            destination = folder / f"{clean_filename(stem)}（{suffix}）.txt"
            suffix += 1
        return destination

    def _subtitle_worker(self, info):
        try:
            output = self._unique_txt_path(Path(self.dir_var.get()) / "逐字稿", info.get("title") or "逐字稿")
            self.media_resolver.subtitle_to_text(info["subtitle"], output)
            self.events.put(("subtitle_done", info, str(output)))
        except Exception as exc:
            self.events.put(("subtitle_error", info, str(exc)))

    def _start_transcript_audio_download(self, info):
        task_id = str(time.time_ns())
        title = clean_filename(info.get("title") or "逐字稿")
        job_dir = app_data_dir() / "media_jobs" / ("job-" + task_id)
        job_dir.mkdir(parents=True, exist_ok=True)
        task = {
            "id": task_id, "title": title, "format": "mp3",
            "url": info.get("media_url") or info.get("download_url") or info.get("source_url"),
            "source_url": info.get("source_url") or info.get("download_url"),
            "dir": str(job_dir), "quality": "128K", "playlist": False,
            "cookie_args": list(info.get("cookie_args") or []),
            "http_headers": dict(info.get("http_headers") or {}),
            "xiaoe_refresh": dict(info.get("xiaoe_refresh") or {}),
            "auto_transcribe": True, "transcript_only": True,
            "transcript_output_dir": str(Path(self.dir_var.get()) / "逐字稿"),
            "delete_source_on_success": True, "cleanup_dir": str(job_dir),
            "transcript_model_label": "SenseVoice Q8（极速精准·离线）",
            "transcript_language_label": "自动识别（中文优先）", "word_timestamps": False,
        }
        if len(self.active_downloads) >= self.max_parallel_downloads:
            self.pending_downloads.append(task)
            self._refresh_download_queue()
            self.status_var.set(f"已加入逐字稿队列：{title}")
        else:
            self._launch_download_task(task)
            self.status_var.set(f"正在获取临时音频：{title}")

    def _record_txt_history(self, title, path):
        self.store.data["history"].insert(0, {
            "title": title, "format": "txt",
            "date": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "path": str(path),
        })
        self.store.data["history"] = self.store.data["history"][:500]
        self.store.save()
        self.refresh_history()

    def _enqueue_browser_capture(self, info):
        if self.browser_capture_active:
            self.pending_browser_captures.append(dict(info))
            self.status_var.set(f"浏览器捕获任务已排队：{info.get('title') or '公开网页'}")
            return
        self.browser_capture_active = True
        self.browser_capture_cancel.clear()
        self.parse_button.configure(text="取消浏览器监听", command=self._cancel_browser_capture, state="normal")
        self._set_resolver_badge("浏览器辅助", "#9a631c", "#fbf1df")
        threading.Thread(target=self._browser_capture_worker, args=(dict(info),), daemon=True).start()

    def _browser_capture_worker(self, info):
        try:
            candidates = self.browser_capture.capture(
                info.get("source_url") or info.get("download_url"), self.browser_capture_cancel,
                progress_callback=lambda stage, percent, detail: self.events.put(
                    ("browser_progress", stage, percent, detail)
                ), timeout=90,
            )
            self.events.put(("browser_capture_done", info, candidates))
        except (BrowserCaptureProtected, ProtectedMediaError) as exc:
            self.events.put(("browser_capture_error", "平台保护：" + str(exc)))
        except Exception as exc:
            self.events.put(("browser_capture_error", str(exc)))
        finally:
            self.browser_capture.close()

    def _cancel_browser_capture(self):
        self.browser_capture_cancel.set()
        self.status_var.set("正在取消浏览器监听……")

    def _finish_browser_capture(self):
        self.browser_capture_active = False
        self.browser_capture_cancel.clear()
        self.parse_button.configure(
            text="粘贴链接生成逐字稿", command=self.one_click_full_workflow, state="normal"
        )
        if self.pending_browser_captures:
            next_info = self.pending_browser_captures.pop(0)
            self.after(150, lambda: self._enqueue_browser_capture(next_info))

    @staticmethod
    def _cleanup_stale_media_jobs():
        root = app_data_dir() / "media_jobs"
        if not root.is_dir():
            return
        cutoff = time.time() - 24 * 3600
        for child in root.iterdir():
            try:
                if child.is_dir() and child.stat().st_mtime < cutoff:
                    shutil.rmtree(child)
            except OSError:
                pass

    def update_ytdlp(self):
        if not messagebox.askyesno(APP_NAME, "从 yt-dlp 官方 GitHub 检查并更新解析组件吗？\n更新失败会自动保留当前版本。"):
            return
        self.status_var.set("正在从官方发布页检查解析组件更新……")
        threading.Thread(target=self._update_ytdlp_worker, daemon=True).start()

    def _update_ytdlp_worker(self):
        import hashlib
        target = resource_path("yt-dlp.exe")
        temp_dir = Path(tempfile.mkdtemp(prefix="resolver-update-", dir=str(app_data_dir())))
        candidate = temp_dir / "yt-dlp.exe"
        backup = target.with_suffix(".exe.bak")
        try:
            base = "https://github.com/yt-dlp/yt-dlp/releases/latest/download/"
            with urlopen(Request(base + "SHA2-256SUMS", headers={"User-Agent": "ZhuanJieKou/1.11"}), timeout=30) as response:
                checksums = response.read().decode("utf-8", errors="replace")
            match = re.search(r"^([0-9a-fA-F]{64})\s+\*?yt-dlp\.exe$", checksums, re.M)
            if not match:
                raise RuntimeError("官方校验清单中没有 yt-dlp.exe。")
            with urlopen(Request(base + "yt-dlp.exe", headers={"User-Agent": "ZhuanJieKou/1.11"}), timeout=120) as response:
                candidate.write_bytes(response.read())
            actual = hashlib.sha256(candidate.read_bytes()).hexdigest()
            if actual.lower() != match.group(1).lower():
                raise RuntimeError("下载文件 SHA-256 与官方清单不一致。")
            result = subprocess.run(
                [str(candidate), "--version"], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if result.returncode:
                raise RuntimeError("新解析组件无法启动。")
            shutil.copy2(target, backup)
            try:
                shutil.copy2(candidate, target)
            except Exception:
                shutil.copy2(backup, target)
                raise
            self.events.put(("updater_done", result.stdout.strip()))
        except Exception as exc:
            self.events.put(("updater_error", str(exc)))
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def cancel_transcription(self):
        process = self.transcribe_process
        if self.current_transcription_task and messagebox.askyesno(APP_NAME, "确定取消当前逐字稿任务吗？"):
            self.pending_transcriptions.clear()
            self.transcription_cancel_event.set()
            try:
                if process:
                    process.terminate()
            except OSError:
                pass
            self.transcript_status_var.set("正在取消任务……")

    def open_transcript_output(self):
        path = Path(self.transcript_output_var.get())
        path.mkdir(parents=True, exist_ok=True)
        self.open_path(path)

    def choose_dir(self):
        path = filedialog.askdirectory(initialdir=self.dir_var.get() if hasattr(self, "dir_var") else self.store.data["output_dir"])
        if path:
            self.dir_var.set(path)
            self.store.data["output_dir"] = path
            self.store.save()

    def _save_auto_transcribe(self):
        self.store.data["auto_transcribe"] = bool(self.auto_transcribe_var.get())
        self.store.save()
        self.download_button.configure(
            text="下载音频并生成逐字稿" if self.auto_transcribe_var.get()
            else "仅下载音频"
        )

    def cookie_source_changed(self, _event=None):
        self.store.data["cookie_source"] = self.cookie_source_var.get()
        self.store.save()
        self._refresh_cookie_controls()

    def choose_cookie_file(self):
        path = filedialog.askopenfilename(
            title="选择 Netscape 格式的 cookies.txt",
            filetypes=(("Cookie 文件", "*.txt"), ("所有文件", "*.*")),
        )
        if path:
            try:
                first = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()[0]
            except Exception:
                first = ""
            if first not in ("# Netscape HTTP Cookie File", "# HTTP Cookie File"):
                messagebox.showwarning(APP_NAME, "这不是标准 Netscape cookies.txt 文件。")
                return
            self.cookie_file_var.set(path)
            self.cookie_source_var.set("cookies.txt 文件")
            self.store.data["cookie_source"] = "cookies.txt 文件"
            self.store.data["cookie_file"] = path
            self.store.save()
            self._refresh_cookie_controls()

    def read_manual_cookie(self):
        try:
            value = self.clipboard_get().strip()
        except tk.TclError:
            value = ""
        value = re.sub(r"^\s*cookie\s*:\s*", "", value, flags=re.I).strip()
        if len(value) < 20 or "=" not in value:
            messagebox.showinfo(
                APP_NAME,
                "剪贴板中没有可用的 Cookie。\n\n"
                "请在你已经登录且能播放课程的浏览器页面中：\n"
                "1. 按 F12，打开 Network（网络）。\n"
                "2. 刷新课程页面，点击任意同站点请求。\n"
                "3. 打开 Headers → Request Headers。\n"
                "4. 复制 Cookie 后面的完整值。\n"
                "5. 回到这里再次点击“读取剪贴板 Cookie”。",
            )
            return
        self.manual_cookie = value
        self.cookie_source_var.set("手动 Cookie（本次）")
        self.store.data["cookie_source"] = "手动 Cookie（本次）"
        self.store.save()
        self._refresh_cookie_controls()
        self.status_var.set("已读取手动 Cookie，仅在本次程序运行期间保存在内存中。现在可以解析。")
        messagebox.showinfo(APP_NAME, "Cookie 已读取。现在可以点击“解析音频链接”。")

    def one_click_cookie_parse(self):
        """Read the current site's authorized browser cookie and parse at once."""
        try:
            clipboard = self.clipboard_get().strip()
        except tk.TclError:
            clipboard = ""

        clipboard_url = extract_share_url(clipboard)
        if clipboard_url.startswith(("http://", "https://")):
            self.url_var.set(unwrap_redirect_url(clipboard_url))

        url = unwrap_redirect_url(self.url_var.get().strip())
        self.url_var.set(url)
        if not re.match(r"^https?://", url, re.I):
            self.auto_workflow_pending = False
            messagebox.showwarning(APP_NAME, "请先粘贴要解析的完整网页链接。")
            return

        # Also accept a raw `Cookie: name=value; ...` copied from DevTools.
        pasted_cookie = re.sub(r"^\s*cookie\s*:\s*", "", clipboard, flags=re.I).strip()
        has_pasted_cookie = (
            not clipboard_url.startswith(("http://", "https://"))
            and len(pasted_cookie) >= 20
            and "=" in pasted_cookie
        )

        source = "手动 Cookie（本次）" if has_pasted_cookie else self.cookie_source_var.get()
        manual_cookie = pasted_cookie if has_pasted_cookie else self.manual_cookie
        if source == "不使用" or (
            source in ("自动读取（本次）", "手动 Cookie（本次）") and not manual_cookie
        ):
            profile = app_data_dir() / "edge-login-profile"
            if profile.is_dir():
                self.cookie_source_var.set("专用 Edge 登录窗口")
                source = "专用 Edge 登录窗口"
            else:
                self.auto_workflow_pending = False
                messagebox.showinfo(
                    APP_NAME,
                    "还没有可读取的登录状态。\n\n"
                    "请先点击“打开登录窗口”，在专用 Edge 中登录并确认课程可以播放；"
                    "关闭整个专用窗口后，再点击“一键完成：音频＋逐字稿”。",
                )
                self.open_dedicated_login()
                return

        self.auto_cookie_button.configure(state="disabled")
        self.parse_button.configure(state="disabled")
        self.status_var.set("正在逐一读取并验证本机浏览器中的当前课程登录状态……")
        cookie_file = self.cookie_file_var.get().strip()
        threading.Thread(
            target=self._one_click_cookie_worker,
            args=(url, source, cookie_file, manual_cookie),
            daemon=True,
        ).start()

    def one_click_full_workflow(self):
        """Paste/parse, download the audio, then enqueue offline transcription."""
        url = unwrap_redirect_url(extract_share_url(self.url_var.get().strip()))
        if not re.match(r"^https?://", url, re.I):
            try:
                clipboard_url = extract_share_url(self.clipboard_get())
            except tk.TclError:
                clipboard_url = ""
            if clipboard_url.startswith(("http://", "https://")):
                url = unwrap_redirect_url(clipboard_url)
                self.url_var.set(url)
        if not re.match(r"^https?://", url, re.I):
            messagebox.showwarning(APP_NAME, "请先粘贴要解析的完整视频网址。")
            return

        self.auto_workflow_pending = True
        self._set_resolver_badge("识别平台", "#9a631c", "#fbf1df")
        self.transcript_model_var.set("SenseVoice Q8（极速精准·离线）")
        self.word_timestamps_var.set(False)
        if not self.auto_transcribe_var.get():
            self.auto_transcribe_var.set(True)
            self._save_auto_transcribe()
        self.status_var.set("一键任务已开始：正在读取登录状态并解析……")
        if is_xiaoe_url(url):
            self.one_click_cookie_parse()
        else:
            self.parse_url()

    def load_course_batch(self):
        url = unwrap_redirect_url(self.url_var.get().strip())
        if not re.match(r"^https?://", url, re.I):
            try:
                clipboard_url = extract_http_url(self.clipboard_get())
            except tk.TclError:
                clipboard_url = ""
            if clipboard_url.startswith(("http://", "https://")):
                url = unwrap_redirect_url(clipboard_url)
                self.url_var.set(url)
        parsed = urlparse(url)
        query = parse_qs(parsed.query, keep_blank_values=True)
        course_id = (query.get("course_id") or query.get("product_id") or [""])[0]
        if not is_xiaoe_url(url) or not course_id.startswith(("course_", "c_")):
            messagebox.showinfo(
                APP_NAME,
                "请先粘贴该课程中任意一节视频的完整网址，网址中需要包含 course_id。",
            )
            return

        source = self.cookie_source_var.get()
        if source == "不使用" or (
            source in ("自动读取（本次）", "手动 Cookie（本次）") and not self.manual_cookie
        ):
            profile = app_data_dir() / "edge-login-profile"
            if not profile.is_dir():
                messagebox.showinfo(
                    APP_NAME,
                    "首次批量读取前，请先在专用 Edge 中登录并确认课程可以播放。"
                    "关闭整个专用窗口后，再点击“批量选择本课程视频”。",
                )
                self.open_dedicated_login()
                return
            source = "专用 Edge 登录窗口"
            self.cookie_source_var.set(source)

        self.batch_button.configure(state="disabled")
        self.status_var.set("正在读取同一课程的视频列表……")
        threading.Thread(
            target=self._batch_catalog_worker,
            args=(
                url, source, self.cookie_file_var.get().strip(),
                self.manual_cookie, course_id,
                (query.get("sub_course_id") or [""])[0],
            ),
            daemon=True,
        ).start()

    def _batch_catalog_worker(self, url, source, cookie_file, manual_cookie, course_id, sub_course_id):
        try:
            cookie = self._xiaoe_cookie_header(
                url, source=source, cookie_file=cookie_file, manual_cookie=manual_cookie,
            )
            parsed = urlparse(url)
            origin = f"{parsed.scheme}://{parsed.netloc}"
            endpoint = (
                f"{origin}/xe.course.business_go.avoidlogin.e_course."
                "resource_catalog_list.get/3.0.0"
            )
            items = []
            seen = set()
            payload = self._json_post(
                endpoint,
                {"course_id": course_id, "sub_course_id": sub_course_id},
                cookie,
                url,
            )
            self._validate_xiaoe_response(payload)
            data = payload.get("data") or {}

            def add_rows(rows, group=""):
                for row in rows or []:
                    if not isinstance(row, dict):
                        continue
                    chapter_id = str(row.get("chapter_id") or row.get("resource_id") or "")
                    title = str(row.get("chapter_title") or row.get("resource_title") or chapter_id)
                    children = row.get("children") or []
                    next_group = title if children and not chapter_id.startswith("v_") else group
                    if (
                        chapter_id.startswith("v_")
                        and chapter_id not in seen
                        and row.get("unlock_state") not in (0, False)
                    ):
                        seen.add(chapter_id)
                        chapter_query = urlencode({
                            "product_id": course_id,
                            "course_id": course_id,
                            "sub_course_id": sub_course_id,
                        })
                        items.append({
                            "group": group or "未分组",
                            "sort": str(row.get("sort_c") or row.get("sort_value") or len(items) + 1),
                            "title": title,
                            "url": f"{origin}/p/course/video/{chapter_id}?{chapter_query}",
                        })
                    if children:
                        add_rows(children, next_group)

            add_rows(data.get("list") or [])
            if not items:
                raise RuntimeError("该课程没有返回已解锁的视频章节。")
            self.events.put(("batch_catalog_ready", cookie, items))
        except Exception as exc:
            self.events.put(("batch_catalog_error", str(exc)))

    def _show_batch_selector(self, items, cookie):
        window = tk.Toplevel(self)
        window.title("批量选择本课程视频")
        window.geometry("900x620")
        window.minsize(720, 460)
        window.transient(self)
        frame = ttk.Frame(window, padding=18)
        frame.pack(fill="both", expand=True)
        ttk.Label(
            frame,
            text=f"已读取 {len(items)} 个已解锁视频。按住 Ctrl/Shift 可多选。",
            font=("Microsoft YaHei UI", 11, "bold"),
        ).pack(anchor="w", pady=(0, 12))
        tree = ttk.Treeview(
            frame, columns=("group", "sort", "title"), show="headings", selectmode="extended",
        )
        tree.heading("group", text="所属栏目")
        tree.heading("sort", text="序号")
        tree.heading("title", text="视频标题")
        tree.column("group", width=260)
        tree.column("sort", width=80, anchor="center", stretch=False)
        tree.column("title", width=500)
        scrollbar = ttk.Scrollbar(frame, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=scrollbar.set)
        tree.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="left", fill="y")
        for index, item in enumerate(items):
            tree.insert(
                "", "end", iid=str(index),
                values=(item["group"], item["sort"], item["title"]),
            )
        controls = ttk.Frame(window, padding=(18, 0, 18, 18))
        controls.pack(fill="x")
        ttk.Button(
            controls, text="全选",
            command=lambda: tree.selection_set(tree.get_children()),
        ).pack(side="left")
        ttk.Button(
            controls, text="清除选择",
            command=lambda: tree.selection_remove(tree.selection()),
        ).pack(side="left", padx=8)

        def submit():
            selected = [items[int(iid)] for iid in tree.selection()]
            if not selected:
                messagebox.showinfo(APP_NAME, "请至少选择一个视频。", parent=window)
                return
            window.destroy()
            self.batch_button.configure(state="disabled")
            self.status_var.set(f"正在解析选中的 {len(selected)} 个视频……")
            threading.Thread(
                target=self._batch_resolve_worker,
                args=(selected, cookie),
                daemon=True,
            ).start()

        ttk.Button(
            controls, text="下载所选并生成逐字稿",
            style="Primary.TButton", command=submit,
        ).pack(side="right")

    def _batch_resolve_worker(self, items, cookie):
        try:
            infos = []
            failed = []
            for item in items:
                try:
                    infos.append(self._resolve_xiaoe_video(item["url"], cookie))
                except Exception as exc:
                    failed.append(f"{item['title']}：{exc}")
            if not infos:
                raise RuntimeError("所选章节均未能解析。")
            self.events.put(("batch_resolved", infos, failed))
        except Exception as exc:
            self.events.put(("batch_resolve_error", str(exc)))

    def _one_click_cookie_worker(self, url, source, cookie_file, manual_cookie):
        shop = self._xiaoe_shop_prefix(urlparse(url).hostname or "")
        source_map = self.store.data.get("xiaoe_source_by_shop") or {}
        preferred = source_map.get(shop, "") if isinstance(source_map, dict) else ""
        candidates = [preferred, source]
        if manual_cookie:
            candidates.append("手动 Cookie（本次）")
        candidates.extend([
            "专用 Edge 登录窗口", "Edge 登录状态",
            "Chrome 登录状态", "Firefox 登录状态",
        ])
        if cookie_file and Path(cookie_file).is_file():
            candidates.append("cookies.txt 文件")

        ordered = []
        for candidate in candidates:
            if candidate and candidate not in ordered and candidate != "不使用":
                ordered.append(candidate)

        errors = []
        for candidate in ordered:
            try:
                cookie = self._xiaoe_cookie_header(
                    url,
                    source=candidate,
                    cookie_file=cookie_file,
                    manual_cookie=manual_cookie,
                )
                if len(cookie) < 20 or "=" not in cookie:
                    raise RuntimeError("没有读取到有效 Cookie。")
                if not self._has_xiaoe_login_cookie(cookie):
                    raise XiaoeLoginRequired("只有匿名 Cookie，没有当前店铺登录令牌。")
                # A token-looking cookie is not proof of course access. Verify
                # it against this exact chapter and keep trying later browser
                # profiles when it is stale or belongs to another account.
                info = self._resolve_xiaoe_with_cookie(url, cookie)
                self.events.put(("xiaoe_authorized", cookie, url, candidate, info))
                return
            except Exception as exc:
                errors.append(f"{candidate}：{str(exc).splitlines()[0]}")

        detail = "\n".join(errors[:5])
        self.events.put((
            "xiaoe_cookie_login_required",
            "已逐一检查现有浏览器，但没有任何登录状态通过当前课程的访问验证。"
            + (f"\n\n自动检查结果：\n{detail}" if detail else ""),
            url,
        ))

    def open_dedicated_login(self):
        candidates = (
            Path(os.environ.get("PROGRAMFILES(X86)", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("PROGRAMFILES", "")) / "Microsoft/Edge/Application/msedge.exe",
            Path(os.environ.get("LOCALAPPDATA", "")) / "Microsoft/Edge/Application/msedge.exe",
        )
        edge = next((path for path in candidates if path.is_file()), None)
        if edge is None:
            messagebox.showerror(APP_NAME, "没有找到 Microsoft Edge。请改用 Chrome 登录状态或 cookies.txt。")
            return
        url = unwrap_redirect_url(self.url_var.get().strip())
        if not url.startswith(("http://", "https://")):
            url = "https://www.xiaoe-tech.com/"
        elif is_xiaoe_url(url):
            self.login_retry_url = url
            self.login_retry_pending = True
        profile = app_data_dir() / "edge-login-profile"
        profile.mkdir(parents=True, exist_ok=True)
        self.cookie_source_var.set("专用 Edge 登录窗口")
        self.store.data["cookie_source"] = "专用 Edge 登录窗口"
        self.store.save()
        self._refresh_cookie_controls()
        command = [
            str(edge),
            f"--user-data-dir={profile}",
            "--no-first-run",
            "--disable-background-mode",
            "--new-window",
            url,
        ]
        try:
            self.login_browser_process = subprocess.Popen(command)
            self.login_opened_at = time.time()
            self.status_var.set(
                "请在专用 Edge 中登录当前课程店铺并确认视频可播放。"
                "完成后关闭整个专用窗口，软件会自动校验并继续。"
            )
            self.after(1000, self._watch_dedicated_login)
        except OSError as exc:
            self.login_retry_pending = False
            messagebox.showerror(APP_NAME, "无法打开专用 Edge：" + str(exc))

    def _watch_dedicated_login(self):
        process = self.login_browser_process
        if process is not None and process.poll() is None:
            self.after(1000, self._watch_dedicated_login)
            return
        # Edge can hand off from the launcher process to child processes.
        # Do not read a stale/locked Cookie database until every process using
        # this dedicated profile has really closed.
        if self._dedicated_edge_running():
            self.after(1200, self._watch_dedicated_login)
            return
        self.login_browser_process = None
        if not self.login_retry_pending:
            return
        # Edge may hand off to another process during startup. Waiting briefly
        # prevents an immediate retry before its profile database is released.
        elapsed = time.time() - self.login_opened_at
        if elapsed < 3:
            self.after(int((3 - elapsed) * 1000), self._watch_dedicated_login)
            return
        retry_url = self.login_retry_url
        self.login_retry_pending = False
        self.login_retry_url = ""
        if retry_url:
            self.url_var.set(retry_url)
        self.status_var.set("登录窗口已关闭，正在自动校验当前课程登录状态……")
        self.after(1200, self.one_click_cookie_parse)

    def _refresh_cookie_controls(self):
        if not hasattr(self, "cookie_file_button"):
            return
        using_file = self.cookie_source_var.get() == "cookies.txt 文件"
        using_login_window = self.cookie_source_var.get() == "专用 Edge 登录窗口"
        using_manual = self.cookie_source_var.get() in ("自动读取（本次）", "手动 Cookie（本次）")
        self.cookie_file_button.configure(state="normal" if using_file else "disabled")
        self.login_browser_button.configure(state="normal")
        self.manual_cookie_button.configure(state="normal")
        self.auto_cookie_button.configure(state="normal")
        self.login_browser_button.configure(
            text="重新打开登录窗口" if using_login_window else "打开登录窗口"
        )
        path = self.cookie_file_var.get().strip()
        if using_file and path:
            auth_status = Path(path).name
        elif using_manual:
            current_host = urlparse(self.url_var.get().strip()).hostname or ""
            if self.manual_cookie and self.auth_verified_host == current_host:
                auth_status = "已验证当前课程"
            else:
                auth_status = "待验证" if self.manual_cookie else "等待读取"
        else:
            auth_status = ""
        self.cookie_file_label.configure(text=auth_status)

    def clear_form(self):
        self.url_var.set("")
        self.name_var.set("")
        self.result_var.set("尚未解析")
        self._set_resolver_badge("等待链接", MUTED, PANEL)
        self.status_var.set("准备就绪")
        self.parsed = None
        self.url_entry.focus_set()

    def refresh_history(self):
        for item in self.history.get_children():
            self.history.delete(item)
        for index, item in enumerate(self.store.data.get("history", [])):
            self.history.insert("", "end", iid=str(index), values=(item.get("title"), item.get("format", "").upper(),
                                                                   item.get("date"), item.get("path")))

    def selected_history(self):
        selected = self.history.selection()
        if not selected:
            messagebox.showinfo(APP_NAME, "请先选择一条完成记录。")
            return None
        index = int(selected[0])
        history = self.store.data.get("history", [])
        return history[index] if index < len(history) else None

    def open_selected(self):
        item = self.selected_history()
        if item:
            self.open_path(Path(item["path"]))

    def open_selected_folder(self):
        item = self.selected_history()
        if item:
            self.open_path(Path(item["path"]).parent)

    def clear_history(self):
        if messagebox.askyesno(APP_NAME, "只清除完成记录，不会删除已下载文件。继续吗？"):
            self.store.data["history"] = []
            self.store.save()
            self.refresh_history()

    @staticmethod
    def open_path(path):
        if not path.exists():
            messagebox.showwarning(APP_NAME, "文件或目录已不存在。")
            return
        if os.name == "nt":
            os.startfile(str(path))
        else:
            subprocess.Popen(["open" if sys.platform == "darwin" else "xdg-open", str(path)])

    def show_help(self):
        messagebox.showinfo(APP_NAME,
            "使用方法\n\n"
            "1. 粘贴公开音视频网页、短链、分享文案或 m3u8/mp4 直链。\n"
            "2. 点击“粘贴链接生成逐字稿”。\n"
            "3. 软件优先读取人工字幕，其次自动字幕；没有字幕才提取音频并调用 SenseVoice。\n"
            "4. 直接解析失败时会打开独立的 Edge 捕获窗口，请播放几秒，捕获成功后会自动继续。\n"
            "5. 成功后只保留 UTF-8 TXT；临时音频自动删除。失败音频保留 24 小时供重试。\n"
            "6. 不需要另外安装 Buzz、Python、FFmpeg 或模型。\n\n"
            "批量下载：粘贴课程中任意一节的完整网址，点击“批量选择本课程视频”。"
            "在窗口中按住 Ctrl/Shift 多选同一栏目视频，再点击“下载所选并生成逐字稿”。\n"
            "批量任务最多 3 个并行下载，逐字稿会使用 SenseVoice Q8 依次生成。\n"
            "识别优先使用 RTX CUDA；若驱动或显存不合适，会自动改用 CPU AVX2。\n\n"
            "文件转换：进入“文件转换”页，可一次拖入多个 PDF、Word、Excel、CSV、EPUB。"
            "选择一个或多个任务后设置目标格式，再点击“开始全部”。"
            "转换完全离线，源文件不会被覆盖；扫描版 PDF 本版会提示需要 OCR。\n\n"
            "小鹅通等登录页面：首次使用时，软件会打开专用 Edge。"
            "请按网页提供的验证码方式登录并确认课程能播放，关闭整个专用窗口后再点一次一键完成。\n"
            "不同课程店铺的登录状态彼此独立；软件会记住每个店铺上次验证成功的浏览器，"
            "关闭登录窗口后自动继续解析，不需要打开 F12 或手工寻找 Cookie。\n\n"
            "说明：尽可能支持所有公开网页，但不绕过登录权限、付费墙、私有内容、平台加密或 DRM。"
            "微信视频号若只暴露 Finder 加密协议，会明确提示不可处理。请仅处理你有权保存的内容。\n"
            "站点改版时可点击顶部“更新解析组件”，软件只从 yt-dlp 官方发布页下载并校验 SHA-256。")

    def on_close(self):
        busy = (
            self.active_downloads or self.pending_downloads
            or self.transcribe_process or self.pending_transcriptions
            or self.current_conversion_task is not None
            or self.current_ocr_task is not None
            or self.browser_capture_active or self.pending_browser_captures
        )
        if busy and not messagebox.askyesno(APP_NAME, "任务仍在进行，确定退出并终止任务吗？"):
            return
        try:
            for process in list(self.download_processes.values()):
                try:
                    process.terminate()
                except Exception:
                    pass
            if self.current_transcription_task:
                self.transcription_cancel_event.set()
            if self.transcribe_process:
                self.transcribe_process.terminate()
            self.browser_capture_cancel.set()
            self.pending_browser_captures.clear()
            self.browser_capture.close()
            if self.conversion_process:
                self.current_conversion_task["cancel_requested"] = True
                self._terminate_conversion_process(self.conversion_process)
            self.ocr_engine.close()
        finally:
            self.destroy()


if __name__ == "__main__":
    app = AudioXhl()
    app.mainloop()
