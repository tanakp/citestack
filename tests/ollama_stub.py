"""Deterministic protocol peer for deployment tests; never used in production."""

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Peer(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def reply(self, data):
        raw = json.dumps(data).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        self.reply({"models": [{"name": "qwen3:4b"}]})

    def do_POST(self):
        self.rfile.read(int(self.headers["Content-Length"]))
        self.reply(
            {
                "done": True,
                "response": json.dumps(
                    {
                        "summary": "Checkout is down.",
                        "category": "availability",
                        "priority": "high",
                        "affected_services": ["checkout"],
                        "requires_human_review": False,
                    }
                ),
            }
        )


if __name__ == "__main__":
    ThreadingHTTPServer(("0.0.0.0", 11434), Peer).serve_forever()
