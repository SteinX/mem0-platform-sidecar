from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

EMBEDDING_DIMENSIONS = 1536


class OpenAIStubHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if self.path == "/v1/models":
            self._write_json(
                {
                    "object": "list",
                    "data": [
                        {"id": "gpt-5-mini", "object": "model"},
                        {"id": "text-embedding-3-small", "object": "model"},
                    ],
                }
            )
            return
        self.send_error(404)

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/v1/embeddings":
            inputs = payload.get("input", [])
            if isinstance(inputs, str):
                inputs = [inputs]
            self._write_json(
                {
                    "object": "list",
                    "model": payload.get("model", "text-embedding-3-small"),
                    "data": [
                        {
                            "object": "embedding",
                            "index": index,
                            "embedding": self._embedding_for(text),
                        }
                        for index, text in enumerate(inputs)
                    ],
                    "usage": {
                        "prompt_tokens": len(inputs),
                        "total_tokens": len(inputs),
                    },
                }
            )
            return
        if self.path == "/v1/chat/completions":
            self._write_json(
                {
                    "id": "chatcmpl-e2e-stub",
                    "object": "chat.completion",
                    "created": 0,
                    "model": payload.get("model", "gpt-5-mini"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {"role": "assistant", "content": "[]"},
                            "finish_reason": "stop",
                        }
                    ],
                }
            )
            return
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        return

    def _write_json(self, payload: dict[str, object]) -> None:
        encoded = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    def _embedding_for(self, text: object) -> list[float]:
        seed = sum(ord(character) for character in str(text)) % 997
        return [((seed + index) % 101) / 100.0 for index in range(EMBEDDING_DIMENSIONS)]


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 8000), OpenAIStubHandler).serve_forever()
