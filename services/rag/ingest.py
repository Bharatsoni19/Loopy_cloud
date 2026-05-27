"""
Loopy RAG — ingestion.

Builds a retrieval index from the project's own documentation + a small FinTech
knowledge base. We use a dependency-light TF-IDF + cosine retriever so the
service runs with zero API keys or GPUs (important for a student demo and for
keeping the EC2 instance cheap). The retriever interface is deliberately the
same shape you'd get from a vector DB, so swapping in Amazon Bedrock Titan
embeddings + OpenSearch later is a drop-in change (see rag_service.py).
"""
from __future__ import annotations

import glob
import json
import os
import pickle
import re

from sklearn.feature_extraction.text import TfidfVectorizer

INDEX_PATH = os.getenv("RAG_INDEX", "rag_index.pkl")
DOCS_GLOBS = ["knowledge/*.md", "knowledge/*.txt", "../../docs/*.md", "../../*.md"]


def chunk(text: str, size: int = 600, overlap: int = 100) -> list[str]:
    """Split on paragraph boundaries, then pack into ~size-char windows."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    chunks, buf = [], ""
    for p in paras:
        if len(buf) + len(p) > size and buf:
            chunks.append(buf.strip())
            buf = buf[-overlap:]
        buf += "\n" + p
    if buf.strip():
        chunks.append(buf.strip())
    return chunks


def build() -> dict:
    docs, meta = [], []
    seen = set()
    for pattern in DOCS_GLOBS:
        for path in glob.glob(pattern):
            rp = os.path.realpath(path)
            if rp in seen or not os.path.isfile(path):
                continue
            seen.add(rp)
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            for i, ch in enumerate(chunk(text)):
                docs.append(ch)
                meta.append({"source": os.path.basename(path), "chunk": i})

    if not docs:                       # always have a tiny built-in KB
        docs = list(_FALLBACK_KB.values())
        meta = [{"source": k, "chunk": 0} for k in _FALLBACK_KB]

    vec = TfidfVectorizer(stop_words="english", ngram_range=(1, 2), max_features=8000)
    matrix = vec.fit_transform(docs)
    index = {"vectorizer": vec, "matrix": matrix, "docs": docs, "meta": meta}
    with open(INDEX_PATH, "wb") as f:
        pickle.dump(index, f)
    print(json.dumps({"indexed_chunks": len(docs),
                      "sources": sorted({m['source'] for m in meta})}))
    return index


# Minimal seed knowledge so the assistant is useful even before docs are mounted.
_FALLBACK_KB = {
    "loop-cards.md": (
        "A Loop Card is a prepaid, shareable music-time card in Loopy. "
        "Each recharge costs 120 Loopy Coins and grants 60 minutes of premium "
        "listening. Cards can be shared with friends, who can spend the minutes. "
        "Minutes can also be transferred peer-to-peer between cards."),
    "recharge.md": (
        "To recharge a Loop Card, open the Loop Cards tab, pick a card and tap "
        "Recharge +60 min. Coins are debited and a 60-minute premium pass starts. "
        "Recharges are idempotent: a retried request never double-charges."),
    "security.md": (
        "Loopy Pay uses JWT bearer tokens (HS256) for authentication, idempotency "
        "keys to prevent double-spend, a token-bucket rate limiter for throttling, "
        "and a double-entry ledger so minutes and coins can never be minted or lost."),
    "cloud.md": (
        "Loopy runs on AWS: the FastAPI services run on EC2 behind an nginx gateway, "
        "payment events sync to an S3 raw bucket, AWS Glue crawls and transforms them "
        "into curated Parquet for Athena analytics, and CloudWatch collects logs and "
        "metrics. Infrastructure is provisioned with Terraform and shipped via a "
        "GitHub Actions CI/CD pipeline with a DevSecOps scanning stage."),
}


if __name__ == "__main__":
    build()
