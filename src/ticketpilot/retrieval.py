import asyncio
import hashlib
import json
import math
import re
from collections import Counter
from datetime import datetime
from pathlib import Path
from threading import BoundedSemaphore, Lock
from typing import Any, Literal, Protocol

import numpy as np

from ticketpilot.policies import PolicyCorpus, applicable_chunks, policy_citation
from ticketpilot.schemas import Citation, RequestPrincipal

Strategy = Literal["keyword", "bm25", "dense", "hybrid", "rerank"]


def tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[a-z0-9]+", text.casefold())
    for phrase in re.findall(r"[\u4e00-\u9fff]+", text):
        tokens.extend(phrase[i : i + 2] for i in range(len(phrase) - 1))
        if len(phrase) == 1:
            tokens.append(phrase)
    return tokens


class EmbeddingBackend(Protocol):
    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray: ...


class RerankerBackend(Protocol):
    def score(self, query: str, texts: list[str]) -> list[float]: ...


class FastEmbedBackend:
    model_name = "BAAI/bge-small-zh-v1.5"

    def __init__(self, cache_dir: str | None = None) -> None:
        from fastembed import TextEmbedding

        self.model = TextEmbedding(self.model_name, cache_dir=cache_dir, threads=2)
        self.lock = Lock()

    def embed(self, texts: list[str], *, query: bool = False) -> np.ndarray:
        if query:
            texts = ["为这个句子生成表示以用于检索相关文章：" + text for text in texts]
        with self.lock:
            vectors = np.array(list(self.model.embed(texts, batch_size=32)), dtype=np.float32)
        return vectors / np.maximum(np.linalg.norm(vectors, axis=1, keepdims=True), 1e-12)


class FastEmbedReranker:
    model_name = "BAAI/bge-reranker-base"

    def __init__(self, cache_dir: str | None = None) -> None:
        from fastembed.rerank.cross_encoder import TextCrossEncoder

        self.model = TextCrossEncoder(self.model_name, cache_dir=cache_dir, threads=2)
        self.lock = Lock()

    def score(self, query: str, texts: list[str]) -> list[float]:
        with self.lock:
            return [float(value) for value in self.model.rerank(query, texts)]


