from myvoice.services.transcript_buffer import TranscriptBuffer, join_chunks


def test_empty_starts_empty():
    b = TranscriptBuffer()
    assert b.text == ""


def test_first_chunk_no_leading_space():
    b = TranscriptBuffer()
    added = b.append("Hello world")
    assert added == "Hello world"
    assert b.text == "Hello world"


def test_second_chunk_gets_glue_space():
    b = TranscriptBuffer()
    b.append("Hello")
    added = b.append("world")
    assert added == " world"
    assert b.text == "Hello world"


def test_punctuation_no_leading_space():
    b = TranscriptBuffer()
    b.append("Hello")
    added = b.append(", world")
    assert added == ", world"
    assert b.text == "Hello, world"


def test_hebrew_unicode_preserved():
    b = TranscriptBuffer()
    b.append("שלום")
    b.append("עולם")
    assert "שלום" in b.text
    assert "עולם" in b.text
    assert b.text == "שלום עולם"


def test_arabic_unicode_preserved():
    b = TranscriptBuffer()
    b.append("مرحبا")
    b.append("بالعالم")
    assert b.text == "مرحبا بالعالم"


def test_whitespace_collapsed():
    b = TranscriptBuffer()
    b.append("  Hello    world  ")
    assert b.text == "Hello world"


def test_join_chunks_helper():
    assert join_chunks("", "hi") == "hi"
    assert join_chunks("hi", "") == "hi"
    assert join_chunks("hi", "there") == "hi there"
    assert join_chunks("hi", "!") == "hi!"
