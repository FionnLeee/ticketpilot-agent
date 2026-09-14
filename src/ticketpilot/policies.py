import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from ticketpilot.paths import find_ancestor_path
from ticketpilot.schemas import Citation, RequestPrincipal

DEFAULT_POLICY_MANIFEST_PATH = find_ancestor_path(
    Path(__file__), "data", "ticketpilot", "policy_manifest.json"
)


class PolicyRetriever(Protocol):
    async def search(
        self, principal: RequestPrincipal, query: str, limit: int = 3
    ) -> list[Citation]: ...


@dataclass(frozen=True)
class PolicyChunk:
    source_id: str
    chunk_id: str
    title: str
    tenant_ids: frozenset[str]
    keywords: tuple[str, ...]
    content: str


@dataclass(frozen=True)
class PolicyCorpus:
    dataset_id: str
    effective_at: str
    chunks: tuple[PolicyChunk, ...]


def load_policy_corpus(
    manifest_path: Path = DEFAULT_POLICY_MANIFEST_PATH,
) -> PolicyCorpus:
    manifest: dict[str, Any] = json.loads(manifest_path.read_text(encoding="utf-8"))
    corpus_path = manifest_path.parent / manifest["corpus_path"]
    # The manifest hash is taken over LF content so a CRLF checkout (git autocrlf on
    # Windows) still verifies.
    corpus_bytes = corpus_path.read_bytes().replace(b"\r\n", b"\n")
    actual_hash = hashlib.sha256(corpus_bytes).hexdigest()
    if actual_hash != manifest["corpus_sha256"]:
        raise ValueError("Policy corpus hash does not match its manifest")

    payload: dict[str, Any] = json.loads(corpus_bytes)
    if payload["corpus_id"] != manifest["dataset_id"]:
        raise ValueError("Policy corpus id does not match its manifest")
    if len(payload["chunks"]) != manifest["chunk_count"]:
        raise ValueError("Policy corpus chunk count does not match its manifest")

    chunks = tuple(
        PolicyChunk(
            source_id=item["source_id"],
            chunk_id=item["chunk_id"],
            title=item["title"],
            tenant_ids=frozenset(item["tenant_ids"]),
            keywords=tuple(keyword.casefold() for keyword in item["keywords"]),
            content=item["content"],
        )
        for item in payload["chunks"]
    )
    return PolicyCorpus(
        dataset_id=manifest["dataset_id"],
        effective_at=manifest["effective_at"],
        chunks=chunks,
    )


class LocalPolicyRetriever:
    def __init__(self, corpus: PolicyCorpus | None = None) -> None:
        self.corpus = corpus or load_policy_corpus()

    async def search(
        self, principal: RequestPrincipal, query: str, limit: int = 3
    ) -> list[Citation]:
        normalized_query = query.casefold()
        scored_chunks: list[tuple[int, PolicyChunk]] = []
        for chunk in self.corpus.chunks:
            if "*" not in chunk.tenant_ids and principal.tenant_id not in chunk.tenant_ids:
                continue
            score = sum(len(keyword) for keyword in chunk.keywords if keyword in normalized_query)
            if score > 0:
                scored_chunks.append((score, chunk))

        scored_chunks.sort(key=lambda item: (-item[0], item[1].chunk_id))
        return [
            Citation(
                source_id=chunk.source_id,
                title=chunk.title,
                chunk_id=chunk.chunk_id,
                excerpt=chunk.content,
            )
            for _, chunk in scored_chunks[:limit]
        ]
