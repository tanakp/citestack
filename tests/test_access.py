import hashlib
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest
from pydantic import ValidationError

from citestack.access import QuotaExceeded, QuotaStore, QuotaUnavailable, Tenant, TenantRegistry
from citestack.config import Settings


def test_production_requires_auth_and_hosts():
    with pytest.raises(ValidationError, match="Production requires"):
        Settings(environment="production", _env_file=None)
    with pytest.raises(ValidationError, match="allowed_hosts"):
        Settings(environment="production", api_key="a" * 32, _env_file=None)
    settings = Settings(
        environment="production",
        api_key="a" * 32,
        allowed_hosts=["api.example.org"],
        _env_file=None,
    )
    assert "a" * 32 not in repr(settings)


def test_rotation_secret_file_and_error_redaction(tmp_path):
    secret = tmp_path / "secret"
    secret.write_text("a" * 32)
    settings = Settings(
        environment="production",
        api_key_file=secret,
        previous_api_key="b" * 32,
        allowed_hosts=["localhost"],
        _env_file=None,
    )
    registry = TenantRegistry(settings)
    assert registry.authenticate(b"a" * 32).id == "default"
    assert registry.authenticate(b"b" * 32).id == "default"
    assert registry.authenticate(b"wrong") is None
    assert registry.authenticate(None) is None
    assert registry.authenticate(b"a" * 513) is None
    with pytest.raises(ValidationError) as caught:
        Settings(environment="production", api_key="sensitive-short-key", _env_file=None)
    assert "sensitive-short-key" not in str(caught.value)


@pytest.mark.parametrize(
    "url", ["ftp://localhost", "http://user:secret@localhost", "http://localhost?key=secret"]
)
def test_reject_credentials_and_invalid_upstream_urls(url):
    with pytest.raises(ValidationError):
        Settings(ollama_url=url, _env_file=None)


def write_registry(settings, tenants):
    settings.tenant_config = settings.index_path.parent / "tenants.json"
    settings.tenant_config.write_text(json.dumps({"tenants": tenants}))


def definition(name, key, **kwargs):
    return {"id": name, "key_hashes": [hashlib.sha256(key.encode()).hexdigest()], **kwargs}


def test_registry_keys_bind_distinct_data(settings):
    write_registry(
        settings,
        [
            definition("alpha", "alpha-key", index_path="alpha.sqlite"),
            definition("beta", "beta-key", index_path="beta.sqlite"),
        ],
    )
    registry = TenantRegistry(settings)
    assert (
        registry.authenticate(b"alpha-key").index_path
        == settings.tenant_config.parent / "alpha.sqlite"
    )
    assert registry.authenticate(b"beta-key").id == "beta"
    assert registry.authenticate(None) is None


@pytest.mark.parametrize(
    "tenants",
    [
        [definition("alpha", "one"), definition("beta", "one")],
        [definition("alpha", "one"), definition("alpha", "two")],
        [
            definition("alpha", "one", index_path="same"),
            definition("beta", "two", index_path="./same"),
        ],
    ],
)
def test_ambiguous_registry_fails_closed(settings, tenants):
    write_registry(settings, tenants)
    with pytest.raises(ValueError):
        TenantRegistry(settings)


def test_rate_and_daily_quota_survive_restart_and_clock_rollback(tmp_path):
    now = [100.0]
    path = tmp_path / "quota.sqlite"
    tenant = Tenant("alpha", None, 60, 2, 3)
    store = QuotaStore(path, clock=lambda: now[0])
    store.admit(tenant)
    store.admit(tenant)
    with pytest.raises(QuotaExceeded):
        store.admit(tenant)
    now[0] += 1
    QuotaStore(path, clock=lambda: now[0]).admit(tenant)
    now[0] += 100
    with pytest.raises(QuotaExceeded):
        store.admit(tenant)
    now[0] = 0
    with pytest.raises(QuotaExceeded):
        store.admit(tenant)
    now[0] = 86400
    store.admit(tenant)
    store.admit(Tenant("beta", None, 60, 1, 1))


def test_quota_concurrent_admission_never_exceeds_limit(tmp_path):
    path = tmp_path / "quota.sqlite"
    stores = [QuotaStore(path, clock=lambda: 100) for _ in range(4)]
    tenant = Tenant("alpha", None, 60, 100, 7)

    def submit(i):
        try:
            stores[i % 4].admit(tenant)
            return True
        except (QuotaExceeded, QuotaUnavailable):
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        assert sum(pool.map(submit, range(40))) == 7


def test_quota_corruption_fails_closed(tmp_path):
    store = QuotaStore(tmp_path / "quota.sqlite")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TABLE quota")
    with pytest.raises(QuotaUnavailable):
        store.admit(Tenant("alpha", None, 60, 1, 10))


def test_previous_key_alone_does_not_enable_anonymous_access(settings):
    settings.previous_api_key = "previous-key"
    registry = TenantRegistry(settings)
    assert registry.authenticate(None) is None
    assert registry.authenticate(b"previous-key").id == "default"
