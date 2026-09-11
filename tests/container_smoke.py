"""Exercise the packaged service over TCP with a deterministic Ollama protocol peer.

Runs under Docker in CI. No model credentials, external network, or model downloads.
This tests transport/deployment behavior; it does not measure model quality.
"""

import json
import os
import secrets
import socket
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx


class OllamaPeer(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    available = True

    def log_message(self, *_):
        pass

    def reply(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply({"models": [{"name": "qwen3:4b"}]} if self.available else {"models": []})

    def do_POST(self):
        payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        if "malformed-sentinel" in payload["prompt"]:
            self.reply({"response": "not valid JSON", "done": True})
            return
        self.reply(
            {
                "done": True,
                "response": json.dumps(
                    {
                        "summary": "Checkout is unavailable.",
                        "category": "availability",
                        "priority": "high",
                        "affected_services": ["checkout"],
                        "requires_human_review": False,
                    }
                ),
            }
        )


def main():
    peer = ThreadingHTTPServer(("127.0.0.1", 0), OllamaPeer)
    threading.Thread(target=peer.serve_forever, daemon=True).start()
    with socket.socket() as reservation:
        reservation.bind(("127.0.0.1", 0))
        port = reservation.getsockname()[1]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        key = secrets.token_urlsafe(32)
        key_path = root / "api-key"
        key_path.write_text(key)
        key_path.chmod(0o600)
        env = {
            **os.environ,
            "CITESTACK_ENVIRONMENT": "production",
            "CITESTACK_API_KEY_FILE": str(key_path),
            "CITESTACK_ALLOWED_HOSTS": '["127.0.0.1"]',
            "CITESTACK_QUOTA_PATH": str(root / "quotas.sqlite"),
            "CITESTACK_OLLAMA_URL": f"http://127.0.0.1:{peer.server_port}",
            "CITESTACK_READINESS_CACHE_SECONDS": "0",
            "CITESTACK_RATE_LIMIT_BURST": "20",
            "CITESTACK_MAX_REQUEST_BYTES": "1024",
            "CITESTACK_SHUTDOWN_TIMEOUT": "2",
        }
        with (root / "service.log").open("w+") as log:
            process = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "citestack.cli",
                    "serve",
                    "--structured-only",
                    "--port",
                    str(port),
                ],
                env=env,
                stdout=log,
                stderr=log,
            )
            checks = []
            try:
                with httpx.Client(
                    base_url=f"http://127.0.0.1:{port}", timeout=5, trust_env=False
                ) as client:
                    deadline = time.monotonic() + 20
                    while True:
                        if process.poll() is not None:
                            raise AssertionError("Packaged API failed to start")
                        try:
                            if client.get("/healthz").status_code == 200:
                                break
                        except httpx.ConnectError:
                            pass
                        assert time.monotonic() < deadline, "Startup deadline exceeded"
                        time.sleep(0.1)
                    assert client.get("/readyz").status_code == 200
                    assert client.get("/openapi.json").status_code == 404
                    assert (
                        client.post(
                            "/v1/structured/ticket", json={"text": "checkout down"}
                        ).status_code
                        == 401
                    )
                    headers = {"X-API-Key": key}
                    response = client.post(
                        "/v1/structured/ticket",
                        headers=headers,
                        json={"text": "checkout down private-input-sentinel"},
                    )
                    assert response.status_code == 200 and response.json()["status"] == "success"
                    assert response.headers["x-request-id"]
                    checks.extend(["startup", "readiness", "authentication", "structured_output"])
                    fallback = client.post(
                        "/v1/structured/ticket",
                        headers=headers,
                        json={"text": "malformed-sentinel"},
                    )
                    assert (
                        fallback.json()["status"] == "fallback" and fallback.json()["attempts"] == 2
                    )
                    assert (
                        client.post(
                            "/v1/structured/ticket", headers=headers, json={"text": "x" * 2000}
                        ).status_code
                        == 413
                    )
                    invalid = client.post(
                        "/v1/structured/ticket",
                        headers=headers,
                        json={"text": "private-input-sentinel", "extra": "secret"},
                    )
                    assert invalid.status_code == 422 and "secret" not in invalid.text
                    assert (
                        client.get("/healthz", headers={"Host": "untrusted.example"}).status_code
                        == 400
                    )
                    assert "citestack_output_total" in client.get("/metrics", headers=headers).text
                    OllamaPeer.available = False
                    assert client.get("/readyz").status_code == 503
                    assert client.get("/healthz").status_code == 200
                    checks.extend(
                        [
                            "malformed_fallback",
                            "body_limit",
                            "validation_redaction",
                            "host_validation",
                            "metrics",
                            "dependency_failure",
                        ]
                    )
            finally:
                process.terminate()
                try:
                    process.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
                    raise AssertionError("API failed to drain") from None
                peer.shutdown()
                peer.server_close()
            assert process.returncode in {0, -15}
            log.seek(0)
            logs = log.read()
            assert key not in logs and "private-input-sentinel" not in logs
            checks.extend(["shutdown", "log_redaction"])
            print(json.dumps({"passed": checks, "peer": "deterministic protocol stub"}))


if __name__ == "__main__":
    main()
