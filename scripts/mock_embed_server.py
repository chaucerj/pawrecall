#!/usr/bin/env python3
"""Mock OpenAI-compatible embedding server for testing pawrecall's semantic layer.

Deterministic char-bigram hashing vectors (dim 256). NO semantic quality —
only verifies the pipeline (backfill -> cosine -> hybrid RRF -> MCP) works.

Usage:  python3 mock_embed_server.py [port]     (default 8765)
Config: {"provider": "openai", "url": "http://127.0.0.1:8765/v1", "model": "mock", "key": "test"}
"""
import json
import sys
from http.server import BaseHTTPRequestHandler, HTTPServer

DIM = 256


def embed(text):
    v = [0.0] * DIM
    t = text.lower()
    for i in range(len(t) - 1):
        v[hash(t[i:i + 2]) % DIM] += 1.0
    if len(t) == 1:
        v[hash(t) % DIM] += 1.0
    n = sum(x * x for x in v) ** 0.5 or 1.0
    return [x / n for x in v]


class H(BaseHTTPRequestHandler):
    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        texts = body.get("input") or []
        if isinstance(texts, str):
            texts = [texts]
        out = [{"object": "embedding", "index": i, "embedding": embed(t)} for i, t in enumerate(texts)]
        resp = json.dumps({"object": "list", "data": out, "model": body.get("model", "mock")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(resp)))
        self.end_headers()
        self.wfile.write(resp)

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8765
    print(f"mock embed server on :{port}")
    HTTPServer(("127.0.0.1", port), H).serve_forever()
