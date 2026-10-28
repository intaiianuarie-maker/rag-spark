"""Shared validation for workspace-owned persisted connector settings.

The API and worker must apply the same policy. Operator-owned environment
configs retain their separate loader; a workspace may read server paths only
beneath operator-approved roots. Errors never contain input payloads.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator


def approved_local_path(value: str) -> str:
    roots = [part.strip() for part in os.environ.get("CONNECTOR_LOCAL_ROOTS", "").split(",")
             if part.strip()]
    if not roots:
        raise ValueError("no approved local roots configured")
    try:
        candidate = Path(value).resolve()
        for root in roots:
            if candidate.is_relative_to(Path(root).resolve()):
                return str(candidate)
    except (OSError, ValueError, RuntimeError) as exc:
        raise ValueError("invalid local path") from exc
    raise ValueError("local path not under an approved root")


class LocalSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=512)

    @field_validator("path")
    @classmethod
    def contained(cls, value: str) -> str:
        return approved_local_path(value)


class WebSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    urls: list[str] = Field(min_length=1, max_length=50)
    allowed_origins: list[str] = Field(default_factory=list, max_length=50)
    politeness_delay: float | None = Field(default=None, ge=0, le=3600)
    politeness_delay_ms: int | None = Field(default=None, ge=0, le=3600000)
    max_redirects: int = Field(default=5, ge=0, le=20)

    @field_validator("urls", "allowed_origins")
    @classmethod
    def plain_http_urls(cls, values: list[str]) -> list[str]:
        from .adapters.web_connector import origin_of

        for value in values:
            try:
                origin = origin_of(value)
            except ValueError:
                origin = None
            if origin is None or any(ord(ch) < 32 for ch in value):
                raise ValueError("URL requires an http(s) host and no embedded credentials")
        return values


class S3Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bucket: str = Field(min_length=1, max_length=256)
    prefix: str = Field(default="", max_length=512)
    endpoint_ref: str | None = Field(default=None, max_length=256)

    @field_validator("endpoint_ref")
    @classmethod
    def supported_endpoint(cls, value: str | None) -> None:
        if value is not None:
            raise ValueError("endpoint_ref is not supported by this adapter")
        return None


class GitSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str = Field(min_length=1, max_length=2048)
    ref: str = Field(default="HEAD", max_length=128)

    @field_validator("repo")
    @classmethod
    def approved_repo(cls, value: str) -> str:
        from .adapters.git_connector import RepoScope, classify_repo, validate_remote_url

        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc not in ("", "localhost") or parsed.query or parsed.fragment:
                raise ValueError("file repository URL must name a local path")
            # Decode BEFORE containment, matching the actual Git adapter.
            return approved_local_path(unquote(parsed.path))
        if classify_repo(value) is RepoScope.REMOTE:
            try:
                return validate_remote_url(value)
            except ValueError as exc:
                raise ValueError("invalid repository URL or embedded credentials") from exc
        return approved_local_path(value)


SETTING_MODELS: dict[str, type[BaseModel]] = {
    "local_upload": LocalSettings,
    "web": WebSettings,
    "s3": S3Settings,
    "git": GitSettings,
}


def _approved_s3_source(workspace_id: str | None, settings: dict[str, Any]) -> None:
    from storage.originals import originals_bucket

    # The shared credentials used to store originals must never let a source
    # read another workspace's originals or recursively ingest its own writes.
    if settings["bucket"] == originals_bucket():
        raise ValueError("the document-originals bucket cannot be a source")
    try:
        policy = json.loads(os.environ.get("CONNECTOR_S3_SOURCES", "{}"))
    except (TypeError, ValueError):
        raise ValueError("invalid approved S3 source configuration") from None
    if not isinstance(policy, dict):
        raise ValueError("invalid approved S3 source configuration")
    scopes = policy.get(workspace_id, []) if workspace_id else []
    shared = policy.get("*", [])
    if not isinstance(scopes, list) or not isinstance(shared, list):
        raise ValueError("invalid approved S3 source configuration")
    for scope in scopes + shared:
        if (isinstance(scope, dict) and scope.get("bucket") == settings["bucket"]
                and isinstance(scope.get("prefix"), str)
                and settings.get("prefix", "").startswith(scope["prefix"])):
            return
    raise ValueError("S3 source is outside the operator-approved workspace scope")


def validate_settings(
    kind: str, settings: Any, *, workspace_id: str | None = None
) -> dict[str, Any]:
    model = SETTING_MODELS.get(kind)
    if model is None:
        raise ValueError("unsupported connector kind")
    try:
        validated = model.model_validate(settings).model_dump(mode="json", exclude_none=True)
    except ValidationError as exc:
        errors = exc.errors(include_input=False, include_context=False, include_url=False)
        # Our field validators use safe constants; omit rejected input/contexts.
        parts = []
        for error in errors:
            location = error["loc"]
            # Unknown field names are rejected input too, not safe metadata.
            field = location[0] if location and location[0] in model.model_fields else "settings"
            parts.append(f"{field}: {error['msg']}")
        # Pydantic's chained exception includes rejected input in its string.
        # A worker logs this error, so suppress that cause as well as omitting
        # input from the public message.
        raise ValueError("; ".join(parts) or "invalid settings") from None
    if kind == "s3":
        _approved_s3_source(workspace_id, validated)
    return validated
