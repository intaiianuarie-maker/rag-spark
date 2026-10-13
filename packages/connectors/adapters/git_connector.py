"""Git source connector — pull tracked files from a repo ref (TASK-M6-001).

Fork-first (mirrors local/s3/web): same ``Connector`` contract, deterministic
discovery + paginated cursor, blob-hash integrity, tombstone-safe.

``git`` runs as a subprocess via an injectable ``runner`` seam, so the connector
is fully testable offline without a real repo. Refs default to ``HEAD``;
discovery lists blobs at the ref via ``git ls-tree -r -z`` and their blob SHAs
serve as a change signal. No credentials in argv — private repos authenticate
through the environment (e.g. ``GIT_SSH_COMMAND`` pointing at a deploy key),
never through ``settings``.

Local-vs-remote contract (T-V1-034, extended by T-V1-040)
---------------------------------------------------------
A source is classified by :func:`classify_repo`:

* **local** — an existing checkout. Read verbatim, byte-identically to the
  pre-T-V1-040 behaviour: ``ls-tree``/``cat-file`` only.
* **remote** — a URL. It is **never** handed to ``git -C`` unacquired.
  :meth:`GitConnector._acquire` validates it (:func:`validate_remote_url`,
  written for hostile input — scheme allowlist, no embedded credentials, no
  option-shaped values), clones it into a **fresh, bounded** scratch directory
  (shallow, single-branch, no checkout, no submodules; size- and time-capped),
  and passes the result through :func:`contain_path` so nothing outside the
  scratch root can be reached. Every failure path deletes the scratch and
  raises :class:`GitSourceRefused` — loud, typed, and never a partial sync.

Reuse: Git (GPL) is invoked as an external tool, not vendored source — no GPL
code is copied in-tree, so the license guard stays clean.
"""

from __future__ import annotations

import re
import tempfile
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

#: T-V1-034: the only git subcommands this adapter may ever run *against a
#: checkout it was handed*. ``ls-tree`` and ``cat-file`` are read-only and
#: local; ``clone``/``fetch``/``pull``/``ls-remote``/anything else is refused
#: before it can reach a process. ``clone`` is deliberately absent: it is
#: issued only by :meth:`GitConnector._clone`, which builds its own argv from a
#: validated URL and never takes one from a caller.
_ALLOWED_GIT_COMMANDS = frozenset({"ls-tree", "cat-file"})

#: T-V1-040 — schemes a *remote* source may use. Everything else is refused
#: rather than handed to git, because an unexpected scheme is how a source
#: config reaches a transport nobody reviewed (``ext::`` runs an arbitrary
#: command, ``file://`` reads local paths through a URL-shaped confused
#: deputy, ``fd::`` re-opens descriptors).
_ALLOWED_REMOTE_SCHEMES = frozenset({"https", "http", "ssh", "git"})

#: T-V1-040 — acquisition bounds. Each one closes a different door:
#:
#: * ``clone --depth 1``  → history is not transferred (a 10 GB repo costs one
#:   commit's objects), so a hostile remote cannot exhaust disk via history;
#: * ``clone --no-checkout`` → no working tree is materialised, so a repo full
#:   of hostile *paths* (device files, symlinks, hooks) is never unpacked —
#:   only ``.git`` objects exist, and ``ls-tree``/``cat-file`` read from those;
#: * ``no-recurse-submodules`` → a submodule cannot drag in a second,
#:   unreviewed repository (the classic ``file://``-submodule escape);
#: * ``MAX_ACQUIRED_BYTES`` → post-clone size ceiling on the scratch dir;
#:   over it, the clone is deleted and the source refused;
#: * ``CLONE_TIMEOUT_SECONDS`` + the HTTP low-speed env pair → a stalled or
#:   dripping remote aborts instead of holding the worker.
MAX_ACQUIRED_BYTES = 512 * 1024 * 1024  # 512 MiB of objects is a docs repo, not a dataset
CLONE_TIMEOUT_SECONDS = 300
#: Below these, git treats a transfer as stalled and aborts it (50 B/s for 60s).
_LOW_SPEED_LIMIT = 1024
_LOW_SPEED_TIME = 60

#: Every acquisition gets its own fresh directory under this root, so one
#: source can never observe or overwrite another source's clone.
SCRATCH_PREFIX = "git-connector-"


class GitSourceRefused(ValueError):
    """A git source was refused **loudly**, before any subprocess ran.

    Subclasses :class:`ValueError` so existing call sites and tests that catch
    ``ValueError`` keep working, while callers that want to distinguish "bad
    source" from "bad config" can catch this. Never raised after a partial
    sync: acquisition either yields a contained repo or raises.
    """


