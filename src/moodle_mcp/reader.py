"""Read and search the text of Moodle course files (PDF, DOCX, PPTX, XLSX, text, HTML).

Files are downloaded once into a cache directory (``$MOODLE_MCP_CACHE_DIR`` or
``~/.cache/moodle-mcp``) through :mod:`downloads`, so the Moodle token is never exposed.
Text extraction is done locally; Office formats are read straight from their zip/XML
structure, only PDF needs a dependency (``pypdf``).
"""

from __future__ import annotations

import hashlib
import html.parser
import io
import re
import unicodedata
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

from typing_extensions import TypedDict

from . import downloads
from .logger import logger
from .utils import getenv

CACHE_DIR_ENV = "MOODLE_MCP_CACHE_DIR"
MAX_FILE_BYTES = 50 * 1024 * 1024  # larger files are not read
MAX_XML_BYTES = 100 * 1024 * 1024  # decompressed size cap per zip member (zip-bomb guard)
MAX_PDF_PAGES = 1000
MAX_SHEET_ROWS = 5000
DEFAULT_PAGE_CHARS = 20000

TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".yml", ".yaml", ".log", ".tex", ".rtf",
    ".py", ".java", ".js", ".ts", ".c", ".h", ".cpp", ".cs", ".sql", ".r", ".sh", ".php", ".css",
    ".ini", ".cfg", ".toml", ".kt", ".go", ".rs", ".ipynb",
}
HTML_EXTENSIONS = {".html", ".htm"}
SUPPORTED_EXTENSIONS = {".pdf", ".docx", ".pptx", ".xlsx"} | TEXT_EXTENSIONS | HTML_EXTENSIONS


class FileText(TypedDict):
    moduleid: int
    filename: str
    path: str
    extractor: str
    total_chars: int
    page: int
    total_pages: int
    has_more: bool
    text: str
    warnings: list[str]


class SearchHit(TypedDict):
    moduleid: int
    module: str
    section: str
    filename: str
    location: str
    snippet: str


class SearchResult(TypedDict):
    courseid: int
    query: str
    files_searched: int
    files_skipped: list[str]
    hits: list[SearchHit]
    truncated: bool


class UnsupportedFile(ValueError):
    pass


# ---------------------------------------------------------------------------
# Extractors. Each returns (text, warnings).
# ---------------------------------------------------------------------------


def _decode(data: bytes) -> str:
    for encoding in ("utf-8-sig", "utf-8", "cp1250"):  # cp1250: common in Czech course material
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


