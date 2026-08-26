"""Credential resolution.

Credentials are injected at the runtime boundary and never enter model context:
the explorer works against a session the runtime opened, and the distiller only
ever sees the field *name* a value came from, never the value.

Secret Manager when a project is configured; environment otherwise.
"""

from __future__ import annotations

import json
import os
import stat
from functools import lru_cache
from pathlib import Path

CREDENTIAL_FIELDS = ("username", "password")

# Local fallback when there is no Secret Manager. Owner-readable only, and
# never returned to any code path that reaches a prompt.
LOCAL_STORE = Path(os.getenv("CLICKWRIGHT_HOME", "var")) / "secrets.json"


def _slug(connector_id: str) -> str:
    return connector_id.replace("-", "_")


def _read_local() -> dict[str, str]:
    if not LOCAL_STORE.exists():
        return {}
    try:
        return json.loads(LOCAL_STORE.read_text())
    except (OSError, ValueError):
        return {}


@lru_cache(maxsize=32)
def _secret(name: str) -> str | None:
    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    if project:
        try:
            from google.cloud import secretmanager

            client = secretmanager.SecretManagerServiceClient()
            path = f"projects/{project}/secrets/{name}/versions/latest"
            return client.access_secret_version(name=path).payload.data.decode()
        except Exception:
            pass
    return os.getenv(name.upper()) or _read_local().get(name)


def store_credentials(connector_id: str, username: str | None, password: str | None) -> bool:
    """Save credentials for a target the operator just added.

    Secret Manager when a project is configured, an owner-only file otherwise.
    Returns whether anything was stored. The values are never logged, never
    echoed back over the API, and never placed in model context — the runtime
    injects them into the browser directly.
    """
    supplied = {"username": username, "password": password}
    supplied = {k: v for k, v in supplied.items() if v}
    if not supplied:
        return False

    project = os.getenv("GOOGLE_CLOUD_PROJECT")
    for field, value in supplied.items():
        name = f"{_slug(connector_id)}_{field}"
        if project:
            _store_in_secret_manager(project, name, value)
        else:
            store = _read_local()
            store[name] = value
            LOCAL_STORE.parent.mkdir(parents=True, exist_ok=True)
            LOCAL_STORE.write_text(json.dumps(store, indent=2))
            LOCAL_STORE.chmod(stat.S_IRUSR | stat.S_IWUSR)
        _secret.cache_clear()
    return True


def _store_in_secret_manager(project: str, name: str, value: str) -> None:
    from google.cloud import secretmanager

    client = secretmanager.SecretManagerServiceClient()
    parent = f"projects/{project}"
    try:
        client.create_secret(
            parent=parent,
            secret_id=name,
            secret={"replication": {"automatic": {}}},
        )
    except Exception:
        pass  # already exists; add a new version below
    client.add_secret_version(
        parent=f"{parent}/secrets/{name}", payload={"data": value.encode()}
    )


def resolve_credentials(connector_id: str) -> dict[str, str]:
    """Returns {username, password} for a connector, or {} if it needs none."""
    slug = _slug(connector_id)
    resolved = {}
    for field in CREDENTIAL_FIELDS:
        value = _secret(f"{slug}_{field}")
        if value:
            resolved[field] = value
    return resolved


def redact_for_log(values: dict) -> dict:
    """Never log a credential, even by accident."""
    return {
        k: ("••••••" if k in CREDENTIAL_FIELDS or "pass" in k.lower() else v)
        for k, v in values.items()
    }
