from __future__ import annotations

from autoapply.llm.embeddings import LocalHashEmbedder, content_hash, cosine


def test_embeddings_are_deterministic():
    embedder = LocalHashEmbedder(dim=256)
    assert embedder.embed("python backend engineer") == embedder.embed("python backend engineer")


def test_vectors_are_unit_length():
    vector = LocalHashEmbedder(dim=256).embed("distributed systems in python")
    assert abs(sum(v * v for v in vector) ** 0.5 - 1.0) < 1e-9


def test_related_text_scores_above_unrelated_text():
    embedder = LocalHashEmbedder(dim=1024)
    job = embedder.embed("Backend engineer building Python services on Postgres and Redis")
    close = embedder.embed("Built Python backend services backed by Postgres")
    far = embedder.embed("Pastry chef specialising in laminated dough and viennoiserie")
    assert cosine(job, close) > cosine(job, far)


def test_empty_text_is_the_zero_vector():
    assert set(LocalHashEmbedder(dim=64).embed("")) == {0.0}


def test_content_hash_is_case_and_whitespace_insensitive():
    assert content_hash("  Backend Engineer  ") == content_hash("backend engineer")
    assert content_hash("a") != content_hash("b")


def test_cosine_handles_zero_vectors():
    assert cosine([0.0, 0.0], [1.0, 0.0]) == 0.0