class _HTMLText(html.parser.HTMLParser):
    _BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section", "article"}
    _SKIP = {"script", "style", "head"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in self._SKIP:
            self._skip += 1
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in self._SKIP:
            self._skip = max(0, self._skip - 1)
        elif tag in self._BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if not self._skip:
            self.parts.append(data)


def _extract_html(data: bytes) -> tuple[str, list[str]]:
    parser = _HTMLText()
    parser.feed(_decode(data))
    return _tidy("".join(parser.parts)), []


def _extract_text(data: bytes) -> tuple[str, list[str]]:
    return _tidy(_decode(data)), []


def _tidy(text: str) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\x00", "")
    text = re.sub(r"[ \t\f\v]+\n", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def _extract_pdf(data: bytes) -> tuple[str, list[str]]:
    try:
        from pypdf import PdfReader
    except ImportError as e:  # pragma: no cover - declared dependency
        raise UnsupportedFile("PDF support needs the `pypdf` package") from e
    warnings: list[str] = []
    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        try:
            if not reader.decrypt(""):
                raise UnsupportedFile("PDF is password protected")
        except UnsupportedFile:
            raise
        except Exception as e:
            raise UnsupportedFile("PDF is encrypted and cannot be read") from e
    pages = reader.pages
    total = len(pages)
    if total > MAX_PDF_PAGES:
        warnings.append(f"PDF has {total} pages; only the first {MAX_PDF_PAGES} were read")
    parts: list[str] = []
    empty = 0
    for number in range(min(total, MAX_PDF_PAGES)):
        try:
            text = (pages[number].extract_text() or "").strip()
        except Exception:  # damaged page: keep going
            text = ""
            warnings.append(f"Page {number + 1} could not be read")
        if not text:
            empty += 1
        parts.append(f"--- Page {number + 1} ---\n{text}")
    if total and empty == min(total, MAX_PDF_PAGES):
        warnings.append("No text found: the PDF is probably scanned images (OCR is not supported)")
    return _tidy("\n\n".join(parts)), warnings


def _zip_member(archive: zipfile.ZipFile, name: str) -> bytes:
    info = archive.getinfo(name)
    if info.file_size > MAX_XML_BYTES:
        raise UnsupportedFile(f"{name} is too large when decompressed")
    with archive.open(info) as handle:
        data = handle.read(MAX_XML_BYTES + 1)
    if len(data) > MAX_XML_BYTES:
        raise UnsupportedFile(f"{name} is too large when decompressed")
    return data


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1]


def _paragraphs(root: ET.Element, para: str, text_tags: dict[str, str]) -> list[str]:
    """Collect paragraphs (elements named ``para``) as lines; text_tags maps tag -> emitted string
    ('' means use the element's text)."""
    lines: list[str] = []
    for p in root.iter():
        if _local(p.tag) != para:
            continue
        pieces: list[str] = []
        for node in p.iter():
            name = _local(node.tag)
            if name in text_tags:
                pieces.append((node.text or "") if text_tags[name] == "" else text_tags[name])
        line = "".join(pieces).strip()
        if line:
            lines.append(line)
    return lines


def _open_zip(data: bytes, expected: str) -> zipfile.ZipFile:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as e:
        raise UnsupportedFile(f"Not a valid {expected} file (old binary Office formats are not supported)") from e
    return archive


def _extract_docx(data: bytes) -> tuple[str, list[str]]:
    archive = _open_zip(data, "DOCX")
    if "word/document.xml" not in archive.namelist():
        raise UnsupportedFile("Not a valid DOCX file")
    root = ET.fromstring(_zip_member(archive, "word/document.xml"))
    lines = _paragraphs(root, "p", {"t": "", "tab": "\t", "br": " "})
    return _tidy("\n".join(lines)), []


def _natural_key(name: str) -> list:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", name)]


def _extract_pptx(data: bytes) -> tuple[str, list[str]]:
    archive = _open_zip(data, "PPTX")
    names = archive.namelist()
    slides = sorted((n for n in names if re.fullmatch(r"ppt/slides/slide\d+\.xml", n)), key=_natural_key)
    if not slides:
        raise UnsupportedFile("Not a valid PPTX file")
    parts: list[str] = []
    for index, name in enumerate(slides, 1):
        lines = _paragraphs(ET.fromstring(_zip_member(archive, name)), "p", {"t": ""})
        block = f"--- Slide {index} ---\n" + "\n".join(lines)
        number = re.search(r"(\d+)", name).group(1)
        notes_name = f"ppt/notesSlides/notesSlide{number}.xml"
        if notes_name in names:
            notes = _paragraphs(ET.fromstring(_zip_member(archive, notes_name)), "p", {"t": ""})
            # Notes slides also contain the slide number placeholder; drop pure digits.
            notes = [n for n in notes if not n.isdigit()]
            if notes:
                block += "\n[Notes]\n" + "\n".join(notes)
        parts.append(block)
    return _tidy("\n\n".join(parts)), []


def _extract_xlsx(data: bytes) -> tuple[str, list[str]]:
    archive = _open_zip(data, "XLSX")
    names = archive.namelist()
    if "xl/workbook.xml" not in names:
        raise UnsupportedFile("Not a valid XLSX file")
    warnings: list[str] = []

    shared: list[str] = []
    if "xl/sharedStrings.xml" in names:
        for si in ET.fromstring(_zip_member(archive, "xl/sharedStrings.xml")):
            shared.append("".join(t.text or "" for t in si.iter() if _local(t.tag) == "t"))

    workbook = ET.fromstring(_zip_member(archive, "xl/workbook.xml"))
    rels: dict[str, str] = {}
    if "xl/_rels/workbook.xml.rels" in names:
        for rel in ET.fromstring(_zip_member(archive, "xl/_rels/workbook.xml.rels")):
            rels[rel.get("Id", "")] = rel.get("Target", "")

    parts: list[str] = []
    for sheet in workbook.iter():
        if _local(sheet.tag) != "sheet":
            continue
        rid = next((v for k, v in sheet.attrib.items() if _local(k) == "id"), "")
        target = rels.get(rid, "")
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        if path not in names:
            continue
        rows: list[str] = []
        for row in ET.fromstring(_zip_member(archive, path)).iter():
            if _local(row.tag) != "row":
                continue
            if len(rows) >= MAX_SHEET_ROWS:
                warnings.append(f"Sheet '{sheet.get('name')}' truncated at {MAX_SHEET_ROWS} rows")
                break
            cells: list[str] = []
            for cell in row:
                if _local(cell.tag) != "c":
                    continue
                kind = cell.get("t")
                value = next((c for c in cell if _local(c.tag) == "v"), None)
                if kind == "s" and value is not None and (value.text or "").isdigit() and int(value.text) < len(shared):
                    cells.append(shared[int(value.text)])
                elif kind == "inlineStr":
                    cells.append("".join(t.text or "" for t in cell.iter() if _local(t.tag) == "t"))
                elif value is not None:
                    cells.append(value.text or "")
                else:
                    cells.append("")
            if any(cells):
                rows.append("\t".join(cells).rstrip("\t"))
        parts.append(f"--- Sheet: {sheet.get('name')} ---\n" + "\n".join(rows))
    return _tidy("\n\n".join(parts)), warnings


def extract_text(data: bytes, filename: str) -> tuple[str, str, list[str]]:
    """Return ``(text, extractor_name, warnings)`` or raise :class:`UnsupportedFile`."""
    try:
        return _extract_text_unchecked(data, filename)
    except (ET.ParseError, zipfile.BadZipFile, KeyError) as e:
        raise UnsupportedFile(f"The file is damaged and cannot be read ({type(e).__name__})") from None
    except UnsupportedFile:
        raise
    except Exception as e:  # parser libraries (pypdf) raise many types on corrupt input
        raise UnsupportedFile(f"The file could not be read ({type(e).__name__})") from None


def _extract_text_unchecked(data: bytes, filename: str) -> tuple[str, str, list[str]]:
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        text, warnings = _extract_pdf(data)
    elif ext == ".docx":
        text, warnings = _extract_docx(data)
    elif ext == ".pptx":
        text, warnings = _extract_pptx(data)
    elif ext == ".xlsx":
        text, warnings = _extract_xlsx(data)
    elif ext in HTML_EXTENSIONS:
        text, warnings = _extract_html(data)
    elif ext in TEXT_EXTENSIONS:
        text, warnings = _extract_text(data)
    else:
        raise UnsupportedFile(
            f"Unsupported file type '{ext or filename}'. Supported: pdf, docx, pptx, xlsx, html and plain text/code. "
            "Use download_course_file to get the file itself."
        )
    return text, ext.lstrip("."), warnings


# ---------------------------------------------------------------------------
# Cache + MCP-facing functions
# ---------------------------------------------------------------------------


def _cache_root() -> Path:
    base = getenv(CACHE_DIR_ENV) or str(Path.home() / ".cache" / "moodle-mcp")
    return Path(base).expanduser()


def _cached_path(courseid: int, meta: dict) -> Path:
    key = hashlib.sha256(f"{meta['filepath']}\0{meta['filename']}".encode()).hexdigest()[:12]
    version = meta.get("timemodified", 0)
    return (_cache_root() / str(courseid) / str(meta["moduleid"]) / f"{key}-{version}"
            / downloads._safe_filename(meta["filename"]))


def _ensure_local(courseid: int, meta: dict, url: str) -> Path:
    if meta["filesize"] > MAX_FILE_BYTES:
        raise UnsupportedFile(f"File is larger than {MAX_FILE_BYTES // (1024 * 1024)} MiB")
    path = _cached_path(courseid, meta)
    if path.is_file() and (not meta["filesize"] or path.stat().st_size == meta["filesize"]):
        return path
    path.unlink(missing_ok=True)
    downloads._download_file(meta, url, path)
    return path


def _text_of(courseid: int, meta: dict, url: str) -> tuple[str, str, list[str], Path]:
    path = _ensure_local(courseid, meta, url)
    sidecar = path.with_name(path.name + ".txt")
    if sidecar.is_file():
        return sidecar.read_text(encoding="utf-8"), path.suffix.lstrip(".").lower(), [], path
    text, extractor, warnings = extract_text(path.read_bytes(), meta["filename"])
    try:
        sidecar.write_text(text, encoding="utf-8")
    except OSError:
        pass  # caching the extraction is optional
    return text, extractor, warnings, path


def read_course_file(courseid: int, moduleid: int, filename: str | None = None,
                     filepath: str | None = None, page: int = 0,
                     max_chars: int = DEFAULT_PAGE_CHARS) -> FileText:
    """Return the text of one course file, split into pages of ``max_chars`` characters."""
    if page < 0:
        raise ValueError("page must be >= 0")
    if not 1000 <= max_chars <= 100000:
        raise ValueError("max_chars must be between 1000 and 100000")
    matches = [(meta, url) for meta, url in downloads._files(courseid)
               if meta["moduleid"] == moduleid
               and (filename is None or meta["filename"] == filename)
               and (filepath is None or meta["filepath"] == filepath)]
    if len(matches) != 1:
        raise ValueError("File not found or ambiguous; use list_course_files and specify filename/filepath")
    meta, url = matches[0]
    text, extractor, warnings, path = _text_of(courseid, meta, url)
    total_pages = max(1, -(-len(text) // max_chars))
    if page >= total_pages:
        raise ValueError(f"page out of range: file has {total_pages} page(s) of {max_chars} characters")
    chunk = text[page * max_chars:(page + 1) * max_chars]
    if not text:
        warnings = warnings + ["The file contains no extractable text"]
    return {"moduleid": moduleid, "filename": meta["filename"], "path": str(path),
            "extractor": extractor, "total_chars": len(text), "page": page,
            "total_pages": total_pages, "has_more": page + 1 < total_pages,
            "text": chunk, "warnings": warnings}


def _fold(text: str) -> str:
    """Lower-case and strip diacritics so 'ekonomie' matches 'Ekonomické' style Czech text."""
    return "".join(c for c in unicodedata.normalize("NFD", text.casefold()) if not unicodedata.combining(c))


def _locate(text: str, position: int) -> str:
    """Nearest preceding '--- Page/Slide/Sheet ... ---' marker."""
    marker = None
    for marker in re.finditer(r"^--- (Page \d+|Slide \d+|Sheet: .+?) ---$", text[:position], re.M):
        pass
    return marker.group(1) if marker else ""


def search_course_files(courseid: int, query: str, max_results: int = 20,
                        context_chars: int = 120, max_files: int = 100) -> SearchResult:
    """Search the text of all readable files in a course (accent- and case-insensitive)."""
    query = query.strip()
    if len(query) < 2:
        raise ValueError("query must have at least 2 characters")
    if not 1 <= max_results <= 100:
        raise ValueError("max_results must be between 1 and 100")
    needle = _fold(query)
    result: SearchResult = {"courseid": courseid, "query": query, "files_searched": 0,
                            "files_skipped": [], "hits": [], "truncated": False}
    candidates = [(m, u) for m, u in downloads._files(courseid)
                  if Path(m["filename"]).suffix.lower() in SUPPORTED_EXTENSIONS]
    if len(candidates) > max_files:
        result["truncated"] = True
        candidates = candidates[:max_files]
    for meta, url in candidates:
        try:
            text, _, _, _ = _text_of(courseid, meta, url)
        except Exception as exc:  # noqa: BLE001 - one bad file must not break the search
            logger.info(f"Skipping {meta['filename']}: {type(exc).__name__}")
            # Only our own error types carry safe messages (others may contain URLs).
            reason = str(exc) if isinstance(exc, (UnsupportedFile, RuntimeError)) else type(exc).__name__
            result["files_skipped"].append(f"{meta['filename']}: {reason}")
            continue
        result["files_searched"] += 1
        folded = _fold(text)
        if len(folded) != len(text):  # folding changed offsets (rare): fall back to plain casefold
            folded = text.casefold()
            needle_used = query.casefold()
        else:
            needle_used = needle
        start = 0
        while (index := folded.find(needle_used, start)) != -1:
            if len(result["hits"]) >= max_results:
                result["truncated"] = True
                return result
            lo, hi = max(0, index - context_chars), min(len(text), index + len(needle_used) + context_chars)
            result["hits"].append({
                "moduleid": meta["moduleid"], "module": meta["module"], "section": meta["section"],
                "filename": meta["filename"], "location": _locate(text, index),
                "snippet": ("…" if lo else "") + " ".join(text[lo:hi].split()) + ("…" if hi < len(text) else ""),
            })
            start = index + len(needle_used)
    return result
