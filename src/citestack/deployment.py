"""Create a private local deployment directory without printing credentials."""

import hashlib
import json
import os
import re
import secrets
from pathlib import Path


def initialize(directory: Path, hostname: str, tenants: list[str], *, daily_limit=1000):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]{0,251}", hostname):
        raise ValueError("Use a DNS hostname or IPv4 address")
    if (
        not tenants
        or len(tenants) > 128
        or len(set(tenants)) != len(tenants)
        or any(not re.fullmatch(r"[a-z][a-z0-9_-]{0,31}", tenant) for tenant in tenants)
    ):
        raise ValueError("Use unique lowercase tenant IDs")
    if not 1 <= daily_limit <= 10_000_000:
        raise ValueError("daily_limit must be between 1 and 10,000,000")
    if os.getuid() == 0:
        raise ValueError("Initialize as the non-root account that will own the deployment files")
    directory = directory.resolve()
    if any(character in str(directory) for character in ("'", "\n", ":")):
        raise ValueError("Deployment path must not contain quotes, newlines, or colons")
    directory.mkdir(mode=0o700, parents=True, exist_ok=False)
    for name in ("config", "keys", "indexes", "quotas", "tls", "ollama", "retrieval-models"):
        (directory / name).mkdir(mode=0o700)
    definitions = []
    for tenant in tenants:
        key = secrets.token_urlsafe(32)
        for suffix, value in (("key", key), ("header", "X-API-Key: " + key)):
            path = directory / "keys" / f"{tenant}.{suffix}"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w") as stream:
                stream.write(value + "\n")
        definitions.append(
            {
                "id": tenant,
                "key_hashes": [hashlib.sha256(key.encode()).hexdigest()],
                "index_path": f"/app/indexes/{tenant}.sqlite",
                "daily_limit": daily_limit,
            }
        )
    registry = directory / "config" / "tenants.json"
    registry.write_text(json.dumps({"tenants": definitions}, indent=2) + "\n")
    settings = {
        "CITESTACK_DEPLOY_DIR": str(directory),
        "CITESTACK_RUN_UID": str(os.getuid()),
        "CITESTACK_RUN_GID": str(os.getgid()),
        "CITESTACK_BIND_IP": "127.0.0.1",
        "CITESTACK_HTTPS_PORT": "8443",
        "CITESTACK_PRODUCTION_HOSTS": json.dumps([hostname, "127.0.0.1", "api"]),
    }
    # Compose dotenv single quotes preserve JSON and spaces without interpolation.
    if any("'" in value or "\n" in value for value in settings.values()):
        raise ValueError("Deployment path must not contain quotes or newlines")
    env = directory / "compose.env"
    env.write_text("".join(f"{name}='{value}'\n" for name, value in settings.items()))
    env.chmod(0o600)
    return {
        "directory": str(directory),
        "compose_env": str(env),
        "tenants": tenants,
        "next_steps": [
            "Install TLS cert.pem and key.pem in tls/",
            "Copy each client's verified snapshot into indexes/<tenant>.sqlite",
            "Populate retrieval-models/ with the pinned Hugging Face cache",
            "Start Ollama and pull qwen3:4b, then start the API and proxy",
        ],
    }
