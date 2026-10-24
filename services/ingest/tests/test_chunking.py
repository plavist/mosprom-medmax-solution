from app.chunking import chunk_text


def test_empty():
    assert chunk_text("  \n ") == []


def test_short_text_single_chunk():
    assert chunk_text("Привет, мир") == ["Привет, мир"]


def test_long_text_split_with_limit_and_overlap():
    text = " ".join(f"Предложение номер {i}." for i in range(200))
    chunks = chunk_text(text, size=300, overlap=50)
    assert len(chunks) > 5
    assert all(len(c) <= 300 for c in chunks)
    assert "".join(chunks).count("номер 199") >= 1


def test_no_punctuation_hard_split():
    chunks = chunk_text("a" * 2500, size=1000, overlap=100)
    assert all(len(c) <= 1000 for c in chunks) and len(chunks) >= 3


def test_paragraphs_kept():
    chunks = chunk_text("Абзац один.\n\nАбзац два.", size=900)
    assert chunks == ["Абзац один.\nАбзац два."]


def test_zero_overlap():
    chunks = chunk_text("слово. " * 300, size=200, overlap=0)
    assert all(len(c) <= 200 for c in chunks)


def test_long_unit_flushes_current_chunk():
    chunks = chunk_text("Короткий абзац.\n\n" + "b" * 1500, size=500, overlap=50)
    assert chunks[0] == "Короткий абзац." and all(len(c) <= 500 for c in chunks)


def test_overlap_plus_unit_exceeding_size_resets():
    text = ("x" * 280 + ". ") + ("y" * 280 + ".")
    chunks = chunk_text(text, size=300, overlap=100)
    assert all(len(c) <= 300 for c in chunks) and len(chunks) == 2
