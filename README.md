# 转接口

面向课程、访谈与录屏的 Windows 桌面工具。将音视频转成文字，也支持按本机能力转换常见文档格式。

## 快速开始

Windows 10/11 x64，建议使用含 Tk 的 Python 3.11。用 `python app.py` 打开界面。转写分支需自行配置 FFmpeg、FunASR SenseVoice 与相应模型。其他选配组件、文件位置和许可见 [第三方声明](THIRD_PARTY_NOTICES.txt) 和构建脚本。构建入口为 `build-release.ps1`。将 `yt-dlp.exe` 与 `ffmpeg.exe` 放进 `tools/`，并在项目 `.venv` 中安装 PyInstaller，或设置 `ZHUANJIEKOU_PYTHON` 指向带 PyInstaller 的 Python。构建还需要 NSIS。脚本会校验和下载固定的转写运行时与模型。

## 使用示例

用一段属于自己的短录音开始。载入本地文件，选择转写和输出位置。可识别的外部来源优先处理字幕，音频文件通过本机模型识别，产物为可编辑的逐字稿。文档格式转换会根据格式使用本机已有的 Office、LibreOffice 或 Calibre 等独立程序。

## 代码结构

- `app.py` 是界面和任务入口。
- `transcription_engine.py`、`media_resolver.py` 负责转录与媒体解析。
- `conversion_engine.py`、`rapid_ocr_engine.py` 提供转换和可选离线 OCR。
- `tests/` 保存转录、媒体和浏览器集成相关的本地回归用例。

## 开发过程

[查看开发历程](DEVELOPMENT.md)

## 许可与使用范围

项目原创代码采用 MIT。第三方程序、模型和字典按对应原许可使用。仅处理你有权访问和转换的内容。
