# reid/__init__.py
"""
reid — Core Re-Identification subsystem package.

Public exports:
    OSNetEmbedder   — Pretrained OSNet inference wrapper
    VisitorRegistry — Global identity store with event publishing
    EventType       — Enumeration of publishable event types
    build_event     — Event dict factory
    cosine_similarity / batch_match — Similarity primitives
    extract_crop    — Frame → PersonCrop extractor
"""

from .embedder   import OSNetEmbedder
from .registry   import VisitorRegistry, ResolveResult, VisitorRecord
from .events     import EventType, EventPublisher, build_event, create_publisher
from .similarity import cosine_similarity, batch_match, l2_normalise
from .crop_utils import extract_crop, PersonCrop

__all__ = [
    "OSNetEmbedder",
    "VisitorRegistry",
    "ResolveResult",
    "VisitorRecord",
    "EventType",
    "EventPublisher",
    "build_event",
    "create_publisher",
    "cosine_similarity",
    "batch_match",
    "l2_normalise",
    "extract_crop",
    "PersonCrop",
]
