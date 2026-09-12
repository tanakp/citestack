# Production deployment profile

The supported target is one Docker host, one API process, distinct client snapshots,
and a local Ollama service. `compose.production.yaml` adds verified TLS ingress,
non-root service accounts, read-only container filesystems, explicit memory/CPU/PID
limits, private backend ports, persistent quotas, and read-only model/index mounts.
This is a single-host deployment with planned restart downtime, not a highly available
cluster. Monitoring/alert wiring and measured capacity remain in the acceptance plan.

## Prepare as the non-root deployment account

Build the corpus and model cache using the README quick start, then initialize private
configuration. Use the DNS name on your TLS certificate:

```bash
uv run citestack init-production data/production --hostname api.example.org \
  --tenant alpha --tenant beta
```

Initialization refuses to overwrite an existing directory. It writes random client keys
to mode-0600 files in `keys/`; only SHA-256 key digests enter `config/tenants.json`. Keys
are never printed. The generated `compose.env` contains paths, numeric UID/GID, hostnames,
and a loopback bind address; no client credentials. All files under `data/` are ignored
by Git. Transfer each client's key through your normal secure credential channel.

The profile runs containers with the initializing account's numeric UID/GID so mode-0600
TLS keys and mode-0700 writable directories are accessible without running the services
as root or making secrets world-readable. Run initialization and Compose as that same
non-root account on a Linux/macOS Docker host. Rootless Docker UID remapping and Windows
filesystems require a separate permissions validation; they are not the tested target.

For public documentation clients, create separate verified index copies and populate
the shared retrieval-model cache. For private client data, build each client's corpus
separately; never copy one client's private snapshot into another client's directory.

```bash
uv run citestack backup data/production/indexes/alpha.sqlite
uv run citestack backup data/production/indexes/beta.sqlite
cp -a .cache/huggingface/. data/production/retrieval-models/
```

Install your TLS certificate chain at `data/production/tls/cert.pem` and private key at
`data/production/tls/key.pem`; keep the key mode 0600. Obtain certificates through your
chosen certificate authority. CI uses a temporary localhost test certificate and
explicitly verifies its trust chain; it does not disable certificate verification.

## Start the pinned services

```bash
docker compose --env-file data/production/compose.env -f compose.production.yaml build
docker compose --env-file data/production/compose.env -f compose.production.yaml up -d ollama
docker compose --env-file data/production/compose.env -f compose.production.yaml exec ollama ollama pull qwen3:4b
docker compose --env-file data/production/compose.env -f compose.production.yaml up -d --wait
```

Python, Nginx, and Ollama images are pinned by digest. The accepted model manifest digest
is `359d7dd4bcdab3d86b87d73ac27966f4dbb9f5efdfcc75d34a8764a09474fae7`; compare it with the
model listed by Ollama before promotion. The model tag itself is mutable. API readiness
checks availability, while the quality gate checks the exact model/server fingerprint.
Run the quality suite after intentional model changes before updating references.

The default API mode is extractive evidence preview. For generated answers, add
`CITESTACK_ANSWER_MODE: ollama` to the API environment and run the answer-quality checks
appropriate to your documents. No cloud API key is needed.

Only the proxy publishes a host port, initially `127.0.0.1:8443`. Set
`CITESTACK_BIND_IP=0.0.0.0` in the generated environment only when you intend internet
exposure, have installed a valid public certificate, and have configured the host
firewall. API port 8000 and Ollama port 11434 remain private to the Compose network.
`/metrics` is blocked at the public proxy. TLS requires 1.2 or 1.3; the proxy suppresses
raw access logs and enforces a 64 KiB body cap and connection limits.
Nginx request error logs are also suppressed because they can contain raw query
strings. Use API metrics, readiness, and container state for upstream diagnosis.

Use a header file to avoid putting a key in process arguments or shell history:

```bash
curl https://api.example.org:8443/v1/index \
  --header @data/production/keys/alpha.header
```

For loopback testing with a certificate for a real DNS name, use curl's `--resolve`
option to route that name to 127.0.0.1 while preserving hostname verification. Never
use `--insecure` as the production connectivity check.

## Resource and persistence boundaries

The API is limited to two CPUs, 2 GiB RAM, 256 PIDs, and two inference workers. Ollama is
limited to two CPUs, 6 GiB RAM, one parallel generation, and a queue of two. Nginx has
0.5 CPU, 128 MiB RAM, and 64 PIDs. These are explicit limits, not a throughput promise;
real sustained-load measurements and resulting recommended client concurrency are still
required before final acceptance. A 40-second supervisor grace period bounds shutdown
after the API's 30-second drain window.

Writable state is limited to the host's `quotas/` and `ollama/` directories plus bounded
temporary filesystems. Client indexes, retrieval model files, registry, TLS certificate,
and TLS key are mounted read-only. Removing/replacing `quotas/` resets admission history;
normal API restarts preserve it. The Docker build context is an allowlist of package
sources, lockfile, and README, excluding credentials, corpora, caches, and local work.

## Upgrade and rollback

1. Keep the previous application image under a distinct release tag (or registry digest),
   the prior client registry, and verified index backups. Do not use a mutable image tag
   as your only rollback record.
2. Build the candidate image, run unit/container/model quality checks, and inspect every
   candidate client snapshot. Retain the old snapshots until the new version is accepted.
3. Stop incoming traffic for a maintenance window. Restore the selected snapshots using
   the [recovery commands](recovery.md), then recreate **both API and proxy** so Nginx
   refreshes the backend address. Verify readiness and each client's snapshot ID over TLS.
4. To roll back, select the previous application image with `CITESTACK_IMAGE`, restore its
   matching index format, restore the matching registry if needed, and recreate API/proxy.
   Preserve the quota database; an application rollback must not refund consumed quotas.

Certificate rotation replaces the certificate/key files on the host and restarts the
proxy. Client key rotation updates registry digests and restarts the API; the registry
supports overlapping active keys. Do not publish or mount clients' raw key files into
the API container. Back up credentials and certificates using encrypted storage and an
operator-controlled retention policy, separately from public index artifacts.

## Deployment verification

`tests/deployment_smoke.py` drives the actual Compose profile. Standard CI checks trusted
TLS, rejection of an untrusted certificate, authentication, proxy body limits, private
metrics, quota persistence across restart, and Docker's effective user/filesystem/port/
resource settings. It also starts the pinned Ollama image as the non-root deployment
user before switching to a deterministic protocol peer for fast API assertions.

The full-corpus retrieval CI job repeats the Compose check with the real index and pinned
retrieval models mounted read-only. Generation quality uses the separate pinned real-model
job; the protocol peer is explicitly not presented as model-quality evidence. The profile
must pass these CI checks at the final published revision before the release is accepted.
