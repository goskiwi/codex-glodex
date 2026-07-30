"""Python 3.11-compatible public protocol shared by the M2c app and GPU service."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

M2C_ENDPOINT: Final = "http://127.0.0.1:18000"
M2C_SERVICE_SCHEMA: Final = "glodex.m2c-model-service.v1"
M2C_EMBEDDING_MODEL: Final = "glodex-bge-m3-v1"
M2C_RERANKER_MODEL: Final = "glodex-bge-reranker-v1"
M2C_DIMENSION: Final = 1_024
M2C_MAX_EMBEDDING_TEXTS: Final = 8
M2C_MAX_TEXT_CHARACTERS: Final = 2_000
M2C_MAX_RERANK_DOCUMENTS: Final = 40
M2C_MAX_QUERY_CHARACTERS: Final = 512

_DIGEST = re.compile(r"[0-9a-f]{64}\Z")


@dataclass(frozen=True, slots=True)
class M2cModelIdentity:
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
    schema_version: str = M2C_SERVICE_SCHEMA

    def __post_init__(self) -> None:
        if (
            self.schema_version != M2C_SERVICE_SCHEMA
            or type(self.manifest_digest) is not str
            or _DIGEST.fullmatch(self.manifest_digest) is None
            or self.embedding_model != M2C_EMBEDDING_MODEL
            or self.reranker_model != M2C_RERANKER_MODEL
            or self.dimension != M2C_DIMENSION
            or self.max_embedding_texts != M2C_MAX_EMBEDDING_TEXTS
            or self.max_text_characters != M2C_MAX_TEXT_CHARACTERS
            or self.max_rerank_documents != M2C_MAX_RERANK_DOCUMENTS
            or self.max_query_characters != M2C_MAX_QUERY_CHARACTERS
            or self.device_class != "cuda"
            or self.gpu_model_class != "a100"
        ):
            raise ValueError("M2c model identity is invalid")


__all__ = [
    "M2C_DIMENSION",
    "M2C_EMBEDDING_MODEL",
    "M2C_ENDPOINT",
    "M2C_MAX_EMBEDDING_TEXTS",
    "M2C_MAX_QUERY_CHARACTERS",
    "M2C_MAX_RERANK_DOCUMENTS",
    "M2C_MAX_TEXT_CHARACTERS",
    "M2C_RERANKER_MODEL",
    "M2C_SERVICE_SCHEMA",
    "M2cModelIdentity",
]
