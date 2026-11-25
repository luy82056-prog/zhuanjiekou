import base64
import csv
import importlib.util
import os
import shutil
import subprocess
from pathlib import Path


SUPPORTED_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".odt",
    ".xls", ".xlsx", ".ods", ".csv", ".epub",
}

WORD_EXTENSIONS = {".doc", ".docx", ".odt"}
EXCEL_EXTENSIONS = {".xls", ".xlsx", ".ods"}

FORMAT_LABELS = {
    "pdf": "PDF",
    "docx": "Word（DOCX）",
    "xlsx": "Excel（XLSX）",
    "csv": "CSV",
    "epub": "EPUB",
}


class ConversionError(RuntimeError):
    pass


class NeedsOcrError(ConversionError):
    pass


class ConversionCancelled(ConversionError):
    pass


def _first_existing(candidates):
    for candidate in candidates:
        if not candidate:
            continue
        found = shutil.which(candidate)
        if found:
            return Path(found)
        path = Path(candidate)
        if path.is_file():
            return path
    return None


def detect_engines():
    program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
    program_files_x86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    word = _first_existing([
        Path(program_files) / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
        Path(program_files_x86) / "Microsoft Office" / "root" / "Office16" / "WINWORD.EXE",
        "WINWORD.EXE",
    ])
    excel = _first_existing([
        Path(program_files) / "Microsoft Office" / "root" / "Office16" / "EXCEL.EXE",
        Path(program_files_x86) / "Microsoft Office" / "root" / "Office16" / "EXCEL.EXE",
        "EXCEL.EXE",
    ])
    libreoffice = _first_existing([
        Path(program_files) / "LibreOffice" / "program" / "soffice.exe",
        Path(program_files_x86) / "LibreOffice" / "program" / "soffice.exe",
        "soffice.exe",
    ])
    calibre = _first_existing([
        Path(program_files) / "Calibre2" / "ebook-convert.exe",
        Path(program_files_x86) / "Calibre2" / "ebook-convert.exe",
        "ebook-convert.exe",
    ])
    return {
        "word": word,
        "excel": excel,
        "libreoffice": libreoffice,
        "calibre": calibre,
        "pdf_tables": (
            importlib.util.find_spec("pdfplumber") is not None
            and importlib.util.find_spec("openpyxl") is not None
        ),
        "pdf_docx": (
            importlib.util.find_spec("pdfplumber") is not None
            and importlib.util.find_spec("docx") is not None
        ),
        "openpyxl": importlib.util.find_spec("openpyxl") is not None,
    }


def source_kind(path):
    suffix = Path(path).suffix.lower()
    if suffix == ".pdf":
        return "pdf"
    if suffix in WORD_EXTENSIONS:
        return "word"
    if suffix in EXCEL_EXTENSIONS:
        return "excel"
    if suffix == ".csv":
        return "csv"
    if suffix == ".epub":
        return "epub"
    return ""


def available_targets(path, engines):
    kind = source_kind(path)
    targets = []
    if kind == "pdf":
        if engines.get("word") or engines.get("libreoffice") or engines.get("pdf_docx"):
            targets.append("docx")
        if engines.get("calibre"):
            targets.append("epub")
        if engines.get("pdf_tables"):
            targets.append("xlsx")
    elif kind == "word":
        if engines.get("word") or engines.get("libreoffice"):
            targets.append("pdf")
        if engines.get("calibre"):
            targets.append("epub")
    elif kind == "excel":
        if engines.get("excel") or engines.get("libreoffice"):
            targets.extend(["pdf", "csv"])
        elif engines.get("openpyxl"):
            targets.append("csv")
    elif kind == "csv":
        if engines.get("excel") or engines.get("libreoffice") or engines.get("openpyxl"):
            targets.append("xlsx")
        if engines.get("excel") or engines.get("libreoffice"):
            targets.append("pdf")
    elif kind == "epub" and engines.get("calibre"):
        targets.extend(["docx", "pdf"])
    return targets


