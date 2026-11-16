"""A tiny stand-in for the Anthropic Messages API, used by the replay e2e tests.

    python frontend/e2e/fake_anthropic.py 7798

The `stepback ui` under test is started with ANTHROPIC_BASE_URL pointing here, so replays
go through the real SDK code path without any real API call or cost.

* POST /v1/messages/count_tokens -> {"input_tokens": ~len/4}
* POST /v1/messages             -> echoes the system prompt; model "claude-retired-1"
                                   gets the API's 404 not_found_error.
"""

from __future__ import annotations

import json
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args: Any) -> None:  # keep test output quiet
        pass

    def _send(self, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("content-type", "application/json")
        self.send_header("request-id", "req_fake")
        self.send_header("content-length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        self._send(200, {"ok": True})

    def do_POST(self) -> None:
        length = int(self.headers.get("content-length") or 0)
        request = json.loads(self.rfile.read(length) or b"{}")
        if not (self.headers.get("x-api-key") or self.headers.get("authorization")):
            self._send(401, {"type": "error", "error": {"type": "authentication_error", "message": "missing API key"}})
            return
        size = len(json.dumps([request.get("system"), request.get("messages"), request.get("tools")]))
        if self.path.split("?")[0].endswith("/count_tokens"):
            self._send(200, {"input_tokens": max(1, size // 4)})
            return
        model = request.get("model", "")
        if model == "claude-retired-1":
            self._send(404, {"type": "error", "error": {"type": "not_found_error", "message": f"model: {model}"}})
            return
        system = request.get("system")
        system_text = system if isinstance(system, str) else json.dumps(system)
        self._send(
            200,
            {
                "id": "msg_fake_replay",
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": f"Replayed reply (system: {(system_text or '')[:60]})"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": max(1, size // 4), "output_tokens": 21},
            },
        )


def main() -> None:
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 7798
    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    print(f"fake Anthropic API on http://127.0.0.1:{port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
