"""Extract and persist user-uploaded research PDFs."""

from __future__ import annotations

import hashlib
import io
import re
from functools import lru_cache
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable
from urllib.parse import quote
from urllib.request import Request, urlopen
import json

from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
PAPER_DIR = ROOT / "data" / "papers"
MAX_PDF_BYTES = 15_000_000
MAX_PAGES = 150
MAX_CHARS = 2_000_000
MAX_VISION_PAGES = 12


@lru_cache(maxsize=1)
def _ocr_engine():
    from rapidocr import RapidOCR
    return RapidOCR()


def local_ocr(image: bytes) -> tuple[str, float]:
    result = _ocr_engine()(image)
    lines = [str(value).strip() for value in (result.txts if result.txts is not None else ()) if str(value).strip()]
    scores = [float(value) for value in (result.scores if result.scores is not None else ())]
    return "\n".join(lines), sum(scores) / len(scores) if scores else 0.0


def _render_page(raw: bytes, page_number: int) -> bytes:
    import pymupdf
    with pymupdf.open(stream=raw, filetype="pdf") as document:
        page = document.load_page(page_number - 1)
        image = page.get_pixmap(dpi=150, alpha=False).tobytes("png")
        if len(image) > 4_000_000:
            image = page.get_pixmap(dpi=110, alpha=False).tobytes("png")
    return image


def _clean(value: Any, maximum: int = 500) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()[:maximum]


