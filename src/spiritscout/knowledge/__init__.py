"""知识库：五库 + 两经验库（设计书 3.5）。

- 结构化：SQLite（``KnowledgeStore``），FTS5 跨库关键词检索；
- 语义：Chroma（``SemanticIndex``，可选依赖），embedding 可配置
  （hash 本地默认 / openai_compatible 云端）；
- 种子数据：``seed.py``（设计书第三章 + 附录B/C 原文落地，条目级来源标注）。
"""

from spiritscout.knowledge.seed import SEED_VERSION
from spiritscout.knowledge.semantic import (
    HashEmbedding,
    OpenAICompatibleEmbedding,
    SemanticIndex,
    index_store,
)
from spiritscout.knowledge.store import BASES, KnowledgeStore, fts_terms

__all__ = [
    "BASES",
    "SEED_VERSION",
    "HashEmbedding",
    "KnowledgeStore",
    "OpenAICompatibleEmbedding",
    "SemanticIndex",
    "fts_terms",
    "index_store",
]