class PolicySearchIndex:
    def __init__(
        self,
        corpus: PolicyCorpus,
        embedding: EmbeddingBackend | None = None,
        reranker: RerankerBackend | None = None,
    ) -> None:
        self.corpus = corpus
        self.embedding = embedding
        self.reranker = reranker
        self.positions = {chunk.chunk_id: index for index, chunk in enumerate(corpus.chunks)}
        self.texts = [f"{chunk.title}。{chunk.content}" for chunk in corpus.chunks]
        self.terms = [Counter(tokenize(text)) for text in self.texts]
        self.vectors: np.ndarray | None = None
        self.fingerprint = hashlib.sha256(
            json.dumps(
                [vars(chunk) | {"tenant_ids": sorted(chunk.tenant_ids)} for chunk in corpus.chunks],
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()

    def warm(self) -> None:
        if self.embedding is not None and self.vectors is None:
            self.vectors = self.embedding.embed(self.texts)

    def search(
        self,
        principal: RequestPrincipal,
        query: str,
        *,
        strategy: Strategy = "hybrid",
        limit: int = 3,
        as_of: datetime | None = None,
        min_similarity: float = 0.50,
        candidate_limit: int = 12,
    ) -> list[Citation]:
        if not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        chunks = applicable_chunks(self.corpus, principal, as_of)
        if not chunks:
            return []
        indices = [self.positions[chunk.chunk_id] for chunk in chunks]
        if strategy == "keyword":
            ranked = sorted(
                (
                    (sum(len(k) for k in c.keywords if k in query.casefold()), c.chunk_id, c)
                    for c in chunks
                ),
                key=lambda item: (-item[0], item[1]),
            )
            return [policy_citation(c) for score, _, c in ranked[:limit] if score > 0]
        terms = set(tokenize(query))
        lengths = {i: sum(self.terms[i].values()) for i in indices}
        average = max(sum(lengths.values()) / len(indices), 1)
        lexical = {i: 0.0 for i in indices}
        for term in terms:
            df = sum(term in self.terms[i] for i in indices)
            idf = math.log(1 + (len(indices) - df + 0.5) / (df + 0.5))
            for i in indices:
                tf = self.terms[i][term]
                lexical[i] += idf * tf * 2.5 / (tf + 1.5 * (0.25 + 0.75 * lengths[i] / average))
        sparse_order = sorted(
            (i for i in indices if lexical[i] > 0), key=lambda i: (-lexical[i], i)
        )
        if strategy == "bm25":
            return [policy_citation(self.corpus.chunks[i]) for i in sparse_order[:limit]]
        if self.embedding is None or self.vectors is None:
            raise RuntimeError("Dense retrieval requires a warmed embedding index")
        vector = self.embedding.embed([query], query=True)[0]
        similarities = {i: float(self.vectors[i] @ vector) for i in indices}
        dense_order = sorted(indices, key=lambda i: (-similarities[i], i))
        if similarities[dense_order[0]] < min_similarity:
            return []
        if strategy == "dense":
            selected = dense_order[:limit]
        else:
            fused: dict[int, float] = {}
            for ranking in (sparse_order[:candidate_limit], dense_order[:candidate_limit]):
                for rank, index in enumerate(ranking, 1):
                    fused[index] = fused.get(index, 0.0) + 1 / (60 + rank)
            selected = sorted(fused, key=lambda i: (-fused[i], i))[:candidate_limit]
            if strategy == "rerank":
                if self.reranker is None:
                    raise RuntimeError("Rerank strategy requires a cross encoder")
                scores = self.reranker.score(query, [self.texts[i] for i in selected])
                selected = [
                    i for _, i in sorted(zip(scores, selected), key=lambda p: (-p[0], p[1]))
                ]
        return [policy_citation(self.corpus.chunks[i]) for i in selected[:limit]]


class HybridPolicyRetriever:
    def __init__(
        self, index: PolicySearchIndex, strategy: Strategy = "hybrid", min_similarity: float = 0.50
    ) -> None:
        self.index = index
        self.strategy = strategy
        self.min_similarity = min_similarity
        self.slots = BoundedSemaphore(2)

    async def search(
        self,
        principal: RequestPrincipal,
        query: str,
        limit: int = 5,
        *,
        as_of: datetime | None = None,
    ) -> list[Citation]:
        if not self.slots.acquire(blocking=False):
            raise TimeoutError("retrieval_capacity")

        def search_bounded():
            try:
                return self.index.search(
                    principal,
                    query,
                    strategy=self.strategy,
                    limit=limit,
                    min_similarity=self.min_similarity,
                    as_of=as_of,
                )
            finally:
                self.slots.release()

        # Cancellation cannot stop ONNX work; keep its permit until the worker actually exits.
        return await asyncio.shield(asyncio.to_thread(search_bounded))

    def metadata(self) -> dict[str, Any]:
        return {
            "retrieval_strategy": self.strategy,
            "corpus_id": self.index.corpus.dataset_id,
            "corpus_fingerprint": self.index.fingerprint,
            "embedding_model": getattr(self.index.embedding, "model_name", None),
            "reranker_model": getattr(self.index.reranker, "model_name", None),
        }


def build_policy_retriever(
    manifest_path: Path,
    strategy: Strategy,
    cache_dir: str | None = None,
    min_similarity: float = 0.50,
) -> HybridPolicyRetriever:
    from ticketpilot.policies import load_policy_corpus

    embedding = FastEmbedBackend(cache_dir) if strategy in {"dense", "hybrid", "rerank"} else None
    reranker = FastEmbedReranker(cache_dir) if strategy == "rerank" else None
    index = PolicySearchIndex(load_policy_corpus(manifest_path), embedding, reranker)
    index.warm()
    return HybridPolicyRetriever(index, strategy, min_similarity)
