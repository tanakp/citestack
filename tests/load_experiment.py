"""Opt-in, bounded real-model HTTP load and dependency-recovery experiment.

Requires the full local index, cached retrieval models, and Ollama's qwen3:4b.
Creates only its own child processes, credentials, and temporary quota database.
"""

import argparse
import asyncio
import json
import os
import platform
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path

import httpx


def unused_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def percentile(values, fraction):
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int((len(ordered) - 1) * fraction))], 4)


async def phase(client, path, payloads, concurrency, seconds):
    started = time.monotonic()
    deadline = started + seconds
    rows = []

    async def worker(offset):
        number = offset
        while time.monotonic() < deadline:
            tick = time.monotonic()
            response = await client.post(path, json=payloads[number % len(payloads)])
            body = response.json()
            row = {"status": response.status_code, "seconds": time.monotonic() - tick}
            if response.status_code == 200:
                if path.endswith("ticket"):
                    row["outcome"] = body["status"]
                else:
                    assert body["citations"], "Public answer lost citation evidence under load"
            rows.append(row)
            number += 1
            # Bound offered load during fast capacity rejections; avoid a busy loop.
            if response.status_code != 200:
                await asyncio.sleep(0.1)

    await asyncio.gather(*(worker(i) for i in range(concurrency)))
    elapsed = time.monotonic() - started
    successes = [r["seconds"] for r in rows if r["status"] == 200]
    result = {
        "path": path,
        "concurrency": concurrency,
        "offered_seconds": seconds,
        "elapsed_seconds": round(elapsed, 3),
        "requests": len(rows),
        "http_statuses": dict(Counter(str(r["status"]) for r in rows)),
        "outcomes": dict(Counter(r["outcome"] for r in rows if "outcome" in r)),
        "successful_requests_per_second": round(len(successes) / elapsed, 3),
        "success_latency_seconds": {
            "p50": percentile(successes, 0.5),
            "p95": percentile(successes, 0.95),
            "max": round(max(successes), 4),
        }
        if successes
        else None,
    }
    print(json.dumps(result), flush=True)
    return result


async def wait_ready(url, process, timeout=180):
    async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            assert process.poll() is None, "Owned server exited; inspect private experiment logs"
            try:
                if (await client.get(url)).status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(0.5)
        raise TimeoutError("Server readiness deadline exceeded")


def stop(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=40)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


