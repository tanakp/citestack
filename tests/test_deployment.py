import hashlib
import json
import stat

import pytest

from citestack.deployment import initialize


def test_production_setup_keeps_keys_private_and_registry_hashed(tmp_path):
    root = tmp_path / "production"
    result = initialize(root, "api.example.org", ["alpha", "beta"])
    config = json.loads((root / "config/tenants.json").read_text())
    for tenant in config["tenants"]:
        key_file = root / "keys" / f"{tenant['id']}.key"
        key = key_file.read_text().strip()
        assert len(key) >= 32
        assert stat.S_IMODE(key_file.stat().st_mode) == 0o600
        assert tenant["key_hashes"] == [hashlib.sha256(key.encode()).hexdigest()]
        assert key not in json.dumps(result) and key not in json.dumps(config)
    with pytest.raises(FileExistsError):
        initialize(root, "api.example.org", ["alpha"])
    assert json.loads((root / "config/tenants.json").read_text()) == config


@pytest.mark.parametrize(
    "hostname,tenants",
    [
        ("bad'host", ["alpha"]),
        ("localhost", ["../escape"]),
        ("localhost", ["same", "same"]),
        ("localhost", [f"client{i}" for i in range(129)]),
    ],
)
def test_invalid_setup_does_not_create_secrets(tmp_path, hostname, tenants):
    root = tmp_path / "production"
    with pytest.raises(ValueError):
        initialize(root, hostname, tenants)
    assert not root.exists()


def test_root_owned_deployment_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr("citestack.deployment.os.getuid", lambda: 0)
    with pytest.raises(ValueError, match="non-root"):
        initialize(tmp_path / "production", "localhost", ["default"])
