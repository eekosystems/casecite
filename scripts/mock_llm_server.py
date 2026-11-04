#!/usr/bin/env python3
"""
A stand-in for the AI provider, so the full product can be exercised offline.

Serves the two OpenAI-compatible endpoints CaseCite calls — chat completions
and embeddings — with deterministic answers, so a browser can upload a real
document, see it indexed, ask a question and get an answer with verified
citations without any provider key or network. Used by the end-to-end
journey tests in CI (frontend/e2e/journeys) and handy for local runs:

    python scripts/mock_llm_server.py --port 8089
    OPENAI_BASE_URL=http://127.0.0.1:8089/v1 OPENAI_API_KEY=mock \\
    EMBEDDING_BASE_URL=http://127.0.0.1:8089/v1 EMBEDDING_MODEL=hash-768 EMBEDDING_DIMENSIONS=768 \\
    uvicorn app.main:app

Embeddings are the benchmark's feature-hashed vectors (backend/evals/
hash_embeddings.py): similarity tracks shared vocabulary, which is enough for
a document's own passages to be retrieved for a question about it.

Chat completions are answered by reading the prompt. The pipeline wraps
third-party text as ``<<<BEGIN DOCUMENT: <label>>>> ... <<<END DOCUMENT>>>``
(app/services/rag/prompt_safety.py), and its utility prompts ask for strict
JSON of a known shape. The server quotes a sentence from the document blocks
as the answer, lists that sentence as the answer's one claim when asked to
ground the answer, and labels it supported when asked to check support — so
the real pipeline (verbatim verification, support labels, citation badges,
usage metering) runs end to end on real data paths. It is a plumbing
stand-in, not a model: nothing here measures answer quality.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from evals.hash_embeddings import DIMENSIONS, embed  # noqa: E402

_SENTENCE = re.compile(r"[^.!?\n]{40,400}[.!?]")
# The grounding prompt labels a block "f0: <filename>"; the answer prompt
# labels it with the document's name. Everything outside the blocks is
# instruction text and must never be quoted.
_BLOCK = re.compile(r"<<<BEGIN DOCUMENT: ([^>]*)>>>\n(.*?)\n<<<END DOCUMENT>>>", re.S)
_QUESTION_LINE = re.compile(r"\s*(query|question|user question|answer)\s*:", re.I)
_LEGAL_WORDS = re.compile(r"\b(shall|agree|party|days|term)\b", re.I)


def _long_sentences(text: str) -> list[str]:
    return [m.group(0).strip() for m in _SENTENCE.finditer(text) if "<<<" not in m.group(0)]


def _document_blocks(prompt: str) -> list[tuple[str, str]]:
    return [(label.strip(), body) for label, body in _BLOCK.findall(prompt)]


def _passage_sentences(prompt: str) -> list[str]:
    """Sentences from the document blocks, else from the prompt minus the
    obvious instruction and question lines."""
    blocks = _document_blocks(prompt)
    if blocks:
        out: list[str] = []
        for _, body in blocks:
            out.extend(_long_sentences(body.replace("[...]", "\n")))
        return out
    lines = [ln for ln in prompt.splitlines() if not _QUESTION_LINE.match(ln)]
    return _long_sentences("\n".join(lines))


def _answer_for(messages: list[dict]) -> str:
    """Quote a sentence from the retrieved passages in the user prompt."""
    user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    candidates = _passage_sentences(user)
    preferred = [s for s in candidates if _LEGAL_WORDS.search(s)]
    chosen = (preferred or candidates or ["The documents do not address this question."])[0]
    return f"Based on your documents: {chosen}"


def _grounding_for(prompt: str) -> dict:
    """One claim per file block, quoting a sentence of it verbatim."""
    claims = []
    for label, body in _document_blocks(prompt):
        ref = label.split(":", 1)[0].strip() if ":" in label else "f0"
        sentences = _long_sentences(body.replace("[...]", "\n"))
        if not sentences:
            continue
        # The same clause the answer quotes, so the claim is the answer's.
        quote = ([s for s in sentences if _LEGAL_WORDS.search(s)] or sentences)[0]
        claims.append({"claim": quote, "answer_quote": quote, "file_ref": ref, "quote": quote})
        if len(claims) >= 3:
            break
    return {"claims": claims}


def _support_for(prompt: str) -> dict:
    numbers = sorted({int(n) for n in re.findall(r'"n":\s*(\d+)', prompt)} or {0})
    return {"items": [{"n": n, "label": "supported", "note": ""} for n in numbers]}


def complete(messages: list[dict]) -> str:
    user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    compact = user.replace(" ", "")
    if '"claims":[' in compact:
        return json.dumps(_grounding_for(user))
    if '"items":[' in compact:
        return json.dumps(_support_for(user))
    if '"intent":' in user:
        return json.dumps({"intent": "ask", "confidence": 0.9})
    if "STRICT JSON" in user:
        return "{}"
    return _answer_for(messages)


class Handler(BaseHTTPRequestHandler):
    server_version = "casecite-mock-llm/1"

    def log_message(self, fmt, *args):  # quiet unless asked
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _json(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b"{}"
        try:
            return json.loads(raw or b"{}")
        except json.JSONDecodeError:
            return {}

    def do_GET(self):  # noqa: N802
        if self.path.rstrip("/") in ("/health", ""):
            self._json(200, {"status": "ok", "mock": True})
        elif self.path.startswith("/v1/models"):
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"id": "mock-chat", "object": "model"},
                        {"id": "hash-768", "object": "model"},
                    ],
                },
            )
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})

    def do_POST(self):  # noqa: N802
        body = self._body()
        if self.path.startswith("/v1/chat/completions"):
            messages = body.get("messages") or []
            text = complete(messages)
            prompt_tokens = sum(len(str(m.get("content", "")).split()) for m in messages)
            completion_tokens = len(text.split())
            self._json(
                200,
                {
                    "id": f"chatcmpl-{uuid.uuid4().hex[:12]}",
                    "object": "chat.completion",
                    "created": int(time.time()),
                    "model": body.get("model") or "mock-chat",
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": text},
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {
                        "prompt_tokens": prompt_tokens,
                        "completion_tokens": completion_tokens,
                        "total_tokens": prompt_tokens + completion_tokens,
                    },
                },
            )
        elif self.path.startswith("/v1/embeddings"):
            inputs = body.get("input")
            if isinstance(inputs, str):
                inputs = [inputs]
            inputs = inputs or []
            self._json(
                200,
                {
                    "object": "list",
                    "data": [
                        {"object": "embedding", "index": i, "embedding": embed(str(text))}
                        for i, text in enumerate(inputs)
                    ],
                    "model": body.get("model") or "hash-768",
                    "usage": {"prompt_tokens": 0, "total_tokens": 0},
                },
            )
        else:
            self._json(404, {"error": {"message": f"no route {self.path}"}})


def serve(port: int, verbose: bool = False) -> None:
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.verbose = verbose  # type: ignore[attr-defined]
    print(f"mock LLM server on http://127.0.0.1:{port} (embeddings: {DIMENSIONS} dims)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Offline stand-in for the AI provider")
    parser.add_argument("--port", type=int, default=8089)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()
    serve(args.port, args.verbose)