def engine_summary(engines):
    names = []
    if engines.get("word"):
        names.append("Microsoft Word")
    if engines.get("excel"):
        names.append("Microsoft Excel")
    if engines.get("libreoffice"):
        names.append("LibreOffice")
    if engines.get("calibre"):
        names.append("Calibre")
    if engines.get("pdf_tables"):
        names.append("PDF 表格提取")
    if engines.get("pdf_docx") and not engines.get("word"):
        names.append("基础 PDF 转 Word")
    return "、".join(names) if names else "未检测到可用转换引擎"


def _powershell_encoded(script):
    encoded = base64.b64encode(script.encode("utf-16le")).decode("ascii")
    return [
        "powershell.exe", "-NoLogo", "-NoProfile", "-NonInteractive",
        "-ExecutionPolicy", "Bypass", "-EncodedCommand", encoded,
    ]


def _ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def _file_uri(path):
    return Path(path).resolve().as_uri()


def inspect_pdf(path):
    try:
        from pypdf import PdfReader
    except ImportError:
        return
    try:
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                unlocked = reader.decrypt("")
            except Exception:
                unlocked = 0
            if not unlocked:
                raise ConversionError("PDF 已加密，需要先解除密码保护。")
        pages = len(reader.pages)
        if pages == 0:
            raise ConversionError("PDF 没有可转换的页面。")
        sample_pages = reader.pages[: min(pages, 8)]
        text_count = sum(len((page.extract_text() or "").strip()) for page in sample_pages)
        if text_count < max(3, len(sample_pages) * 2):
            raise NeedsOcrError("该 PDF 几乎没有文字层，可能是扫描件，需要 OCR 后再转换。")
    except (NeedsOcrError, ConversionError):
        raise
    except Exception as exc:
        raise ConversionError(f"无法读取 PDF：{exc}") from exc