class RepoScope(StrEnum):
    """T-V1-034 Git local-vs-remote contract (pinned in code, not prose)."""

    LOCAL = "local"  # a checkout on this machine — readable offline
    REMOTE = "remote"  # an URL — acquired explicitly, never blind-passed


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


def validate_remote_url(url: str) -> str:
    """Validate an untrusted remote URL, or refuse it loudly (T-V1-040).

    A sync source's URL is **attacker-influenced input**, so this is written
    for a hostile caller. It returns the URL unchanged when safe and raises
    :class:`GitSourceRefused` otherwise — there is no "best effort" branch:

    * scheme must be in :data:`_ALLOWED_REMOTE_SCHEMES` (``ext::`` executes an
      arbitrary command, ``file://`` reads local paths through a URL-shaped
      confused deputy, ``fd::`` re-opens descriptors);
    * **no embedded credentials** — ``https://user:pass@host`` would put the
      secret in argv and in ``ps``. Private repos authenticate through the
      environment, exactly as this module's header already states;
    * no leading ``-`` and no whitespace/control characters, so the value can
      never be read by git as an option or split into two arguments;
    * scp-style ``git@host:path`` is accepted only in its canonical shape and
      only with a plain hostname (no ``..``, no absolute path in the host).
    """
    value = url.strip()
    if not value:
        raise GitSourceRefused("remote URL is empty")
    if value != url or any(ch.isspace() or ord(ch) < 32 for ch in value):
        raise GitSourceRefused("remote URL contains whitespace or control characters")
    if value.startswith("-"):
        raise GitSourceRefused("remote URL must not start with '-' (git option injection)")

    if _SCP_RE.match(value):
        host = value.split("@", 1)[1].split(":", 1)[0]
        if not re.fullmatch(r"[A-Za-z0-9._-]+", host):
            raise GitSourceRefused(f"scp-style remote has an unusable host: {host!r}")
        return value

    parsed = urlparse(value)
    scheme = (parsed.scheme or "").lower()
    if scheme not in _ALLOWED_REMOTE_SCHEMES:
        raise GitSourceRefused(
            f"remote URL scheme {scheme or '(none)'!r} is not allowed; "
            f"one of {sorted(_ALLOWED_REMOTE_SCHEMES)} only"
        )
    # A password never belongs in a URL — it would land in argv and in ``ps``.
    # Over http(s) a *username* is treated the same way, because that position
    # is where deploy tokens are pasted (``https://<token>@host``); over ssh/git
    # a username is the canonical, secret-free form (``ssh://git@host``) and is
    # allowed.
    if parsed.password is not None:
        raise GitSourceRefused(
            "remote URLs must not embed credentials — authenticate via the "
            "environment (e.g. GIT_SSH_COMMAND or a credential helper), never argv"
        )
    if scheme in ("http", "https") and parsed.username is not None:
        raise GitSourceRefused(
            "http(s) remote URLs must not carry userinfo — that position is "
            "where tokens get pasted; authenticate via the environment instead"
        )
    if not parsed.hostname:
        raise GitSourceRefused("remote URL has no host")
    return value


def contain_path(root: str, candidate: str) -> str:
    """Return ``candidate`` only if it resolves **inside** ``root`` (T-V1-040).

    Containment is decided on the *resolved* path, so it is immune to every
    trick that works on the string form: ``..`` segments, an absolute path
    spliced in, a symlink whose target sits outside the root, or a ``root``
    that is itself a symlink. A refusal is a :class:`GitSourceRefused`, never
    a silent truncation — a path we cannot prove contained is a path we do not
    touch.
    """
    import os
    from pathlib import Path

    real_root = Path(root).resolve()
    resolved = Path(candidate).resolve()
    try:
        resolved.relative_to(real_root)
    except ValueError as exc:
        raise GitSourceRefused(
            f"path escapes its root: {candidate!r} resolves to {resolved}, "
            f"outside {real_root}"
        ) from exc
    if os.path.islink(str(Path(candidate))) and resolved != Path(candidate).resolve():
        raise GitSourceRefused(f"path is a symlink outside its root: {candidate!r}")
    return str(resolved)


