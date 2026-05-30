"""
reid/embedder.py
────────────────
OSNet embedding service — the core ReID model wrapper.

Why OSNet?
----------
See CHOICES.md for the full comparison.  In brief:

* OSNet (Omni-Scale Network) was designed specifically for person
  re-identification, learning features at multiple scales simultaneously.
* Pretrained weights on Market-1501 + MSMT17 (>100 K labelled
  identities) provide strong zero-shot generalisation to new retail
  environments without any custom training.
* The x1.0 variant produces 512-d embeddings that balance accuracy and
  inference speed (~5 ms/frame on a mid-range GPU).
* torchreid offers a clean model-zoo API that downloads weights
  automatically on first run.

Architecture Flow
-----------------
  PersonCrop.tensor  →  OSNet forward()  →  L2-normalise  →  512-d vector
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import List, Optional

import numpy as np
import torch

from config.settings import settings
from reid.crop_utils import PersonCrop, batch_crops_to_tensor
from reid.similarity import l2_normalise

logger = logging.getLogger(__name__)

# Lazy import torchreid so the module can be imported without torchreid
# installed (useful for running tests that mock the embedder).
_torchreid = None


def _get_torchreid():
    global _torchreid
    if _torchreid is None:
        try:
            import torchreid
            _torchreid = torchreid
        except ImportError as exc:
            raise RuntimeError(
                "torchreid is not installed.  Run: "
                "pip install git+https://github.com/KaiyangZhou/deep-person-reid.git"
            ) from exc
    return _torchreid


def _resolve_device(preference: str) -> torch.device:
    """Resolve the 'auto' device preference to a concrete torch.device."""
    if preference == "auto":
        if torch.cuda.is_available():
            return torch.device("cuda")
        if hasattr(torch.backends, "mps") and torch.backends.mps.is_available():
            return torch.device("mps")
        return torch.device("cpu")
    return torch.device(preference)


class OSNetEmbedder:
    """
    Wraps a pretrained OSNet model to generate L2-normalised person
    re-identification embeddings from image crops.

    Thread Safety
    -------------
    A single instance should NOT be shared across threads without a lock.
    The pipeline orchestrator creates one embedder per camera thread.

    Usage
    -----
        embedder = OSNetEmbedder()
        embedder.load()

        crops = [PersonCrop(...), ...]
        embeddings = embedder.embed_crops(crops)
        # embeddings is a list of np.ndarray, one per crop
    """

    def __init__(self) -> None:
        self._model: Optional[torch.nn.Module] = None
        self._device: Optional[torch.device] = None
        self._model_name = settings.OSNET_MODEL_NAME
        self._pretrained_dataset = settings.OSNET_PRETRAINED_DATASET
        self._loaded = False

    # ── Lifecycle ────────────────────────────────────────────────────

    def load(self) -> "OSNetEmbedder":
        """
        Download (if needed) and load the pretrained OSNet model into memory.

        The torchreid model-zoo caches weights in ~/.cache/torch/ so
        subsequent runs are instant.

        Returns self for chaining:  embedder = OSNetEmbedder().load()
        """
        if self._loaded:
            return self

        torchreid = _get_torchreid()
        self._device = _resolve_device(settings.REID_DEVICE)

        logger.info(
            "Loading OSNet model '%s' (pretrained on '%s') → device=%s",
            self._model_name,
            self._pretrained_dataset,
            self._device,
        )
        t0 = time.perf_counter()

        # Build model from torchreid model-zoo
        self._model = torchreid.models.build_model(
            name=self._model_name,
            num_classes=1,          # num_classes irrelevant for feature extraction
            pretrained=True,
            use_gpu=self._device.type == "cuda",
        )
        self._model = self._model.to(self._device)
        self._model.eval()

        elapsed = time.perf_counter() - t0
        logger.info("OSNet loaded in %.2f s", elapsed)
        self._loaded = True
        return self

    def unload(self) -> None:
        """Release model from GPU/CPU memory."""
        self._model = None
        self._loaded = False
        if torch.cuda.is_available():
            torch.cuda.empty_cache()
        logger.info("OSNet unloaded")

    # ── Inference ────────────────────────────────────────────────────

    @torch.inference_mode()
    def embed_crops(self, crops: List[PersonCrop]) -> List[np.ndarray]:
        """
        Generate L2-normalised embedding vectors for a batch of person crops.

        Parameters
        ----------
        crops : List[PersonCrop]  — crops from the current frame

        Returns
        -------
        List[np.ndarray] : one 512-d float32 array per crop, in the same order
        """
        if not self._loaded:
            raise RuntimeError("Call OSNetEmbedder.load() before embed_crops()")
        if not crops:
            return []

        # Stack crops into a single batch tensor (N, 3, H, W)
        batch = batch_crops_to_tensor(crops).to(self._device)

        # Forward pass — OSNet returns (N, D) feature tensor
        features: torch.Tensor = self._model(batch)

        # Move to CPU, convert to numpy, L2-normalise each row
        features_np = features.cpu().numpy().astype(np.float32)  # (N, D)
        embeddings = [l2_normalise(features_np[i]) for i in range(len(crops))]

        logger.debug(
            "Embedded %d crops → dim=%d  device=%s",
            len(crops), embeddings[0].shape[0], self._device,
        )
        return embeddings

    @torch.inference_mode()
    def embed_single(self, crop: PersonCrop) -> np.ndarray:
        """Convenience wrapper for a single crop."""
        results = self.embed_crops([crop])
        return results[0]

    # ── Diagnostics ──────────────────────────────────────────────────

    @property
    def is_loaded(self) -> bool:
        return self._loaded

    @property
    def device(self) -> Optional[torch.device]:
        return self._device

    def __repr__(self) -> str:
        return (
            f"OSNetEmbedder(model={self._model_name!r}, "
            f"dataset={self._pretrained_dataset!r}, "
            f"loaded={self._loaded}, device={self._device})"
        )
