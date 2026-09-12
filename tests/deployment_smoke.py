"""Exercise the real production Compose profile over verified TLS. Requires Docker."""

import argparse
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from citestack.deployment import initialize


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rag-index", type=Path)
    parser.add_argument("--model-cache", type=Path)
    parser.add_argument("--check-ollama", action="store_true")
    parser.add_argument("--load-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.load_seconds <= 300 or (args.load_seconds and not args.rag_index):
        parser.error("Load duration must be 0..300 and requires --rag-index")
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary) / "production"
        tenants = ["default", "load"] if args.load_seconds else ["default"]
        initialize(root, "localhost", tenants, daily_limit=2)
        if args.rag_index:
            shutil.copyfile(args.rag_index, root / "indexes/default.sqlite")
            if not args.model_cache:
                raise ValueError("RAG deployment check requires the populated model cache")
        if args.load_seconds:
            shutil.copyfile(args.rag_index, root / "indexes/load.sqlite")
            registry_path = root / "config/tenants.json"
            registry = json.loads(registry_path.read_text())
            registry["tenants"][1].update(daily_limit=10000, burst=1000, rate_per_minute=10000)
            registry_path.write_text(json.dumps(registry))
        subprocess.run(
            [
                "openssl",
                "req",
                "-x509",
                "-newkey",
                "rsa:2048",
                "-nodes",
                "-days",
                "1",
                "-subj",
                "/CN=localhost",
                "-addext",
                "subjectAltName=DNS:localhost",
                "-keyout",
                str(root / "tls/key.pem"),
                "-out",
                str(root / "tls/cert.pem"),
            ],
            check=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        (root / "tls/key.pem").chmod(0o600)
        with socket.socket() as reservation:
            reservation.bind(("127.0.0.1", 0))
            port = reservation.getsockname()[1]
        override = {
            "services": {
                "api": {
                    "image": "citestack:ci",
                    "healthcheck": {"interval": "2s", "start_period": "2s"},
                },
                "ollama": {
                    "image": "citestack:ci",
                    "entrypoint": ["python", "/stub.py"],
                    "command": [],
                    "healthcheck": {
                        "test": [
                            "CMD",
                            "python",
                            "-c",
                            "import urllib.request; "
                            "urllib.request.urlopen('http://127.0.0.1:11434/api/tags')",
                        ],
                        "interval": "2s",
                        "timeout": "5s",
                        "retries": 5,
                    },
                    "volumes": [f"{Path('tests/ollama_stub.py').resolve()}:/stub.py:ro"],
                },
            }
        }
        if not args.rag_index:
            override["services"]["api"]["command"] = [
                "serve",
                "--structured-only",
                "--host",
                "0.0.0.0",
            ]
        else:
            override["services"]["api"]["volumes"] = [
                f"{args.model_cache.resolve()}:/app/retrieval-models:ro"
            ]
        override_path = root / "override.json"
        override_path.write_text(json.dumps(override))
        env = {**os.environ, "CITESTACK_HTTPS_PORT": str(port)}
        command = [
            "docker",
            "compose",
            "--project-name",
            "citestack-" + root.parent.name.lower(),
            "--env-file",
            str(root / "compose.env"),
            "-f",
            "compose.production.yaml",
            "-f",
            str(override_path),
        ]

        def compose(*arguments):
            return subprocess.run(
                [*command, *arguments], check=True, env=env, capture_output=True, text=True
            ).stdout

        context = ssl.create_default_context(cafile=str(root / "tls/cert.pem"))
        key = (root / "keys/default.key").read_text().strip()

        def request(path, payload=None, *, authenticated=True, tls=context, credential=key):
            headers = {"X-API-Key": credential} if authenticated else {}
            data = json.dumps(payload).encode() if payload is not None else None
            if data is not None:
                headers["Content-Type"] = "application/json"
            req = urllib.request.Request(
                f"https://localhost:{port}" + path, data=data, headers=headers
            )
            try:
                with urllib.request.urlopen(req, context=tls, timeout=130) as response:
                    return response.status, response.read()
            except urllib.error.HTTPError as error:
                return error.code, error.read()

        checks = []
        try:
            if args.check_ollama:
                base = command[:-2]
                subprocess.run(
                    [*base, "up", "-d", "--no-deps", "ollama"],
                    env=env,
                    check=True,
                    capture_output=True,
                    text=True,
                )
                deadline = time.monotonic() + 30
                while True:
                    status = subprocess.run(
                        [*base, "exec", "-T", "ollama", "ollama", "list"],
                        env=env,
                        capture_output=True,
                    )
                    if status.returncode == 0:
                        break
                    assert time.monotonic() < deadline, "Rootless Ollama failed to start"
                    time.sleep(0.5)
                checks.append("rootless_ollama_startup")
            compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "120")
            assert request("/readyz")[0] == 200
            try:
                request("/healthz", tls=ssl.create_default_context())
                raise AssertionError("Untrusted TLS certificate was accepted")
            except urllib.error.URLError as error:
                assert isinstance(error.reason, ssl.SSLCertVerificationError)
            checks.extend(["verified_tls", "reject_untrusted_certificate", "dependency_readiness"])
            path = "/v1/answer" if args.rag_index else "/v1/structured/ticket"
            payload = (
                {"question": "What is a Kubernetes Pod?"}
                if args.rag_index
                else {"text": "checkout is down private-input-sentinel"}
            )
            assert request(path, payload, authenticated=False)[0] == 401
            status, raw = request(path, payload)
            assert status == 200
            answer = json.loads(raw)
            assert answer.get("citations") if args.rag_index else answer["status"] == "success"
            assert request("/metrics")[0] == 404
            assert request(path, {"text": "x" * 70000})[0] == 413
            checks.extend(
                ["authentication", "application_request", "private_metrics", "proxy_body_limit"]
            )
            load_report = None
            if args.load_seconds:
                load_key = (root / "keys/load.key").read_text().strip()
                started = time.monotonic()
                deadline = started + args.load_seconds

                def load_worker():
                    observations = []
                    while time.monotonic() < deadline:
                        tick = time.monotonic()
                        status, raw = request(path, payload, credential=load_key)
                        assert status == 200 and json.loads(raw)["citations"]
                        observations.append(time.monotonic() - tick)
                    return observations

                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(load_worker) for _ in range(2)]
                    observations = sorted(value for future in futures for value in future.result())
                elapsed = time.monotonic() - started
                load_report = {
                    "concurrency": 2,
                    "seconds": round(elapsed, 3),
                    "requests": len(observations),
                    "all_have_citations": True,
                    "requests_per_second": round(len(observations) / elapsed, 3),
                    "p95_seconds": round(observations[int((len(observations) - 1) * 0.95)], 3),
                    "api_cpu_limit": 2,
                    "api_memory_bytes": 2147483648,
                    "generation": "extractive",
                    "clients_loaded": 2,
                }
                print(json.dumps({"linux_compose_load": load_report}), flush=True)
                checks.append("sustained_real_retrieval_under_container_limits")
            compose("restart", "api")
            deadline = time.monotonic() + 60
            while True:
                try:
                    if request("/readyz")[0] == 200:
                        break
                except (urllib.error.URLError, ConnectionError):
                    pass
                assert time.monotonic() < deadline, "API did not recover after restart"
                time.sleep(0.5)
            assert request(path, payload)[0] == 200
            assert request(path, payload)[0] == 429
            checks.append("quota_persistence_after_restart")
            for service in ("api", "proxy", "ollama"):
                identifier = compose("ps", "-q", service).strip()
                details = json.loads(subprocess.check_output(["docker", "inspect", identifier]))[0]
                assert details["Config"]["User"].split(":")[0] not in {"", "0", "root"}
                assert details["HostConfig"]["ReadonlyRootfs"]
                assert "ALL" in details["HostConfig"]["CapDrop"]
                assert details["HostConfig"]["Memory"] > 0
                assert details["HostConfig"]["NanoCpus"] > 0
                assert details["HostConfig"]["PidsLimit"] > 0
                assert any(
                    value
                    in {"no-new-privileges", "no-new-privileges:true", "no-new-privileges=true"}
                    for value in details["HostConfig"]["SecurityOpt"]
                )
                if service != "proxy":
                    assert not details["HostConfig"]["PortBindings"]
                if service == "api":
                    mounts = {item["Destination"]: item for item in details["Mounts"]}
                    for path in ("/app/config", "/app/indexes", "/app/retrieval-models"):
                        assert not mounts[path]["RW"]
                    assert mounts["/app/quotas"]["RW"]
            checks.extend(
                ["nonroot_readonly_containers", "resource_limits", "private_backend_ports"]
            )
            logs = compose("logs", "--no-color")
            assert key not in logs and "private-input-sentinel" not in logs
            if args.load_seconds:
                assert load_key not in logs
            checks.append("log_redaction")
            print(
                json.dumps(
                    {
                        "passed": checks,
                        "rag": bool(args.rag_index),
                        "model_peer": "deterministic stub",
                    }
                )
            )
            if load_report:
                Path("data/deployment-load.json").write_text(
                    json.dumps(load_report, indent=2) + "\n"
                )
        except Exception as error:
            logs = compose("logs", "--no-color")
            if isinstance(error, subprocess.CalledProcessError):
                logs += "\n" + (error.stdout or "") + "\n" + (error.stderr or "")
            for key_file in (root / "keys").glob("*.key"):
                logs = logs.replace(key_file.read_text().strip(), "[redacted]")
            print(logs[-6000:].replace("private-input-sentinel", "[redacted]"))
            raise
        finally:
            compose("down", "--remove-orphans")


if __name__ == "__main__":
    main()
