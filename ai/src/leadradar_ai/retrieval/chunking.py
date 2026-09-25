"""Chunking (SPEC AI-03): snippets of 120-220 words with a one-sentence overlap.

Chunk text is an exact slice of document.text (char_start/char_end), so verified quotes map back to the
document. The title prefix for news and jobs is added only to the text that is embedded and searched
(`index_text`), never to the stored slice.
"""

import re
from uuid import UUID, uuid4

from leadradar_ai.contracts import AnalysisDocument, ChunkIn, SourceType

MIN_WORDS = 120
MAX_WORDS = 220
MIN_TAIL_WORDS = 60  # a shorter tail is merged into the previous chunk
TITLE_PREFIXED: frozenset[SourceType] = frozenset({"news", "jobs"})

_WORD = re.compile(r"\S+")
# sentence end: . ! ? … (optionally followed by quotes/brackets) + whitespace, or a line break
_SENTENCE_END = re.compile(r"(?<=[.!?…])[\"'»”)\]]*\s+|\n+")


def index_text(text: str, title: str | None, source_type: SourceType) -> str:
    if title and source_type in TITLE_PREFIXED and not text.startswith(title):
        return f"{title}\n{text}"
    return text


def _sentences(text: str) -> list[tuple[int, int, int]]:
    """(start, end, word count) of each sentence, without surrounding whitespace."""
    spans = []
    pos = 0
    for m in _SENTENCE_END.finditer(text):
        spans.append((pos, m.start()))
        pos = m.end()
    spans.append((pos, len(text)))
    out = []
    for start, end in spans:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        if start < end:
            out.append((start, end, len(_WORD.findall(text[start:end]))))
    return out


def _split_long(text: str, start: int, end: int) -> list[tuple[int, int, int]]:
    """A sentence longer than MAX_WORDS (tables, lists without punctuation) is cut by words."""
    words = list(_WORD.finditer(text, start, end))
    parts = []
    for i in range(0, len(words), MAX_WORDS):
        group = words[i : i + MAX_WORDS]
        parts.append((group[0].start(), group[-1].end(), len(group)))
    return parts


def split_spans(text: str) -> list[tuple[int, int]]:
    sentences = []
    for s in _sentences(text):
        sentences.extend(_split_long(text, s[0], s[1]) if s[2] > MAX_WORDS else [s])
    if not sentences:
        return []

    chunks: list[list[tuple[int, int, int]]] = []
    current: list[tuple[int, int, int]] = []
    words = 0
    for sentence in sentences:
        if current and words >= MIN_WORDS and words + sentence[2] > MAX_WORDS:
            chunks.append(current)
            overlap = current[-1]
            current = [overlap] if overlap[2] + sentence[2] <= MAX_WORDS else []
            words = sum(s[2] for s in current)
        current.append(sentence)
        words += sentence[2]
    if current:
        overlap_only = chunks and len(current) == 1 and current[0] == chunks[-1][-1]
        if not overlap_only:
            if chunks and words < MIN_TAIL_WORDS:
                chunks[-1].extend(s for s in current if s not in chunks[-1])
            else:
                chunks.append(current)
    return [(c[0][0], c[-1][1]) for c in chunks]


def chunk_document(doc: AnalysisDocument, company_id: UUID) -> list[ChunkIn]:
    """Chunks without embeddings (empty list); the index node fills them in batches."""
    return [
        ChunkIn(
            id=uuid4(),
            document_id=doc.id,
            company_id=company_id,
            ord=i,
            text=doc.text[start:end],
            char_start=start,
            char_end=end,
            embedding=[],
        )
        for i, (start, end) in enumerate(split_spans(doc.text))
    ]
