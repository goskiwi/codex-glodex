"""Python 3.11-compatible public protocol shared by the RetrievalModel app and GPU service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

RETRIEVAL_MODEL_ENDPOINT: Final = "http://127.0.0.1:18000"
RETRIEVAL_MODEL_SERVICE_SCHEMA: Final = "glodex.retrieval-model-service.v1"
RETRIEVAL_MODEL_EMBEDDING_MODEL: Final = "glodex-bge-m3-v1"
RETRIEVAL_MODEL_RERANKER_MODEL: Final = "glodex-bge-reranker-v1"
RETRIEVAL_MODEL_DIMENSION: Final = 1_024
RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS: Final = 8
RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS: Final = 2_000
RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS: Final = 50
RETRIEVAL_MODEL_MAX_QUERY_CHARACTERS: Final = 512

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class RetrievalModelIdentity:
    """The safe public identity derived from the GPU-private model manifest."""

    manifest_digest: str
    embedding_model: str
    reranker_model: str
    dimension: int
    max_embedding_texts: int
    max_text_characters: int
    max_rerank_documents: int
    max_query_characters: int
    device_class: str
    gpu_model_class: str
    schema_version: str = RETRIEVAL_MODEL_SERVICE_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != RETRIEVAL_MODEL_SERVICE_SCHEMA
            or type(self.manifest_digest) is not str
            or _DIGEST.fullmatch(self.manifest_digest) is None
            or self.embedding_model != RETRIEVAL_MODEL_EMBEDDING_MODEL
            or self.reranker_model != RETRIEVAL_MODEL_RERANKER_MODEL
            or self.dimension != RETRIEVAL_MODEL_DIMENSION
            or self.max_embedding_texts != RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS
            or self.max_text_characters != RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS
            or self.max_rerank_documents != RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS
            or self.max_query_characters != RETRIEVAL_MODEL_MAX_QUERY_CHARACTERS
            or self.device_class != "cuda"
            or self.gpu_model_class != "a100"
        ):
            raise ValueError("retrieval model identity is invalid")


__all__ = [
    "RETRIEVAL_MODEL_DIMENSION",
    "RETRIEVAL_MODEL_EMBEDDING_MODEL",
    "RETRIEVAL_MODEL_ENDPOINT",
    "RETRIEVAL_MODEL_MAX_EMBEDDING_TEXTS",
    "RETRIEVAL_MODEL_MAX_QUERY_CHARACTERS",
    "RETRIEVAL_MODEL_MAX_RERANK_DOCUMENTS",
    "RETRIEVAL_MODEL_MAX_TEXT_CHARACTERS",
    "RETRIEVAL_MODEL_RERANKER_MODEL",
    "RETRIEVAL_MODEL_SERVICE_SCHEMA",
    "RetrievalModelIdentity",
]
