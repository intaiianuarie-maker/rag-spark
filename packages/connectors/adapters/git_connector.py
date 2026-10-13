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

import re
from collections.abc import Callable
from enum import StrEnum
from urllib.parse import unquote, urlparse

from ..interfaces import (
    Connector,
    ConnectorConfig,
    ConnectorDiscovery,
    ConnectorKind,
    ConnectorSyncResult,
    SourceItem,
    page_items,
    snapshot_digest,
)

#: A git seam: given (args, repo) return raw stdout bytes. Injectable for tests.
GitRunner = Callable[[list[str], str], bytes]

#: T-V1-034: the only git subcommands this adapter may ever run. ``ls-tree``
#: and ``cat-file`` are read-only and local; ``clone``/``fetch``/``pull``/
#: ``ls-remote``/anything else is refused before it can reach a process.
_ALLOWED_GIT_COMMANDS = frozenset({"ls-tree", "cat-file"})


class RepoScope(StrEnum):
    """T-V1-034 Git local-vs-remote contract (pinned in code, not prose)."""

    LOCAL = "local"  # a checkout on this machine — readable offline
    REMOTE = "remote"  # an URL — never contacted by this connector


#: ``file://`` is a local path wearing a scheme; every other scheme is remote.
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*://")
#: scp-style remote shorthand: ``git@host:path`` (no scheme, has a colon).
_SCP_RE = re.compile(r"^[^/@\s]+@[^/\s]+:.+")


def classify_repo(repo: str) -> RepoScope:
    """Classify ``settings.repo`` as a local checkout or a remote URL.

    Contract: this connector is a **reader of an existing local checkout**.
    It runs ``ls-tree``/``cat-file`` only — never ``clone``, ``fetch``,
    ``pull`` or ``ls-remote`` — so a remote URL can only ever be *read* if the
    objects are already on disk. Anything with a URL scheme (except
    ``file://``) or scp syntax is REMOTE and is rejected at the boundary
    rather than silently handed to ``git -C``, which would fail with an
    unrelated error (or, worse, a later command could dial the network).
    """
    value = repo.strip()
    if value.startswith("file://"):
        return RepoScope.LOCAL
    if _SCHEME_RE.match(value) or _SCP_RE.match(value):
        return RepoScope.REMOTE
    return RepoScope.LOCAL


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
        value = str(repo)
        if classify_repo(value) is RepoScope.REMOTE:
            raise ValueError(
                "GitConnector reads a LOCAL checkout and never performs network "
                "operations (no clone/fetch/pull); point settings.repo at a "
                "local clone of the remote repository"
            )
        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc not in ("", "localhost"):
                raise ValueError("file:// repository URLs must be local (no host)")
            value = unquote(parsed.path)
        return value

    def _ref(self, config: ConnectorConfig) -> str:
        return str(config.settings.get("ref", "HEAD"))

    def _run(self, args: list[str], repo: str) -> bytes:
        if not args or args[0] not in _ALLOWED_GIT_COMMANDS:
            # T-V1-034: this connector is a reader of a local checkout. Every
            # subcommand it can issue is in this set, so ``clone``/``fetch``/
            # ``pull``/``ls-remote`` (anything that could dial the network or
            # mutate the repo) can never be reached, even via a bad ref.
            raise ValueError(
                f"git subcommand not allowed in GitConnector: {args[:1] or args!r}"
            )
        if self._runner is not None:
            return self._runner(args, repo)
        import os
        import subprocess

        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on auth
        cmd = ["git", "-C", repo, *args] if repo else ["git", *args]
        proc = subprocess.run(
            cmd,
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
        # Key order (T-V1-034 keyset paging): ``ls-tree -r`` is tree-ordered,
        # which we normalize to plain ``external_id`` order so a resume never
        # depends on directory-entry layout.
        items.sort(key=lambda item: item.external_id)
        return items

    def _show_blob(self, config: ConnectorConfig, sha: str) -> bytes:
        return self._run(["cat-file", "blob", sha], self._repo(config))

    async def discover(
        self, config: ConnectorConfig, cursor: str | None = None
    ) -> ConnectorDiscovery:
        items = self._list_tree(config)
        window, next_cursor = page_items(items, cursor, self._page_size)
        return ConnectorDiscovery(
            items=window,
            next_cursor=next_cursor,
            snapshot=snapshot_digest(items),
        )

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
