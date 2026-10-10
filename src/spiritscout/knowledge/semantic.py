"""知识库语义检索：Chroma 向量库 + 可配置 embedding（3.5：Chroma 用于语义召回）。

embedding 两种 provider（KnowledgeConfig）：
- ``hash``：本地确定性哈希嵌入（ASCII 词 + CJK 二元组的签名哈希投影），
  离线可用、零外部依赖 —— 工程权衡 默认不依赖任何云端/本地模型服务，
  语义能力弱于真实 embedding 模型，仅作召回增强；
- ``openai_compatible``：OpenAI 兼容 ``/embeddings`` 接口（base_url+api_key+
  model 配置即接入云端推理，与系统模型接入约束一致）。

Chroma 为可选依赖（pip install spiritscout[chroma]）；未安装时语义检索显式
报 ConfigurationError 并给出安装指引（不静默降级为关键词检索）。
"""

from __future__ import annotations

import hashlib
import math
import re
from typing import Any

from spiritscout.config.settings import KnowledgeConfig
from spiritscout.core.exceptions import ConfigurationError

_TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.\-]*")
_CJK_RUN_RE = re.compile(r"[一-鿿]+")


def _tokens(text: str) -> list[str]:
    tokens = _TOKEN_RE.findall(text.lower())
    for run in _CJK_RUN_RE.findall(text):
        chars = list(run)
        if len(chars) == 1:
            tokens.append(chars[0])
        else:
            tokens.extend("".join(chars[i:i + 2]) for i in range(len(chars) - 1))
    return tokens


class HashEmbedding:
    """确定性签名哈希投影嵌入（离线默认可用）。"""

    def __init__(self, dim: int = 512):
        self.dim = dim

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._embed_one(t) for t in texts]

    def _embed_one(self, text: str) -> list[float]:
        vec = [0.0] * self.dim
        for token in _tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] & 1 else -1.0
            vec[bucket] += sign
        norm = math.sqrt(sum(x * x for x in vec))
        if norm == 0.0:   # 空文本 → 零向量（Chroma 可存但相似度无意义）
            return vec
        return [x / norm for x in vec]


class OpenAICompatibleEmbedding:
    """OpenAI 兼容 /embeddings 接口（云端推理，base_url+api_key+model）。"""

    def __init__(self, base_url: str, api_key: str, model: str,
                 *, timeout: float = 30.0):
        if not base_url or not model:
            raise ConfigurationError(
                "embedding_provider=openai_compatible 需要配置 "
                "embedding_base_url 与 embedding_model")
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.timeout = timeout

    def _post_embeddings(self, texts: list[str]) -> Any:
        import httpx

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        resp = httpx.post(
            f"{self.base_url}/embeddings",
            json={"model": self.model, "input": texts},
            headers=headers, timeout=self.timeout,
        )
        resp.raise_for_status()
        return resp.json()

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        data = self._post_embeddings(texts)
        try:
            items = sorted(data["data"], key=lambda d: d["index"])
            return [list(map(float, d["embedding"])) for d in items]
        except (KeyError, TypeError, ValueError) as exc:
            raise ConfigurationError(
                f"embedding 接口返回格式不符 OpenAI 规范: {exc}") from exc


def build_embedding(cfg: KnowledgeConfig) -> HashEmbedding | OpenAICompatibleEmbedding:
    if cfg.embedding_provider == "openai_compatible":
        return OpenAICompatibleEmbedding(
            cfg.embedding_base_url, cfg.embedding_api_key, cfg.embedding_model)
    return HashEmbedding()


class SemanticIndex:
    """Chroma 语义索引（与 FTS 共用 store.iter_search_docs() 投影）。"""

    COLLECTION = "spiritscout_kb"

    def __init__(self, cfg: KnowledgeConfig, *, client: Any = None):
        try:
            import chromadb
        except ImportError as exc:
            raise ConfigurationError(
                "语义检索需要可选依赖 chromadb（未安装）。"
                "请执行 pip install 'spiritscout[chroma]' 后重试；"
                "或仅使用 KnowledgeStore.search 关键词检索（无需该依赖）"
            ) from exc
        self.embedding = build_embedding(cfg)
        self._client = client or chromadb.PersistentClient(path=cfg.chroma_path)
        self._col = self._client.get_or_create_collection(
            self.COLLECTION, metadata={"hnsw:space": "cosine"})

    @staticmethod
    def available() -> bool:
        try:
            import chromadb  # noqa: F401
        except ImportError:
            return False
        return True

    def index_docs(self, docs: list[dict[str, str]], *, batch: int = 128) -> int:
        """把检索文档写入向量库（幂等 upsert），返回写入条数。"""
        count = 0
        for i in range(0, len(docs), batch):
            chunk = docs[i:i + batch]
            ids = [f"{d['base']}::{d['entry_key']}" for d in chunk]
            texts = [d["content"] for d in chunk]
            self._col.upsert(
                ids=ids,
                embeddings=self.embedding.embed(texts),
                documents=texts,
                metadatas=[{"base": d["base"], "entry_key": d["entry_key"],
                            "title": d["title"]} for d in chunk],
            )
            count += len(chunk)
        return count

    def search(self, query: str, *, bases: list[str] | None = None,
               limit: int = 10) -> list[dict[str, Any]]:
        """语义召回：返回 [{base, entry_key, title, distance, content}]。"""
        if not query.strip():
            return []
        where = {"base": {"$in": bases}} if bases else None
        result = self._col.query(
            query_embeddings=self.embedding.embed([query]),
            n_results=max(limit, 1),
            where=where,
            include=["documents", "metadatas", "distances"],
        )
        out: list[dict[str, Any]] = []
        ids = result.get("ids") or [[]]
        metas = result.get("metadatas") or [[]]
        docs = result.get("documents") or [[]]
        dists = result.get("distances") or [[]]
        for meta, doc, dist in zip(metas[0], docs[0], dists[0], strict=False):
            out.append({"base": meta["base"], "entry_key": meta["entry_key"],
                        "title": meta["title"], "distance": dist,
                        "content": doc})
        _ = ids
        return out

    def count(self) -> int:
        return int(self._col.count())


def index_store(store: Any, semantic: SemanticIndex) -> int:
    """把 KnowledgeStore 全量检索文档灌入语义索引（种子重建后调用）。"""
    return semantic.index_docs(store.iter_search_docs())