async def run(args):
    ollama = shutil.which("ollama")
    if not ollama:
        raise RuntimeError("Install Ollama and pull qwen3:4b before this experiment")
    api_port, model_port = unused_port(), unused_port()
    base = f"http://127.0.0.1:{api_port}"
    model_base = f"http://127.0.0.1:{model_port}"
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory() as temporary:
        root = Path(temporary)
        key = secrets.token_urlsafe(32)
        key_path = root / "key"
        key_path.write_text(key)
        key_path.chmod(0o600)
        env = {
            **os.environ,
            "CITESTACK_ENVIRONMENT": "production",
            "CITESTACK_API_KEY_FILE": str(key_path),
            "CITESTACK_ALLOWED_HOSTS": '["127.0.0.1"]',
            "CITESTACK_QUOTA_PATH": str(root / "quotas.sqlite"),
            "CITESTACK_OLLAMA_URL": model_base,
            "CITESTACK_RATE_LIMIT_PER_MINUTE": "10000",
            "CITESTACK_RATE_LIMIT_BURST": "1000",
            "CITESTACK_DAILY_REQUEST_LIMIT": "100000",
            "CITESTACK_READINESS_CACHE_SECONDS": "0",
            "CITESTACK_ANSWER_MODE": "extractive",
            "CITESTACK_MAX_CONCURRENT_REQUESTS": "2",
            "OMP_NUM_THREADS": "2",
            "MKL_NUM_THREADS": "2",
            "TOKENIZERS_PARALLELISM": "false",
            "HF_HUB_OFFLINE": "1",
            "OLLAMA_HOST": f"127.0.0.1:{model_port}",
            "OLLAMA_NUM_PARALLEL": "1",
            "OLLAMA_MAX_QUEUE": "2",
        }
        # An ambient key must not override the private experiment credential file.
        env.pop("CITESTACK_API_KEY", None)
        env.pop("CITESTACK_TENANT_CONFIG", None)
        processes = []
        with args.output.with_suffix(".private.log").open("w") as log:

            def start_model():
                process = subprocess.Popen([ollama, "serve"], env=env, stdout=log, stderr=log)
                processes.append(process)
                return process

            try:
                model = start_model()
                await wait_ready(model_base + "/api/version", model, 30)
                api = subprocess.Popen(
                    [sys.executable, "-m", "citestack.cli", "serve", "--port", str(api_port)],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                processes.append(api)
                await wait_ready(base + "/readyz", api)
                async with httpx.AsyncClient(
                    base_url=base, headers={"X-API-Key": key}, timeout=130, trust_env=False
                ) as client:
                    manifest = (await client.get("/v1/index")).json()
                    async with httpx.AsyncClient(trust_env=False) as peer:
                        models = (await peer.get(model_base + "/api/tags")).json()
                        version = (await peer.get(model_base + "/api/version")).json()
                    questions = [
                        {"question": q}
                        for q in (
                            "What is a Kubernetes Pod?",
                            "What is a Kubernetes Deployment?",
                            "What is a Kubernetes StatefulSet?",
                            "What is a Kubernetes ConfigMap?",
                        )
                    ]
                    # Warm retrieval and the generation model outside timed phases.
                    assert (await client.post("/v1/answer", json=questions[0])).status_code == 200
                    tickets = [
                        {"text": text}
                        for text in (
                            "The checkout service is down for every customer. "
                            "Please restore it urgently.",
                            "Please add a CSV export button to the reports service next quarter.",
                            "I was charged twice by the billing service. "
                            "Please investigate my invoice.",
                        )
                    ]
                    assert (await client.post("/v1/structured/ticket", json=tickets[0])).json()[
                        "status"
                    ] == "success"
                    results = []
                    for concurrency in (1, 2, 4):
                        results.append(
                            await phase(client, "/v1/answer", questions, concurrency, args.seconds)
                        )
                    results.append(
                        await phase(client, "/v1/structured/ticket", tickets, 1, args.seconds)
                    )
                    # A real dependency process outage must become visible and recover.
                    stop(model)
                    assert (await client.get("/healthz")).status_code == 200
                    assert (await client.get("/readyz")).status_code == 503
                    failed = (await client.post("/v1/structured/ticket", json=tickets[0])).json()
                    assert failed["status"] == "fallback"
                    model = start_model()
                    await wait_ready(base + "/readyz", api, 60)
                    assert (await client.post("/v1/structured/ticket", json=tickets[0])).json()[
                        "status"
                    ] == "success"
                    assert not any(set(p["http_statuses"]) - {"200", "503"} for p in results)
                    assert all(p["http_statuses"].get("200", 0) for p in results)
                    report = {
                        "recorded_at": datetime.now(UTC).isoformat(),
                        "git_revision": subprocess.check_output(
                            ["git", "rev-parse", "HEAD"], text=True
                        ).strip(),
                        "dirty_worktree": bool(
                            subprocess.check_output(["git", "status", "--porcelain"])
                        ),
                        "platform": platform.platform(),
                        "machine": platform.machine(),
                        "logical_cpus": os.cpu_count(),
                        "python": platform.python_version(),
                        "limits": {
                            "api_workers": 2,
                            "torch_threads": 2,
                            "ollama_parallel": 1,
                            "container_cpu_memory_limits": False,
                            "client_rate_limit_raised_for_load": True,
                        },
                        "snapshot_id": manifest.get("snapshot_id"),
                        "documents": manifest.get("documents"),
                        "ollama_version": version,
                        "models": {
                            "models": [m for m in models["models"] if m["name"] == "qwen3:4b"]
                        },
                        "phases": results,
                        "failure_checks": [
                            "liveness_during_model_outage",
                            "readiness_503",
                            "validated_fallback",
                            "recovery_after_model_restart",
                        ],
                        "limitations": [
                            "Local host, not the limited Linux Compose stack",
                            "Closed-loop load has coordinated omission",
                            "Repeated public questions and synthetic tickets",
                            "No long-term soak or quality grading under load",
                        ],
                    }
                    args.output.write_text(json.dumps(report, indent=2) + "\n")
            finally:
                for process in reversed(processes):
                    stop(process)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=int, default=60)
    parser.add_argument("--output", type=Path, default=Path("data/load-experiment.json"))
    args = parser.parse_args()
    if not 10 <= args.seconds <= 600:
        parser.error("--seconds must be between 10 and 600 per phase")
    asyncio.run(run(args))
