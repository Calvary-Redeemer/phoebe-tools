"""Thin httpx wrapper around the Planning Center Services JSON-API.

Every PCO response is a JSON-API envelope:

    {
      "data": <resource> | [<resource>, ...],
      "included": [<resource>, ...],   # side-loaded via ?include=
      "meta": {"total_count": ..., "count": ..., "can_include": [...]}
    }

where each <resource> is {"type", "id", "attributes", "relationships", "links"}.

That envelope is verbose and burns context if handed to an LLM raw, so this
module centralizes two jobs: making authenticated requests, and *flattening*
resources into compact dicts ({id, type, **attributes, _rel}) with related
names resolved from the `included` array.
"""

from __future__ import annotations

from typing import Any

import httpx

from .config import Settings, load_settings

# Attribute names we try, in order, to give a related resource a human label.
_NAME_KEYS = ("name", "full_name", "title", "first_name")

ResourceList = list[dict[str, Any]]
IncludedIndex = dict[tuple[str, str], dict[str, Any]]


class PCOError(RuntimeError):
    """Raised when the PCO API returns a non-success status.

    Surfaces the JSON-API error body (title/detail) so the model and user see
    *why* a call failed rather than a bare status code.
    """


class PCOClient:
    """Synchronous client for the PCO Services v2 API."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or load_settings()
        self._client = httpx.Client(
            base_url=self.settings.base_url,
            auth=(self.settings.app_id, self.settings.secret),
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "pco-mcp/0.1 (local MCP server)",
            },
            timeout=30.0,
        )

    def close(self) -> None:
        self._client.close()

    # -- raw request ------------------------------------------------------

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make one request and return the parsed JSON body (or {} for 204)."""
        resp = self._client.request(
            method,
            path,
            params=_clean_params(params),
            json=json,
        )
        if resp.is_error:
            raise PCOError(_format_error(resp))
        if resp.status_code == 204 or not resp.content:
            return {}
        return resp.json()

    # -- collections & flattening ----------------------------------------

    def get_collection(
        self, path: str, params: dict[str, Any] | None = None
    ) -> tuple[ResourceList, dict[str, Any]]:
        """GET a list endpoint; return (flattened_resources, meta)."""
        body = self.request("GET", path, params=params)
        index = build_included_index(body.get("included", []))
        data = body.get("data", [])
        resources = [flatten(r, index) for r in data]
        return resources, body.get("meta", {})

    def get_one(
        self, path: str, params: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        """GET a single-resource endpoint; return the flattened resource."""
        body = self.request("GET", path, params=params)
        index = build_included_index(body.get("included", []))
        return flatten(body.get("data", {}), index)

    def paginate(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        *,
        limit: int = 100,
        page_size: int = 100,
    ) -> ResourceList:
        """Walk `offset` pages until `limit` resources are collected or exhausted."""
        params = dict(params or {})
        collected: ResourceList = []
        offset = 0
        while len(collected) < limit:
            params["per_page"] = min(page_size, limit - len(collected))
            params["offset"] = offset
            page, meta = self.get_collection(path, params)
            collected.extend(page)
            pagination = meta.get("pagination") or {}
            next_offset = pagination.get("next", {}).get("offset")
            if not page or next_offset is None:
                break
            offset = next_offset
        return collected[:limit]


# -- module-level helpers (pure, easy to test) ---------------------------


def build_included_index(included: list[dict[str, Any]]) -> IncludedIndex:
    """Index side-loaded resources by (type, id) for relationship resolution."""
    return {
        (item.get("type", ""), item.get("id", "")): item
        for item in included
        if item.get("id")
    }


def _label_for(resource: dict[str, Any]) -> str | None:
    """Best human-readable label for a resource from its attributes."""
    attrs = resource.get("attributes", {})
    for key in _NAME_KEYS:
        if attrs.get(key):
            return str(attrs[key])
    return None


def flatten(resource: dict[str, Any], index: IncludedIndex) -> dict[str, Any]:
    """Collapse a JSON-API resource into a compact, LLM-friendly dict.

    Output shape:
        {"id", "type", <attributes...>, "_rel": {name: {"id", "type", "name"?}}}

    For each to-one relationship, the related id/type is recorded, and if that
    resource was side-loaded (via ?include=) its label is attached too.
    """
    if not resource:
        return {}
    flat: dict[str, Any] = {"id": resource.get("id"), "type": resource.get("type")}
    flat.update(resource.get("attributes", {}))

    rels: dict[str, Any] = {}
    for name, rel in (resource.get("relationships") or {}).items():
        data = rel.get("data")
        if isinstance(data, dict) and data.get("id"):
            ref = {"id": data["id"], "type": data.get("type")}
            included = index.get((data.get("type", ""), data["id"]))
            if included:
                label = _label_for(included)
                if label:
                    ref["name"] = label
            rels[name] = ref
        elif isinstance(data, list):
            rels[name] = [
                {"id": d.get("id"), "type": d.get("type")}
                for d in data
                if d.get("id")
            ]
    if rels:
        flat["_rel"] = rels
    return flat


def _clean_params(params: dict[str, Any] | None) -> dict[str, Any] | None:
    """Drop None values so optional query params are simply omitted."""
    if not params:
        return None
    return {k: v for k, v in params.items() if v is not None}


def _format_error(resp: httpx.Response) -> str:
    """Build a readable message from a JSON-API error response."""
    prefix = f"PCO API {resp.status_code} for {resp.request.method} {resp.request.url}"
    try:
        body = resp.json()
    except ValueError:
        return f"{prefix}: {resp.text[:500]}"
    errors = body.get("errors")
    if isinstance(errors, list) and errors:
        parts = []
        for err in errors:
            title = err.get("title") or ""
            detail = err.get("detail") or ""
            parts.append(": ".join(p for p in (title, detail) if p) or str(err))
        return f"{prefix}: " + " | ".join(parts)
    return f"{prefix}: {body}"
