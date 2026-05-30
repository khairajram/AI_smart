# PROMPT:
# "Write unit tests for a cosine similarity engine used in a person re-identification
#  system. Functions to test: cosine_similarity(a, b) → float in [0,1];
#  cosine_similarity_matrix(queries, gallery) → NxM float matrix;
#  batch_match(query, registry_dict, threshold) → MatchResult with matched/visitor_id/
#  similarity/candidate_ids fields; l2_normalise(v) → unit-norm vector.
#  Cover edge cases: identical vectors → 1.0, orthogonal → 0.0, mismatched
#  shapes raise ValueError, near-zero norm returns 0.0. For batch_match: empty
#  registry, exact match, dissimilar (no match), best-match selection from multiple
#  candidates, low-confidence candidates populated. Use numpy only — no GPU."
#
# CHANGES MADE:
# - Fixed the cosine_similarity orthogonal test: used basis vectors [1,0,0,0] and
#   [0,1,0,0] for guaranteed orthogonality. The LLM used random seeds hoping
#   they'd be near-orthogonal — not guaranteed.
# - Added test for score in unit interval across 10 random seed pairs to catch
#   any implementation that returns values outside [0,1] due to floating point.
# - batch_match low-confidence test: LLM used threshold=0.80 which accidentally
#   matched — changed to threshold=0.95 to ensure below-threshold result.
# - Added test_zero_vector_unchanged for l2_normalise — the LLM missed the
#   zero-vector edge case which would cause a division-by-zero bug.

"""
tests/test_similarity.py
─────────────────────────
Unit tests for the cosine similarity engine.

These tests use only numpy — no GPU or model required.
"""

import numpy as np
import pytest

from reid.similarity import (
    cosine_similarity,
    cosine_similarity_matrix,
    batch_match,
    l2_normalise,
    MatchResult,
)


# ─────────────────────────────────────────────────────────────────────
#  Helpers
# ─────────────────────────────────────────────────────────────────────

def make_embedding(dim: int = 512, seed: int = 0) -> np.ndarray:
    rng = np.random.default_rng(seed)
    v = rng.standard_normal(dim).astype(np.float32)
    return v / (np.linalg.norm(v) + 1e-8)


# ─────────────────────────────────────────────────────────────────────
#  cosine_similarity
# ─────────────────────────────────────────────────────────────────────

class TestCosineSimilarity:

    def test_identical_vectors_score_one(self):
        v = make_embedding(seed=1)
        score = cosine_similarity(v, v)
        assert abs(score - 1.0) < 1e-5

    def test_orthogonal_vectors_score_zero(self):
        a = np.zeros(4, dtype=np.float32); a[0] = 1.0
        b = np.zeros(4, dtype=np.float32); b[1] = 1.0
        score = cosine_similarity(a, b)
        assert abs(score) < 1e-5

    def test_score_in_unit_interval(self):
        for seed in range(10):
            a = make_embedding(seed=seed)
            b = make_embedding(seed=seed + 100)
            score = cosine_similarity(a, b)
            assert 0.0 <= score <= 1.0

    def test_shape_mismatch_raises(self):
        a = make_embedding(512)
        b = make_embedding(256)
        with pytest.raises(ValueError):
            cosine_similarity(a, b)

    def test_near_zero_norm_returns_zero(self):
        a = np.zeros(512, dtype=np.float32)
        b = make_embedding(seed=5)
        score = cosine_similarity(a, b)
        assert score == 0.0


# ─────────────────────────────────────────────────────────────────────
#  cosine_similarity_matrix
# ─────────────────────────────────────────────────────────────────────

class TestCosineSimilarityMatrix:

    def test_output_shape(self):
        queries = np.stack([make_embedding(seed=i) for i in range(3)])
        gallery = np.stack([make_embedding(seed=i + 10) for i in range(5)])
        mat = cosine_similarity_matrix(queries, gallery)
        assert mat.shape == (3, 5)

    def test_diagonal_is_one_when_same(self):
        vecs = np.stack([make_embedding(seed=i) for i in range(4)])
        mat = cosine_similarity_matrix(vecs, vecs)
        diag = np.diag(mat)
        np.testing.assert_allclose(diag, np.ones(4), atol=1e-5)

    def test_values_in_unit_interval(self):
        Q = np.stack([make_embedding(seed=i) for i in range(5)])
        G = np.stack([make_embedding(seed=i + 50) for i in range(7)])
        mat = cosine_similarity_matrix(Q, G)
        assert mat.min() >= 0.0
        assert mat.max() <= 1.0 + 1e-6


# ─────────────────────────────────────────────────────────────────────
#  batch_match
# ─────────────────────────────────────────────────────────────────────

class TestBatchMatch:

    def test_empty_registry_returns_no_match(self):
        q = make_embedding(seed=0)
        result = batch_match(q, {}, threshold=0.82)
        assert result.matched is False
        assert result.visitor_id is None
        assert result.similarity == 0.0

    def test_identical_embedding_matches(self):
        q = make_embedding(seed=42)
        registry = {"visitor_abc": q.copy()}
        result = batch_match(q, registry, threshold=0.82)
        assert result.matched is True
        assert result.visitor_id == "visitor_abc"
        assert result.similarity > 0.99

    def test_dissimilar_embedding_does_not_match(self):
        # Create two near-orthogonal vectors
        a = np.zeros(512, dtype=np.float32); a[0] = 1.0
        b = np.zeros(512, dtype=np.float32); b[256] = 1.0
        registry = {"visitor_xyz": b}
        result = batch_match(a, registry, threshold=0.82)
        assert result.matched is False

    def test_best_match_selected_from_many(self):
        q = make_embedding(seed=99)
        # Add a very similar and a dissimilar entry
        similar  = q * 0.999 + np.random.default_rng(1).standard_normal(512).astype(np.float32) * 0.001
        similar /= np.linalg.norm(similar)
        dissim   = make_embedding(seed=200)
        registry = {"similar": similar, "dissim": dissim}
        result = batch_match(q, registry, threshold=0.80)
        assert result.matched is True
        assert result.visitor_id == "similar"

    def test_low_confidence_candidates_populated(self):
        q = make_embedding(seed=0)
        # Create entry with similarity ~0.70 (above low-conf floor but below threshold)
        # We'll use a blend to get intermediate similarity
        other = make_embedding(seed=1)
        blend = 0.85 * q + 0.15 * other
        blend /= np.linalg.norm(blend)
        registry = {"blended": blend}
        result = batch_match(q, registry, threshold=0.95, low_confidence_threshold=0.50)
        # Should not be a hard match (below 0.95) but should appear as candidate
        assert result.matched is False
        assert len(result.candidate_ids) == 1


# ─────────────────────────────────────────────────────────────────────
#  l2_normalise
# ─────────────────────────────────────────────────────────────────────

class TestL2Normalise:

    def test_output_has_unit_norm(self):
        v = np.array([3.0, 4.0], dtype=np.float32)
        n = l2_normalise(v)
        assert abs(np.linalg.norm(n) - 1.0) < 1e-6

    def test_zero_vector_unchanged(self):
        v = np.zeros(10, dtype=np.float32)
        n = l2_normalise(v)
        assert np.all(n == 0.0)

    def test_already_normalised_unchanged(self):
        v = make_embedding(seed=7)
        n = l2_normalise(v)
        np.testing.assert_allclose(n, v, atol=1e-6)
