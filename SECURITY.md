# Security scope

CiteStack is a single-host portfolio/reference service. Client keys select isolated
snapshots; processes, retrieval model weights, and the host are shared. Operators own
certificate issuance, firewall rules, secret distribution, backups, and host patching.
See [operating controls](docs/operations.md) and [deployment](docs/deployment.md).

CI scans all fetched Git history with checksum-pinned Gitleaks and audits installed,
locked Linux Python dependencies using pip-audit 2.10.1. A detected vulnerability or
scanner failure fails the check; there are no ignored CVE IDs. The CPU-only Torch
wheel is matched to its upstream release version for PyPI advisories; unknown/skipped
third-party packages fail coverage validation. Local CiteStack source is excluded from
PyPI advisory matching. Reports and the exact audit inventory are CI artifacts.
Run the security workflow manually after an advisory, even when no code changed.
Image digest pins preserve reproducibility but must be deliberately refreshed after
security review; Python package auditing does not scan operating-system packages,
model weights, or establish absence of vulnerabilities. These checks complement the
HTTP, tenant isolation, deadline, corruption, and restricted-container tests.

Do not put credentials or private documents in public issues. Use the repository's
private vulnerability reporting channel when available; otherwise contact the owner
through their GitHub profile before sharing sensitive reproduction material. Revoke
any exposed credential immediately, including credentials found in old Git commits.