def _doi(text: str) -> str:
    match = re.search(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", text, re.I)
    return match.group(0).rstrip(".,;)") if match else ""


def crossref_metadata(doi: str) -> dict[str, Any]:
    request = Request(f"https://api.crossref.org/works/{quote(doi, safe='/')}",
                      headers={"User-Agent": "Suyuan-Course-Demo/0.1 (bibliographic metadata lookup)"})
    with urlopen(request, timeout=8) as response:
        item = json.load(response).get("message", {})
    author_names = [" ".join(str(part).strip() for part in (author.get("given"), author.get("family")) if part)
                    for author in item.get("author", []) if isinstance(author, dict)]
    dates = next((item[key].get("date-parts", []) for key in ("published", "published-print", "published-online", "issued")
                  if isinstance(item.get(key), dict) and item[key].get("date-parts")), [])
    year = str(dates[0][0]) if dates and dates[0] else ""
    return {"title": (item.get("title") or [""])[0], "authors": "; ".join(author_names),
            "year": year, "journal": (item.get("container-title") or [""])[0],
            "publisher": item.get("publisher") or ""}


def extract_pdf(raw: bytes, filename: str, *, title: str = "", authors: str = "", year: str = "",
                metadata_hint: dict[str, Any] | None = None,
                doi_lookup: Callable[[str], dict[str, Any]] | None = None,
                ocr_page: Callable[[bytes], tuple[str, float]] | None = None,
                vision_page: Callable[[bytes, int], str] | None = None,
                progress: Callable[[int, int, str], None] | None = None) -> dict[str, Any]:
    if not raw.startswith(b"%PDF-") or len(raw) > MAX_PDF_BYTES:
        raise ValueError("请选择不超过 15 MB 的 PDF 文件。")
    try:
        reader = PdfReader(io.BytesIO(raw), strict=False)
        if reader.is_encrypted:
            raise ValueError("PDF 已加密，请先提供可正常阅读的未加密文件。")
        count = len(reader.pages)
        if count < 1 or count > MAX_PAGES:
            raise ValueError("PDF 页数需在 1–150 页之间。")
        pages = []
        page_methods = []
        ocr_warnings = []
        vision_used = 0
        character_count = 0
        for page in reader.pages:
            number = len(pages) + 1
            text = (page.extract_text(extraction_mode="layout") or "").replace("\x00", "")
            text = re.sub(r"[ \t]+", " ", text).strip()
            method = "text"
            if len(re.sub(r"\s+", "", text)) < 80 and page.get_contents() is not None:
                if progress:
                    progress(number, count, "本地 OCR")
                try:
                    image = _render_page(raw, number)
                    try:
                        recognized, confidence = (ocr_page or local_ocr)(image)
                    except Exception as error:
                        recognized, confidence = "", 0.0
                        ocr_warnings.append(f"第 {number} 页本地 OCR 失败：{type(error).__name__}")
                    recognized = recognized.strip()
                    if len(recognized) > len(text):
                        text, method = recognized, "local_ocr"
                    if vision_page and vision_used < MAX_VISION_PAGES and (len(recognized) < 100 or confidence < 0.68):
                        if progress:
                            progress(number, count, "多模态补救")
                        vision_used += 1
                        try:
                            vision_text = vision_page(image, number).strip()
                            if len(vision_text) >= 40 and (len(vision_text) > len(text) or confidence < 0.68):
                                text, method = vision_text, "vision"
                        except Exception as error:
                            ocr_warnings.append(f"第 {number} 页多模态识别失败：{type(error).__name__}")
                            vision_page = None
                except Exception as error:
                    ocr_warnings.append(f"第 {number} 页本地 OCR 失败：{type(error).__name__}")
            pages.append(text)
            page_methods.append(method)
            character_count += len(text)
            if character_count > MAX_CHARS:
                raise ValueError("PDF 正文超过 200 万字，请拆分后导入。")
            if progress:
                progress(number, count, method)
    except ValueError:
        raise
    except Exception as error:
        raise ValueError("无法读取该 PDF，请确认文件没有损坏。") from error
    total = character_count
    if total < 200:
        raise ValueError("OCR 没有识别到足够的可检索文字；请检查扫描质量，或启用多模态补救。")
    metadata = reader.metadata or {}
    hint = metadata_hint if isinstance(metadata_hint, dict) else {}
    clean_name = Path(filename.replace("\\", "/")).name[:220]
    if not clean_name.lower().endswith(".pdf"):
        raise ValueError("文件名必须以 .pdf 结尾。")
    detected_title = _clean(metadata.get("/Title"))
    if detected_title.lower() in {"untitled", "microsoft word"}:
        detected_title = ""
    detected_authors = _clean(metadata.get("/Author"))
    first_lines = [line.strip() for line in pages[0].splitlines() if line.strip()]
    first_title = next((line for line in first_lines[:8] if 8 <= len(line) <= 160 and
                        not re.search(r"^(摘要|Abstract|DOI|ISSN|第\s*\d+|20\d{2})", line, re.I)), "")
    title_position = first_lines.index(first_title) if first_title else -1
    author_line = first_lines[title_position + 1] if 0 <= title_position < min(len(first_lines) - 1, 7) else ""
    if re.search(r"\d|@|http|摘要|abstract|university|institute|department|journal|doi|keywords", author_line, re.I) or len(author_line) > 120:
        author_line = ""
    embedded_year = re.search(r"(?:19|20)\d{2}", str(metadata.get("/CreationDate") or ""))
    first_page_year = re.search(r"\b(?:19|20)\d{2}\b", " ".join(first_lines[:15]))
    hint_doi = _doi(_clean(hint.get("doi")))
    detected_doi = hint_doi or _doi("\n".join(pages[:2]))
    catalogue = {}
    if detected_doi and doi_lookup:
        try:
            catalogue = doi_lookup(detected_doi) or {}
        except Exception:
            ocr_warnings.append("DOI 书目查询不可用，已保留网页和 PDF 元数据。")
    file_hash = hashlib.sha256(raw).hexdigest()
    year_value = str(year).strip()
    if year_value and (not year_value.isdigit() or not 1900 <= int(year_value) <= datetime.now(UTC).year + 1):
        raise ValueError("年份需为 1900 年至明年之间的四位数字。")
    result_title = _clean(title) or _clean(hint.get("title")) or _clean(catalogue.get("title")) or detected_title or first_title or clean_name[:-4]
    result_authors = _clean(authors) or _clean(hint.get("authors")) or _clean(catalogue.get("authors")) or detected_authors or _clean(author_line)
    result_year = (int(year_value) if year_value else
                   int(hint["year"]) if str(hint.get("year") or "").isdigit() and 1900 <= int(hint["year"]) <= datetime.now(UTC).year + 1 else
                   int(catalogue["year"]) if str(catalogue.get("year") or "").isdigit() and 1900 <= int(catalogue["year"]) <= datetime.now(UTC).year + 1 else
                   int(embedded_year.group()) if embedded_year else
                   int(first_page_year.group()) if first_page_year else None)
    sources = {"title": "manual" if title.strip() else "web" if hint.get("title") else "crossref" if catalogue.get("title") else "pdf_metadata" if detected_title else "first_page" if first_title else "filename",
               "authors": "manual" if authors.strip() else "web" if hint.get("authors") else "crossref" if catalogue.get("authors") else "pdf_metadata" if detected_authors else "first_page" if author_line else "unknown",
               "year": "manual" if year_value else "web" if hint.get("year") and result_year else "crossref" if catalogue.get("year") and result_year else "pdf_metadata" if embedded_year else "first_page" if first_page_year else "unknown",
               "doi": "web" if hint_doi else "first_pages" if detected_doi else "unknown"}
    return {"id": file_hash[:32], "file_hash": file_hash, "filename": clean_name,
            "title": result_title, "authors": result_authors,
            "year": result_year, "doi": detected_doi,
            "page_count": count, "text_chars": total, "pages": pages,
            "page_methods": page_methods, "ocr_warnings": ocr_warnings,
            "metadata": {"journal": _clean(hint.get("journal") or catalogue.get("journal")), "publisher": _clean(hint.get("publisher") or catalogue.get("publisher")),
                         "abstract": _clean(hint.get("abstract"), 3000), "sources": sources},
            "imported_at": datetime.now(UTC).isoformat()}


def save_file(raw: bytes, paper: dict[str, Any]) -> Path:
    PAPER_DIR.mkdir(parents=True, exist_ok=True)
    path = PAPER_DIR / f"{paper['id']}.pdf"
    if not path.exists():
        temporary = path.with_suffix(".tmp")
        temporary.write_bytes(raw)
        temporary.replace(path)
    return path