class GitConnector(Connector):
    """Read files tracked in a git repo at a given ref."""

    kind = ConnectorKind.GIT

    def __init__(
        self,
        *,
        page_size: int = 500,
        runner: GitRunner | None = None,
        scratch_root: str | None = None,
        cloner: Callable[[str, str], None] | None = None,
    ) -> None:
        if page_size < 1:
            raise ValueError(f"page_size must be >= 1, got {page_size}")
        self._page_size = page_size
        self._runner = runner
        #: Where remote sources are acquired. ``None`` ⇒ the system temp dir.
        self._scratch_root = scratch_root or tempfile.gettempdir()
        #: Injectable acquisition seam (tests clone a local bare repo offline).
        self._cloner = cloner

    def _repo(self, config: ConnectorConfig) -> str:
        repo = config.settings.get("repo") or config.settings.get("url")
        if not repo:
            raise GitSourceRefused("GitConnector requires settings.repo (path or URL)")
        value = str(repo)
        if classify_repo(value) is RepoScope.REMOTE:
            # T-V1-040: a remote is acquired EXPLICITLY into bounded scratch,
            # then contained. It is never passed to ``git -C`` as-is — that is
            # the whole point (an unacquired URL would either fail deep in git
            # or operate on whatever happens to sit at that string).
            return self._acquire(value)
        if value.startswith("file://"):
            parsed = urlparse(value)
            if parsed.netloc not in ("", "localhost"):
                raise GitSourceRefused("file:// repository URLs must be local (no host)")
            value = unquote(parsed.path)
        # Local checkout: unchanged from the pre-T-V1-040 contract — an
        # operator's own path, returned verbatim (byte-identical behaviour).
        return value

    # --- T-V1-040: explicit, bounded acquisition of a remote source ----------

    def _scratch_dir(self) -> str:
        return tempfile.mkdtemp(prefix=SCRATCH_PREFIX, dir=self._scratch_root)

    def _clone(self, url: str, dest: str) -> None:
        """Run the one ``git clone`` this adapter is ever allowed to run.

        The argv is built here from a *validated* URL and never from a caller,
        and ``--`` precedes the URL so a value that survived validation still
        cannot be read as an option. Read-side commands stay behind
        :data:`_ALLOWED_GIT_COMMANDS`; this is the only place ``clone`` exists.
        """
        if self._cloner is not None:
            self._cloner(url, dest)
            return
        import os
        import subprocess

        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")  # never block on auth
        # ``GIT_TERMINAL_PROMPT=0`` only tames git's *own* credential prompt.
        # An ssh transport would still sit on host-key confirmation ("yes/no?")
        # or a key passphrase and block the worker — BatchMode makes ssh fail
        # fast instead, and accept-new trusts a first-seen host without a
        # prompt. An operator's own ``GIT_SSH_COMMAND`` (deploy key, jump host)
        # wins, because setdefault never overrides it.
        # ConnectTimeout bounds the case that BatchMode alone does not: an
        # egress blackhole (SYN silently dropped) would otherwise hold the
        # worker for ssh's own multi-minute default, and then for the whole
        # clone timeout on top.
        env.setdefault(
            "GIT_SSH_COMMAND",
            "ssh -o BatchMode=yes -o StrictHostKeyChecking=accept-new "
            "-o ConnectTimeout=10",
        )
        env["GIT_HTTP_LOW_SPEED_LIMIT"] = str(_LOW_SPEED_LIMIT)
        env["GIT_HTTP_LOW_SPEED_TIME"] = str(_LOW_SPEED_TIME)
        subprocess.run(
            [
                "git",
                "clone",
                "--depth",
                "1",
                "--single-branch",
                "--no-checkout",
                "--no-recurse-submodules",
                "--",
                url,
                dest,
            ],
            check=True,
            capture_output=True,
            stdin=subprocess.DEVNULL,  # nothing may ever read a prompt
            env=env,
            timeout=CLONE_TIMEOUT_SECONDS,
        )

    def _acquire(self, url: str) -> str:
        """Validate → clone into a fresh scratch dir → bound → contain.

        Raises :class:`GitSourceRefused` at every failure point and removes the
        scratch directory on the way out, so a refused source leaves **no**
        partial clone behind for a later run to pick up.
        """
        safe_url = validate_remote_url(url)
        dest = self._scratch_dir()
        try:
            self._clone(safe_url, dest)
            self._assert_bounded(dest)
        except GitSourceRefused:
            import shutil

            shutil.rmtree(dest, ignore_errors=True)
            raise
        except Exception as exc:
            import shutil

            shutil.rmtree(dest, ignore_errors=True)
            raise GitSourceRefused(
                f"acquiring {safe_url!r} failed: {type(exc).__name__}"
            ) from exc
        return contain_path(self._scratch_root, dest)

    @staticmethod
    def _assert_bounded(dest: str) -> None:
        """Refuse an acquisition larger than :data:`MAX_ACQUIRED_BYTES`."""
        import os

        total = 0
        for root, _dirs, files in os.walk(dest):
            for name in files:
                total += os.path.getsize(os.path.join(root, name))
                if total > MAX_ACQUIRED_BYTES:
                    raise GitSourceRefused(
                        f"acquired repo exceeds {MAX_ACQUIRED_BYTES} bytes — refusing"
                    )

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
