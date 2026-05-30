"""
reid/similarity.py
──────────────────
Cosine similarity engine for ReID embedding comparison.

Design Rationale
----------------
Cosine similarity is chosen over Euclidean distance because:

1.  MAGNITUDE INDEPENDENCE — embedding vectors are L2-normalised by
    OSNet, so cosine similarity = dot product, which is numerically
    stable and fast.
2.  ROTATION INVARIANCE — small appearance changes (lighting, camera
    angle) affect vector direction less than magnitude.
3.  THRESHOLD INTERPRETABILITY — cosine scores in [0, 1] are human-
    readable.  0.82 means "82 % directional agreement" in feature
    space.

Threshold Tuning Guide
-----------------------
| Threshold | Behaviour                                          |
|-----------|---------------------------------------------------|
| > 0.90    | Very conservative — only near-identical crops merge |
| 0.82–0.90 | Recommended production range                       |
| 0.75–0.82 | More aggressive matching — small risk of merging   |
|           | people with similar clothing                        |
| < 0.75    | Not recommended — high false-positive rate          |
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import numpy as np

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────
#  Core similarity primitives
# ─────────────────────────────────────────────────────────────────────

def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """
    Compute cosine similarity between two 1-D embedding vectors.

    Both vectors should be L2-normalised (as produced by OSNet).
    For normalised vectors, cosine similarity equals the dot product.

    Parameters
    ----------
    a, b : np.ndarray  — 1-D float32 arrays of equal length

    Returns
    -------
    float : similarity in [0.0, 1.0]  (clipped to handle float noise)
    """
    if a.shape != b.shape:
        raise ValueError(
            f"Embedding shape mismatch: {a.shape} vs {b.shape}"
        )
    # Re-normalise defensively in case embeddings were stored unnormalised
    norm_a = np.linalg.norm(a)
    norm_b = np.linalg.norm(b)
    if norm_a < 1e-8 or norm_b < 1e-8:
        logger.warning("Near-zero norm embedding detected — similarity set to 0")
        return 0.0

    similarity = float(np.dot(a, b) / (norm_a * norm_b))
    return float(np.clip(similarity, 0.0, 1.0))


def cosine_similarity_matrix(
    queries: np.ndarray,
    gallery: np.ndarray,
) -> np.ndarray:
    """
    Vectorised cosine similarity between N query and M gallery embeddings.

    Parameters
    ----------
    queries : np.ndarray  — shape (N, D)
    gallery : np.ndarray  — shape (M, D)

    Returns
    -------
    np.ndarray : shape (N, M) — similarity[i, j] = sim(queries[i], gallery[j])
    """
    # L2 normalise rows
    q_norm = queries / (np.linalg.norm(queries, axis=1, keepdims=True) + 1e-8)
    g_norm = gallery / (np.linalg.norm(gallery, axis=1, keepdims=True) + 1e-8)
    return np.clip(q_norm @ g_norm.T, 0.0, 1.0)


# ─────────────────────────────────────────────────────────────────────
#  Registry matching helpers
# ─────────────────────────────────────────────────────────────────────



@dataclass
class MatchResult:
    """Result of a single registry lookup."""
    matched: bool
    visitor_id: Optional[str]
    similarity: float          # Best cosine score found (0.0 if no match)
    candidate_ids: List[str]   # All candidates above LOW_CONFIDENCE_THRESHOLD


def batch_match(
    query_embedding: np.ndarray,
    registry: Dict[str, np.ndarray],    # {visitor_id: embedding}
    threshold: float,
    low_confidence_threshold: float = 0.65,
) -> MatchResult:
    """
    Find the best matching visitor in the registry for a query embedding.

    Parameters
    ----------
    query_embedding          : 1-D float32 array
    registry                 : mapping of visitor_id → embedding
    threshold                : minimum similarity to declare a match
    low_confidence_threshold : minimum similarity to record as a candidate

    Returns
    -------
    MatchResult with matched=True if best score ≥ threshold
    """
    if not registry:
        return MatchResult(matched=False, visitor_id=None, similarity=0.0, candidate_ids=[])

    visitor_ids = list(registry.keys())
    embeddings  = np.stack([registry[vid] for vid in visitor_ids])  # (M, D)

    # Vectorised similarity
    query_2d = query_embedding.reshape(1, -1)                        # (1, D)
    scores = cosine_similarity_matrix(query_2d, embeddings)[0]      # (M,)

    best_idx   = int(np.argmax(scores))
    best_score = float(scores[best_idx])

    # Candidates above low-confidence floor
    candidate_mask = scores >= low_confidence_threshold
    candidates = [visitor_ids[i] for i in range(len(visitor_ids)) if candidate_mask[i]]

    if best_score >= threshold:
        return MatchResult(
            matched=True,
            visitor_id=visitor_ids[best_idx],
            similarity=best_score,
            candidate_ids=candidates,
        )

    return MatchResult(
        matched=False,
        visitor_id=None,
        similarity=best_score,
        candidate_ids=candidates,
    )


def l2_normalise(embedding: np.ndarray) -> np.ndarray:
    """Return L2-normalised copy of the embedding vector."""
    norm = np.linalg.norm(embedding)
    if norm < 1e-8:
        return embedding
    return embedding / norm
