"""Provider-neutral, bounded contracts for the local BGE reranker."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

from glodex.retrieval.contracts import RETRIEVAL_MODEL_RERANKER_MODEL

_MAX_CANDIDATES: Final = 40
_MAX_QUERY_CHARACTERS: Final = 512
_MAX_DOCUMENT_CHARACTERS: Final = 2_000


@dataclass(frozen=True, slots=True)
class RerankDocument:
    """One trusted candidate whose membership cannot be changed by reranking."""

    identity: str
    text: str

    def __post_init__(self) -> None:
        if (
            type(self.identity) is not str
            or not self.identity
            or len(self.identity) > 128
            or type(self.text) is not str
            or not self.text.strip()
            or len(self.text) > _MAX_DOCUMENT_CHARACTERS
            or "\0" in self.text
        ):
            raise ValueError("rerank document is invalid")


@dataclass(frozen=True, slots=True)
class RerankRequest:
    """A bounded rerank request for the fixed local cross-encoder."""

    query: str
    documents: tuple[RerankDocument, ...]

    def __post_init__(self) -> None:
        if (
            type(self.query) is not str
            or not self.query.strip()
            or len(self.query) > _MAX_QUERY_CHARACTERS
            or "\0" in self.query
            or type(self.documents) is not tuple
            or not self.documents
            or len(self.documents) > _MAX_CANDIDATES
            or any(type(document) is not RerankDocument for document in self.documents)
        ):
            raise ValueError("rerank request is invalid")
        identities = tuple(document.identity for document in self.documents)
        if len(identities) != len(set(identities)):
            raise ValueError("rerank identities must be unique")


@dataclass(frozen=True, slots=True)
class RerankResult:
    """Identity-only ordered result emitted by the local BGE cross-encoder."""

    identities: tuple[str, ...]
    model: str = RETRIEVAL_MODEL_RERANKER_MODEL

    def __post_init__(self) -> None:
        if (
            self.model != RETRIEVAL_MODEL_RERANKER_MODEL
            or type(self.identities) is not tuple
            or not self.identities
            or len(self.identities) > _MAX_CANDIDATES
            or any(type(identity) is not str or not identity for identity in self.identities)
            or len(self.identities) != len(set(self.identities))
        ):
            raise ValueError("rerank result is invalid")


__all__ = ["RerankDocument", "RerankRequest", "RerankResult"]
