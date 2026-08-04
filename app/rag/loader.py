"""
Document loaders — one function per file type. Called by TenantRAGService when
building a tenant's knowledge base for the first time.
"""

from __future__ import annotations

import logging
import os

import PyPDF2
import docx


log = logging.getLogger("rag.loader")


def _read_txt(path: str) -> str:
    with open(path, "r", encoding="utf-8") as f:
        return f.read()


def _read_docx(path: str) -> str:
    doc = docx.Document(path)
    return "\n".join(p.text for p in doc.paragraphs)


def _read_pdf(path: str) -> str:
    text = ""
    with open(path, "rb") as f:
        reader = PyPDF2.PdfReader(f)
        if reader.is_encrypted:
            try:
                reader.decrypt("")
            except Exception as e:
                log.error(f"[loader] cannot decrypt PDF {path}: {e}")
                return ""
        for page in reader.pages:
            text += page.extract_text() or ""
    return text


def load_directory(knowledge_dir: str) -> str:
    """Concatenate all supported docs in a directory into one blob."""
    if not os.path.isdir(knowledge_dir):
        raise FileNotFoundError(f"knowledge_dir not found: {knowledge_dir}")

    all_text = ""
    for name in os.listdir(knowledge_dir):
        path = os.path.join(knowledge_dir, name)
        if not os.path.isfile(path):
            continue
        try:
            if name.endswith(".txt"):
                all_text += _read_txt(path) + "\n\n"
            elif name.endswith(".pdf"):
                all_text += _read_pdf(path) + "\n\n"
            elif name.endswith(".docx"):
                all_text += _read_docx(path) + "\n\n"
        except Exception as e:
            log.error(f"[loader] failed reading {name}: {e}")
    return all_text


def split_text(text: str, chunk_size: int) -> list[str]:
    sections = [s.strip() for s in text.split("---") if s.strip()]
    chunks: list[str] = []
    for section in sections:
        words = section.split()
        if len(words) <= chunk_size:
            chunks.append(section)
        else:
            for i in range(0, len(words), chunk_size):
                chunk = " ".join(words[i : i + chunk_size])
                if chunk.strip():
                    chunks.append(chunk)
    return chunks