class ConversionRouter:
    def __init__(self, engines=None):
        self.engines = engines or detect_engines()

    def targets_for(self, source):
        return available_targets(source, self.engines)

    def convert(self, source, target, job_dir, log_path, on_stage=None,
                set_process=None, is_cancelled=None):
        source = Path(source)
        job_dir = Path(job_dir)
        log_path = Path(log_path)
        if not source.is_file():
            raise ConversionError(f"源文件不存在：{source}")
        if source.suffix.lower() not in SUPPORTED_EXTENSIONS:
            raise ConversionError(f"不支持的源格式：{source.suffix}")
        if target not in self.targets_for(source):
            raise ConversionError("当前电脑缺少完成此转换所需的离线引擎。")
        job_dir.mkdir(parents=True, exist_ok=True)
        log_path.parent.mkdir(parents=True, exist_ok=True)
        staged_source = job_dir / ("input" + source.suffix.lower())
        shutil.copy2(source, staged_source)
        staged_output = job_dir / ("result." + target)

        def stage(text, percent):
            if is_cancelled and is_cancelled():
                raise ConversionCancelled("任务已取消。")
            if on_stage:
                on_stage(text, percent)

        stage("准备", 10)
        if source.suffix.lower() == ".pdf":
            inspect_pdf(staged_source)
        stage("转换", 35)

        kind = source_kind(source)
        if kind == "pdf" and target == "xlsx":
            self._pdf_tables_to_xlsx(staged_source, staged_output)
        elif kind == "pdf" and target == "docx":
            self._pdf_to_docx(staged_source, staged_output, job_dir, log_path, set_process)
        elif target == "epub":
            if source.suffix.lower() == ".doc":
                if not self.engines.get("libreoffice"):
                    raise ConversionError("旧版 DOC→EPUB 需要 LibreOffice 作为中间转换引擎。")
                intermediate = self._libreoffice(
                    staged_source, "docx", job_dir, log_path, set_process
                )
                self._calibre(intermediate, staged_output, log_path, set_process)
            else:
                self._calibre(staged_source, staged_output, log_path, set_process)
        elif kind == "epub":
            self._calibre(staged_source, staged_output, log_path, set_process)
        elif kind == "word" and target == "pdf":
            self._word_to_pdf(staged_source, staged_output, job_dir, log_path, set_process)
        elif kind == "excel" and target in {"pdf", "csv"}:
            self._excel_convert(staged_source, staged_output, job_dir, log_path, set_process)
        elif kind == "csv" and target in {"xlsx", "pdf"}:
            self._excel_convert(staged_source, staged_output, job_dir, log_path, set_process)
        else:
            raise ConversionError("尚未实现该转换路径。")

        if is_cancelled and is_cancelled():
            raise ConversionCancelled("任务已取消。")
        if not staged_output.is_file() or staged_output.stat().st_size == 0:
            raise ConversionError("转换引擎已退出，但没有生成有效文件。")
        stage("保存", 90)
        return staged_output

    def _run(self, command, log_path, set_process, cwd=None):
        with Path(log_path).open("ab") as log:
            log.write(("\n> " + " ".join(str(item) for item in command) + "\n").encode(
                "utf-8", errors="replace"
            ))
            process = subprocess.Popen(
                [str(item) for item in command],
                cwd=str(cwd) if cwd else None,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
            if set_process:
                set_process(process)
            code = process.wait()
            if set_process:
                set_process(None)
        if code != 0:
            detail = Path(log_path).read_text(encoding="utf-8", errors="replace")[-1600:]
            raise ConversionError(
                "转换引擎执行失败。"
                + (f"\n{detail.strip()}" if detail.strip() else f"退出代码：{code}")
            )

    @staticmethod
    def _office_pids(image_name):
        try:
            output = subprocess.check_output(
                ["tasklist.exe", "/FI", f"IMAGENAME eq {image_name}", "/FO", "CSV", "/NH"],
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            ).decode("mbcs", errors="replace")
        except Exception:
            return set()
        pids = set()
        for row in csv.reader(output.splitlines()):
            if len(row) >= 2 and row[0].lower() == image_name.lower():
                try:
                    pids.add(int(row[1]))
                except ValueError:
                    pass
        return pids

    def _run_office(self, command, log_path, set_process, cwd, image_name):
        before = self._office_pids(image_name)
        try:
            self._run(command, log_path, set_process, cwd=cwd)
        finally:
            leaked = self._office_pids(image_name) - before
            for process_id in leaked:
                subprocess.run(
                    ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
                )

    def _pdf_to_docx(self, source, output, job_dir, log_path, set_process):
        errors = []
        if self.engines.get("word"):
            script = (
                "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                "$w=$null;$d=$null;try{"
                "$w=New-Object -ComObject Word.Application;"
                "$w.Visible=$false;$w.DisplayAlerts=0;"
                f"$d=$w.Documents.Open({_ps_literal(source)},$false,$true,$false);"
                f"$d.SaveAs2({_ps_literal(output)},16)"
                "}finally{"
                "if($d){try{$d.Close($false)}catch{}};"
                "if($w){try{$w.Quit()}catch{};"
                "try{[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($w)}catch{}}"
                "}"
            )
            try:
                self._run_office(
                    _powershell_encoded(script), log_path, set_process,
                    cwd=job_dir, image_name="WINWORD.EXE",
                )
                return
            except ConversionError as exc:
                errors.append("Word：" + str(exc))
        if self.engines.get("libreoffice"):
            try:
                produced = self._libreoffice(source, "docx", job_dir, log_path, set_process)
                shutil.move(str(produced), str(output))
                return
            except ConversionError as exc:
                errors.append("LibreOffice：" + str(exc))
        try:
            self._basic_pdf_to_docx(source, output)
            return
        except ConversionError as exc:
            errors.append("内置基础转换：" + str(exc))
        raise ConversionError("\n".join(errors) or "没有可用的 PDF→Word 引擎。")

    def _word_to_pdf(self, source, output, job_dir, log_path, set_process):
        errors = []
        if self.engines.get("word") and source.suffix.lower() in {".doc", ".docx"}:
            script = (
                "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                "$w=$null;$d=$null;try{"
                "$w=New-Object -ComObject Word.Application;"
                "$w.Visible=$false;$w.DisplayAlerts=0;"
                f"$d=$w.Documents.Open({_ps_literal(source)},$false,$true,$false);"
                f"$d.ExportAsFixedFormat({_ps_literal(output)},17)"
                "}finally{"
                "if($d){try{$d.Close($false)}catch{}};"
                "if($w){try{$w.Quit()}catch{};"
                "try{[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($w)}catch{}}"
                "}"
            )
            try:
                self._run_office(
                    _powershell_encoded(script), log_path, set_process,
                    cwd=job_dir, image_name="WINWORD.EXE",
                )
                return
            except ConversionError as exc:
                errors.append("Word：" + str(exc))
        if self.engines.get("libreoffice"):
            try:
                produced = self._libreoffice(source, "pdf", job_dir, log_path, set_process)
                shutil.move(str(produced), str(output))
                return
            except ConversionError as exc:
                errors.append("LibreOffice：" + str(exc))
        raise ConversionError("\n".join(errors) or "没有可用的 Word→PDF 引擎。")

    def _excel_convert(self, source, output, job_dir, log_path, set_process):
        target = output.suffix.lower().lstrip(".")
        errors = []
        if self.engines.get("excel") and source.suffix.lower() in {".xls", ".xlsx", ".csv"}:
            if target == "pdf":
                action = f"$b.ExportAsFixedFormat(0,{_ps_literal(output)})"
            elif target == "csv":
                action = f"$b.SaveAs({_ps_literal(output)},62)"
            else:
                action = f"$b.SaveAs({_ps_literal(output)},51)"
            script = (
                "$ErrorActionPreference='Stop';$ProgressPreference='SilentlyContinue';"
                "$e=$null;$b=$null;try{"
                "$e=New-Object -ComObject Excel.Application;"
                "$e.Visible=$false;$e.DisplayAlerts=$false;"
                f"$b=$e.Workbooks.Open({_ps_literal(source)},0,$true);"
                f"{action}"
                "}finally{"
                "if($b){try{$b.Close($false)}catch{}};"
                "if($e){try{$e.Quit()}catch{};"
                "try{[void][Runtime.InteropServices.Marshal]::FinalReleaseComObject($e)}catch{}}"
                "}"
            )
            try:
                self._run_office(
                    _powershell_encoded(script), log_path, set_process,
                    cwd=job_dir, image_name="EXCEL.EXE",
                )
                return
            except ConversionError as exc:
                errors.append("Excel：" + str(exc))
        if self.engines.get("libreoffice"):
            try:
                produced = self._libreoffice(source, target, job_dir, log_path, set_process)
                shutil.move(str(produced), str(output))
                return
            except ConversionError as exc:
                errors.append("LibreOffice：" + str(exc))
        if target in {"csv", "xlsx"} and self.engines.get("openpyxl"):
            try:
                self._openpyxl_convert(source, output)
                return
            except Exception as exc:
                errors.append("内置表格转换：" + str(exc))
        raise ConversionError("\n".join(errors) or "没有可用的表格转换引擎。")

    def _libreoffice(self, source, target, job_dir, log_path, set_process):
        profile = Path(job_dir) / "lo-profile"
        profile.mkdir(parents=True, exist_ok=True)
        before = {path.resolve() for path in Path(job_dir).glob("*")}
        command = [
            self.engines["libreoffice"],
            f"-env:UserInstallation={_file_uri(profile)}",
            "--headless", "--nologo", "--nodefault", "--nofirststartwizard",
            "--convert-to", target, "--outdir", job_dir, source,
        ]
        self._run(command, log_path, set_process, cwd=job_dir)
        candidates = [
            path for path in Path(job_dir).glob("*." + target)
            if path.resolve() not in before and path.is_file()
        ]
        if not candidates:
            candidates = [
                path for path in Path(job_dir).glob("*." + target)
                if path.is_file() and path.name != ("result." + target)
            ]
        if not candidates:
            raise ConversionError("LibreOffice 没有生成目标文件。")
        return max(candidates, key=lambda path: path.stat().st_mtime)

    def _calibre(self, source, output, log_path, set_process):
        if not self.engines.get("calibre"):
            raise ConversionError("未检测到 Calibre ebook-convert。")
        self._run(
            [self.engines["calibre"], source, output],
            log_path, set_process, cwd=source.parent,
        )

    @staticmethod
    def _pdf_tables_to_xlsx(source, output):
        try:
            import pdfplumber
            from openpyxl import Workbook
        except ImportError as exc:
            raise ConversionError("缺少 PDF 表格提取组件。") from exc
        workbook = Workbook()
        workbook.remove(workbook.active)
        table_index = 0
        try:
            with pdfplumber.open(str(source)) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    for local_index, table in enumerate(page.extract_tables() or [], start=1):
                        if not table or not any(any(cell for cell in row) for row in table):
                            continue
                        table_index += 1
                        sheet = workbook.create_sheet(
                            title=f"P{page_number}_T{local_index}"[:31]
                        )
                        for row in table:
                            sheet.append([
                                "" if value is None else str(value) for value in row
                            ])
        except Exception as exc:
            raise ConversionError(f"PDF 表格提取失败：{exc}") from exc
        if table_index == 0:
            raise ConversionError("该 PDF 中未检测到可导出的表格。")
        workbook.save(str(output))

    @staticmethod
    def _basic_pdf_to_docx(source, output):
        try:
            import pdfplumber
            from docx import Document
        except ImportError as exc:
            raise ConversionError("缺少基础 PDF→Word 组件。") from exc
        document = Document()
        content_count = 0
        try:
            with pdfplumber.open(str(source)) as pdf:
                for page_number, page in enumerate(pdf.pages, start=1):
                    if page_number > 1:
                        document.add_page_break()
                    text = (page.extract_text() or "").strip()
                    if text:
                        for line in text.splitlines():
                            if line.strip():
                                document.add_paragraph(line.strip())
                                content_count += 1
                    for table_data in page.extract_tables() or []:
                        if not table_data:
                            continue
                        width = max((len(row) for row in table_data), default=0)
                        if width == 0:
                            continue
                        table = document.add_table(rows=0, cols=width)
                        table.style = "Table Grid"
                        for source_row in table_data:
                            cells = table.add_row().cells
                            for index in range(width):
                                value = source_row[index] if index < len(source_row) else ""
                                cells[index].text = "" if value is None else str(value)
                        content_count += 1
        except Exception as exc:
            raise ConversionError(f"基础 PDF→Word 转换失败：{exc}") from exc
        if content_count == 0:
            raise ConversionError("没有提取到可写入 Word 的内容。")
        document.save(str(output))

    @staticmethod
    def _openpyxl_convert(source, output):
        from openpyxl import Workbook, load_workbook
        if source.suffix.lower() == ".csv" and output.suffix.lower() == ".xlsx":
            workbook = Workbook()
            sheet = workbook.active
            sheet.title = "Sheet1"
            with source.open("r", encoding="utf-8-sig", newline="") as handle:
                for row in csv.reader(handle):
                    sheet.append(row)
            workbook.save(str(output))
            return
        if source.suffix.lower() == ".xlsx" and output.suffix.lower() == ".csv":
            workbook = load_workbook(str(source), read_only=True, data_only=True)
            sheet = workbook.active
            with output.open("w", encoding="utf-8-sig", newline="") as handle:
                writer = csv.writer(handle)
                for row in sheet.iter_rows(values_only=True):
                    writer.writerow(["" if value is None else value for value in row])
            workbook.close()
            return
        raise ConversionError("内置表格转换仅支持 CSV↔XLSX。")
