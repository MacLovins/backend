import itertools
import re
from uuid import uuid4

import pytest
from leadradar_ai.retrieval import chunk_document, index_text
from leadradar_ai.retrieval.chunking import MAX_WORDS, MIN_WORDS, split_spans
from leadradar_ai.testing.factories import make_document


def words(text: str) -> int:
    return len(text.split())


def sentence(i: int, n: int = 10) -> str:
    return " ".join([f"w{i}"] * (n - 1)) + f" end{i}."


def test_short_document_is_one_chunk_with_exact_offsets():
    text = "  DHL expands agentic AI. It processes RFQs automatically.  "
    doc = make_document(text=text)
    chunks = chunk_document(doc, uuid4())
    assert len(chunks) == 1
    c = chunks[0]
    assert c.text == "DHL expands agentic AI. It processes RFQs automatically."
    assert text[c.char_start : c.char_end] == c.text
    assert (c.ord, c.document_id, c.embedding) == (0, doc.id, [])


def test_long_document_chunks_are_sized_and_overlap_by_one_sentence():
    text = " ".join(sentence(i) for i in range(60))  # 600 words
    chunks = chunk_document(make_document(text=text), uuid4())
    assert len(chunks) >= 3
    for c in chunks:
        assert text[c.char_start : c.char_end] == c.text
        assert MIN_WORDS <= words(c.text) <= MAX_WORDS + 60
    for prev, nxt in itertools.pairwise(chunks):
        last_sentence = re.findall(r"[^.]+\.", prev.text)[-1].strip()
        assert nxt.text.startswith(last_sentence)
    # every sentence is covered
    covered = " ".join(c.text for c in chunks)
    assert all(f"end{i}." in covered for i in range(60))
    assert [c.ord for c in chunks] == list(range(len(chunks)))


def test_small_tail_is_merged_into_previous_chunk():
    text = " ".join(sentence(i) for i in range(22)) + " " + sentence(99, n=5)  # 220 + 5 words
    spans = split_spans(text)
    assert text[spans[-1][0] : spans[-1][1]].endswith("end99.")
    assert all(words(text[s:e]) >= MIN_WORDS for s, e in spans)


def test_sentence_without_punctuation_is_cut_by_words():
    text = " ".join(f"token{i}" for i in range(500))
    chunks = chunk_document(make_document(text=text), uuid4())
    assert all(words(c.text) <= MAX_WORDS for c in chunks)
    assert chunks[0].text.startswith("token0 ") and chunks[-1].text.endswith("token499")


def test_line_breaks_split_sentences():
    text = "\n".join(f"Bullet point number {i} without a full stop" for i in range(40))
    spans = split_spans(text)
    assert len(spans) >= 2
    assert all(text[s:e] == text[s:e].strip() for s, e in spans)


@pytest.mark.parametrize("text", ["", "   \n  "])
def test_empty_document_has_no_chunks(text):
    assert chunk_document(make_document(text=text), uuid4()) == []


def test_title_prefix_only_for_news_and_jobs():
    assert index_text("Body.", "Headline", "news") == "Headline\nBody."
    assert index_text("Body.", "RPA Developer", "jobs") == "RPA Developer\nBody."
    assert index_text("Body.", "About us", "website") == "Body."
    assert index_text("Headline and body.", "Headline", "news") == "Headline and body."
    assert index_text("Body.", None, "news") == "Body."
