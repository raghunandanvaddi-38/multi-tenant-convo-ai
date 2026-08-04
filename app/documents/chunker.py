"""
Text chunking — same algorithm as the pre-refactor RAG loader:
split on `---` sections, then fall back to word-count-bounded chunks.
"""

from __future__ import annotations


def chunk_text(text: str, chunk_size: int = 150) -> list[str]:
    if not text or not text.strip():
        return []
    sections = [s.strip() for s in text.split("---") if s.strip()]
    if not sections:
        sections = [text.strip()]
    out: list[str] = []
    for section in sections:
        words = section.split()
        if len(words) <= chunk_size:
            out.append(section)
        else:
            for i in range(0, len(words), chunk_size):
                piece = " ".join(words[i : i + chunk_size])
                if piece.strip():
                    out.append(piece)
    return out
