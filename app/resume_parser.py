"""
Resume parser — extracts text from PDF and DOCX files inside a ZIP archive.

Supports:
  - PDF  (via PyPDF2)
  - DOCX (via python-docx)
  - TXT  (plain text fallback)
"""

from __future__ import annotations

import io
import zipfile
from pathlib import Path
from typing import Optional

import structlog

log = structlog.get_logger(__name__)


def extract_text_from_pdf(data: bytes) -> str:
    """Extract text from PDF bytes."""
    try:
        from PyPDF2 import PdfReader

        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n".join(pages).strip()
    except Exception as e:
        log.warning("pdf_parse_error", error=str(e))
        return ""


def extract_text_from_docx(data: bytes) -> str:
    """Extract text from DOCX bytes."""
    try:
        from docx import Document

        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n".join(paragraphs).strip()
    except Exception as e:
        log.warning("docx_parse_error", error=str(e))
        return ""


def extract_text_from_txt(data: bytes) -> str:
    """Extract text from plain text bytes."""
    try:
        return data.decode("utf-8", errors="replace").strip()
    except Exception:
        return ""


def parse_single_resume(filename: str, data: bytes) -> Optional[str]:
    """Extract text from a single file based on extension."""
    ext = Path(filename).suffix.lower()
    if ext == ".pdf":
        return extract_text_from_pdf(data)
    elif ext in (".docx", ".doc"):
        return extract_text_from_docx(data)
    elif ext == ".txt":
        return extract_text_from_txt(data)
    else:
        log.debug("unsupported_file_type", filename=filename, ext=ext)
        return None


def parse_resumes_from_zip(zip_data: bytes) -> list[dict]:
    """
    Parse all resumes from a ZIP archive.

    Returns list of dicts:
      [{"filename": "john_doe.pdf", "text": "...", "error": None}, ...]
    """
    results = []
    try:
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            for info in zf.infolist():
                # Skip directories and hidden files
                if info.is_dir():
                    continue
                name = Path(info.filename).name
                if name.startswith(".") or name.startswith("__"):
                    continue

                ext = Path(name).suffix.lower()
                if ext not in (".pdf", ".docx", ".doc", ".txt"):
                    continue

                try:
                    data = zf.read(info.filename)
                    text = parse_single_resume(name, data)
                    if text and len(text) > 50:
                        results.append({
                            "filename": name,
                            "text": text,
                            "error": None,
                        })
                        log.debug("resume_parsed", filename=name, chars=len(text))
                    else:
                        results.append({
                            "filename": name,
                            "text": text or "",
                            "error": "Could not extract meaningful text",
                        })
                except Exception as e:
                    results.append({
                        "filename": name,
                        "text": "",
                        "error": str(e),
                    })
                    log.warning("resume_parse_error", filename=name, error=str(e))

    except zipfile.BadZipFile:
        raise ValueError("Invalid ZIP file")

    log.info("resumes_parsed", total=len(results), success=sum(1 for r in results if not r["error"]))
    return results
