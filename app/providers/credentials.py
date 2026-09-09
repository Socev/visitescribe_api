"""Provider credentials, kept out of the environment so they can be rotated
from the admin interface without redeploying the pod."""
from __future__ import annotations

import json
import os
from typing import Any

from .. import db
from ..util import now_iso


def _env_key(provider: str) -> str:
    return {"mistral": "VS_MISTRAL_API_KEY", "ourmind": "VS_OURMIND_TOKEN"}.get(
        provider, f"VS_{provider.upper()}_KEY"
    )


def get(provider: str) -> dict[str, Any] | None:
    row = db.query_one("SELECT * FROM provider_credentials WHERE provider = ?", (provider,))
    if row is not None:
        item = dict(row)
        try:
            item["meta"] = json.loads(item.pop("meta_json") or "{}")
        except (ValueError, TypeError):
            item["meta"] = {}
        return item
    fallback = os.environ.get(_env_key(provider))
    if fallback:
        return {"provider": provider, "kind": "api_key", "secret": fallback,
                "meta": {"source": "environment"}, "expires_at": None}
    return None


def secret(provider: str) -> str | None:
    entry = get(provider)
    return entry["secret"] if entry else None


def store(provider: str, secret_value: str, *, kind: str = "api_key",
          meta: dict | None = None, expires_at: str | None = None) -> None:
    db.execute(
        "INSERT INTO provider_credentials(provider, kind, secret, meta_json, "
        "expires_at, updated_at) VALUES(?,?,?,?,?,?) "
        "ON CONFLICT(provider) DO UPDATE SET kind = excluded.kind, "
        "secret = excluded.secret, meta_json = excluded.meta_json, "
        "expires_at = excluded.expires_at, updated_at = excluded.updated_at",
        (provider, kind, secret_value, json.dumps(meta or {}), expires_at, now_iso()),
    )


def forget(provider: str) -> None:
    db.execute("DELETE FROM provider_credentials WHERE provider = ?", (provider,))


def status() -> list[dict[str, Any]]:
    """Never returns a secret — only whether one exists and where it came from."""
    out = []
    for provider in ("mistral", "ourmind"):
        entry = get(provider)
        out.append({
            "provider": provider,
            "configured": entry is not None,
            "kind": entry["kind"] if entry else None,
            "source": (entry.get("meta", {}).get("source") or "admin") if entry else None,
            "expires_at": entry.get("expires_at") if entry else None,
            "hint": (entry["secret"][:4] + "…") if entry and entry["secret"] else None,
        })
    return out
