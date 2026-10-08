"""Git source connector — pull tracked files from a repo ref (TASK-M6-001).

Fork-first (mirrors local/s3/web): same ``Connector`` contract, deterministic
discovery + paginated cursor, blob-hash integrity, tombstone-safe.

``git`` runs as a subprocess via an injectable ``runner`` seam, so the connector
is fully testable offline without a real repo. Refs default to ``HEAD``;
discovery lists blobs at the ref via ``git ls-tree -r -z`` and their blob SHAs
serve as a change signal. No credentials in argv — private repos authenticate
through the environment (e.g. ``GIT_SSH_COMMAND`` pointing at a deploy key),
never through ``settings``.

Reuse: Git (GPL) is invoked as an external tool, not vendored source — no GPL
code is copied in-tree, so the license guard stays clean.
"""

from __future__ import annotations

from collections.abc import Callable

from ..interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
)

#: A git seam: given (args, repo) return raw stdout bytes. Injectable for tests.
GitRunner = Callable[[list[str], str], bytes]


class GitConnector(Connector):
    """Read files tracked in a git repo at a given ref."""

    kind = ConnectorKind.GIT

    def __init__(self, *, page_size: int = 500, runner: GitRunner | None = None) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        self._page_size = page_size
        self._runner = runner

    def _repo(self, config: ConnectorConfig) -> str:
        repo = config.settings.get("repo") or config.settings.get("url")
        if not repo:
            raise ValueError("GitConnector requires settings.repo (path or URL)")
        return str(repo)

    def _ref(self, config: ConnectorConfig) -> str:
        return str(config.settings.get("ref", "HEAD"))

    def _run(self, args: list[str], repo: str) -> bytes:
        if self._runner is not None:
            return self._runner(args, repo)
        import os
        import subprocess

        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on auth
        proc = subprocess.run(
            ["git", *args, repo],
            check=True,
            capture_output=True,
            env=env,
            timeout=60,
        )
        return proc.stdout

    def _list_tree(self, config: ConnectorConfig) -> list[SourceItem]:
        repo = self._repo(config)
        ref = self._ref(config)
        out = self._run(["ls-tree", "-r", "-z", ref], repo).decode("utf-8", "replace")
        items: list[SourceItem] = []
        for record in out.split("\0"):
            if not record:
                continue
            # format: "<mode> <type> <sha>\t<path>"
            meta, _, path = record.partition("\t")
            parts = meta.split()
            if len(parts) < 3 or parts[1] != "blob":
                continue  # skip trees/submodules
            _mode, _type, sha = parts
            items.append(
                SourceItem(
                    external_id=path,
                    name=path.rsplit("/", 1)[-1],
                    uri=path,
                    metadata={"sha": sha, "ref": ref, "repo": repo},
                )
            )
        return items

    def _show_blob(self, config: ConnectorConfig, sha: str) -> bytes:
        return self._run(["cat-file", "blob", sha], self._repo(config))

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        items = self._list_tree(config)
        start = int(cursor) if cursor else 0
        window = items[start : start + self._page_size]
        next_start = start + self._page_size
        next_cursor = str(next_start) if next_start < len(items) else None
        return ConnectorDiscovery(items=window, next_cursor=next_cursor)

    async def fetch(self, config: ConnectorConfig, item: SourceItem) -> bytes:
        return self._show_blob(config, str(item.metadata.get("sha", "")))

    async def sync(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorSyncResult:
        discovery = await self.discover(config, cursor)
        fetched = 0
        errors: list[str] = []
        for item in discovery.items:
            try:
                content = self._show_blob(config, str(item.metadata.get("sha", "")))
                # Integrity: git blob id is sha1("blob <len>\0" + content).
                blob_hash = _git_blob_hash(content)
                expected = str(item.metadata.get("sha", ""))
                if expected and not blob_hash.startswith(expected[:7]):
                    errors.append(f"{item.external_id}: blob hash mismatch")
                    continue
                fetched += 1
            except Exception as exc:
                errors.append(f"{item.external_id}: {exc}")
        return ConnectorSyncResult(
            discovered=len(discovery.items),
            fetched=fetched,
            next_cursor=discovery.next_cursor,
            errors=errors,
        )


def _git_blob_hash(content: bytes) -> str:
    """Reproduce git's blob object id (sha1) for integrity checks."""
    import hashlib

    header = f"blob {len(content)}\0".encode()
    return hashlib.sha1(header + content).hexdigest()
