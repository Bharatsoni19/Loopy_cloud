"""
╔══════════════════════════════════════════════════════════════╗
║  LOOPY RAG — 'Ask Loopy' support assistant                   ║
║  Retrieval-Augmented Generation over the project's own docs   ║
╚══════════════════════════════════════════════════════════════╝

Pipeline:  question → retrieve top-k chunks (TF-IDF cosine) → ground a concise
answer in those chunks → return answer + citations.

Generation modes (auto-detected):
  * LOOPY_LLM=bedrock  → Amazon Bedrock (Claude / Titan) for the answer
  * LOOPY_LLM=none     → extractive fallback that stitches retrieved chunks
                         (works offline, no API key — default for demos)

The retriever interface (`Retriever.search`) matches what a managed vector store
(OpenSearch Serverless + Bedrock embeddings) returns, so going from the demo
retriever to a production vector DB is a one-class swap.

Run:
    python ingest.py && uvicorn rag_service:app --port 8200
"""
from __future__ import annotations

import os
import pickle

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel
from sklearn.metrics.pairwise import cosine_similarity

INDEX_PATH = os.getenv("RAG_INDEX", "rag_index.pkl")
LLM_MODE = os.getenv("LOOPY_LLM", "none")
AWS_REGION = os.getenv("AWS_REGION", "ap-south-1")
BEDROCK_MODEL = os.getenv("BEDROCK_MODEL", "anthropic.claude-3-haiku-20240307-v1:0")


class Retriever:
    def __init__(self, path: str):
        if not os.path.exists(path):
            import ingest
            self.index = ingest.build()
        else:
            with open(path, "rb") as f:
                self.index = pickle.load(f)

    def search(self, query: str, k: int = 4) -> list[dict]:
        vec = self.index["vectorizer"]
        qv = vec.transform([query])
        sims = cosine_similarity(qv, self.index["matrix"])[0]
        order = sims.argsort()[::-1][:k]
        return [{"text": self.index["docs"][i],
                 "score": round(float(sims[i]), 3),
                 **self.index["meta"][i]} for i in order if sims[i] > 0.02]


retriever = Retriever(INDEX_PATH)
app = FastAPI(title="Loopy RAG — Ask Loopy", version="1.0")
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


class AskReq(BaseModel):
    question: str
    k: int = 4


def generate(question: str, contexts: list[dict]) -> str:
    context_block = "\n\n".join(f"[{c['source']}] {c['text']}" for c in contexts)
    if LLM_MODE == "bedrock":
        try:
            import json
            import boto3
            prompt = (f"Answer the user's question using only the context.\n\n"
                      f"Context:\n{context_block}\n\nQuestion: {question}\n\nAnswer:")
            body = json.dumps({"anthropic_version": "bedrock-2023-05-31",
                               "max_tokens": 400,
                               "messages": [{"role": "user", "content": prompt}]})
            br = boto3.client("bedrock-runtime", region_name=AWS_REGION)
            out = br.invoke_model(modelId=BEDROCK_MODEL, body=body)
            return json.loads(out["body"].read())["content"][0]["text"].strip()
        except Exception as e:
            return f"(Bedrock unavailable: {e})\n\n" + _extractive(question, contexts)
    return _extractive(question, contexts)


def _extractive(question: str, contexts: list[dict]) -> str:
    if not contexts:
        return ("I couldn't find anything about that in the Loopy docs yet. "
                "Try asking about Loop Cards, recharging, sharing, or the AWS setup.")
    top = contexts[0]["text"]
    sentences = [s.strip() for s in top.replace("\n", " ").split(". ") if s.strip()]
    answer = ". ".join(sentences[:3])
    if not answer.endswith("."):
        answer += "."
    return answer


@app.get("/health")
def health():
    return {"status": "ok", "llm_mode": LLM_MODE,
            "chunks": len(retriever.index["docs"])}


@app.post("/ask")
def ask(req: AskReq):
    contexts = retriever.search(req.question, req.k)
    answer = generate(req.question, contexts)
    return {"question": req.question, "answer": answer,
            "citations": [{"source": c["source"], "score": c["score"]}
                          for c in contexts]}


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("rag_service:app", host="0.0.0.0", port=8200, reload=False)
