"""T-V1-040 — the git connector must acquire remotes, never blind-``git -C`` them.

RP-14/QF-14: the git sync path used to hand a source straight to
``git -C <path>``. A remote URL given where a local clone was expected would
either fail deep inside git or — worse — operate on whatever already sat at
that string. This suite pins the three properties that close it:

1. **unacquired ``git -C`` on URLs: 0** — a REMOTE source is validated,
   cloned into bounded scratch, and only the *scratch* path ever reaches
   ``git -C``; with no acquisition possible the source is refused loudly and
   no subprocess runs at all.
2. **containment bypasses: 0** — the acquired path is decided on its
   *resolved* form, so ``..``, absolute escapes and symlinks cannot leave the
   scratch root.
3. **local paths still work** — returned byte-identically, read with the same
   read-only subcommands as before.

No network: the "remote" is a real local git repository whose *acquisition* is
faked by an injected cloner, while ``ls-tree``/``cat-file`` run against **real
git**, so the end-to-end sync path is genuinely exercised.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from connectors import ConnectorConfig, ConnectorKind
from connectors.adapters.git_connector import (
    MAX_ACQUIRED_BYTES,
    SCRATCH_PREFIX,
    GitConnector,
    GitSourceRefused,
    RepoScope,
    classify_repo,
    contain_path,
    validate_remote_url,
)

pytestmark = pytest.mark.unit


def _config(repo: str, **extra: str) -> ConnectorConfig:
    return ConnectorConfig(
        connector_id="c1",
        workspace_id="w1",
        kind=ConnectorKind.GIT,
        settings={"repo": repo, **extra},
    )


# --------------------------------------------------------------------------------------
# 1. URL validation — untrusted input, hostile by assumption
# --------------------------------------------------------------------------------------

HOSTILE_URLS = [
    ("ext::sh -c whoami", "command transport"),
    ("file:///etc", "URL-shaped local read"),
    ("fd::3", "descriptor re-open"),
    ("git+ssh://example.com/r.git", "unreviewed scheme"),
    ("https://user:secret@example.com/r.git", "credential in argv"),
    ("https://secret@example.com/r.git", "token-shaped userinfo"),
    ("-ouploadpack=touch /tmp/x", "option injection"),
    ("https://example.com/r with space", "argument splitting"),
    ("https://example.com/r\nother", "control character"),
    ("https:///repo.git", "no host"),
    ("git@../../tmp:repo.git", "scp host with traversal"),
    ("", "empty"),
]


@pytest.mark.parametrize(
    ("url", "why"), HOSTILE_URLS, ids=[u[0] or "<empty>" for u in HOSTILE_URLS]
)
def test_hostile_remote_urls_are_refused_loudly(url: str, why: str) -> None:
    """Every hostile shape raises the typed error — never a best-effort clone."""
    with pytest.raises(GitSourceRefused) as excinfo:
        validate_remote_url(url)
    assert isinstance(excinfo.value, ValueError)  # old call sites keep working


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/repo.git",
        "http://example.com/repo.git",
        "ssh://git@example.com/repo.git",
        "git://example.com/repo.git",
        "git@example.com:team/repo.git",
    ],
)
def test_legitimate_remote_urls_are_accepted(url: str) -> None:
    assert validate_remote_url(url) == url


def test_file_urls_are_local_not_remote() -> None:
    """``file://`` is a local path wearing a scheme — never a network source."""
    assert classify_repo("file:///srv/repo") is RepoScope.LOCAL
    with pytest.raises(GitSourceRefused):
        validate_remote_url("file:///srv/repo")


# --------------------------------------------------------------------------------------
# 2. Containment — decided on the resolved path
# --------------------------------------------------------------------------------------


def test_containment_allows_a_path_inside_the_root(tmp_path: Path) -> None:
    root = tmp_path / "scratch"
    (root / "repo").mkdir(parents=True)
    assert contain_path(str(root), str(root / "repo")) == str((root / "repo").resolve())


@pytest.mark.parametrize(
    "shape",
    ["../outside", "repo/../../outside", "/etc", "/"],
    ids=["dotdot", "nested-dotdot", "absolute", "root"],
)
def test_containment_refuses_every_escape(tmp_path: Path, shape: str) -> None:
    root = tmp_path / "scratch"
    root.mkdir()
    with pytest.raises(GitSourceRefused) as excinfo:
        contain_path(str(root), str(root / shape))
    assert "escapes its root" in str(excinfo.value)


def test_containment_refuses_a_symlink_pointing_outside(tmp_path: Path) -> None:
    """A symlink is judged by where it GOES, not by what it is called."""
    root = tmp_path / "scratch"
    root.mkdir()
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    link = root / "innocent"
    link.symlink_to(outside, target_is_directory=True)

    with pytest.raises(GitSourceRefused):
        contain_path(str(root), str(link))


def test_containment_survives_a_symlinked_root(tmp_path: Path) -> None:
    """A root that is itself a symlink still contains its real children."""
    real = tmp_path / "real-root"
    (real / "repo").mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real, target_is_directory=True)

    assert contain_path(str(alias), str(alias / "repo")) == str((real / "repo").resolve())


# --------------------------------------------------------------------------------------
# 3. Unacquired git -C on URLs: 0
# --------------------------------------------------------------------------------------


def _recording_connector(**kwargs):
    """A connector whose read-path runner records every (args, repo) it sees."""
    seen: list[tuple[list[str], str]] = []

    def runner(args: list[str], repo: str) -> bytes:
        seen.append((args, repo))
        return b""

    return GitConnector(runner=runner, **kwargs), seen


async def test_a_remote_source_never_reaches_git_c_unacquired(tmp_path: Path) -> None:
    """The URL is acquired first; ``git -C`` only ever sees the scratch path."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    connector, seen = _recording_connector(scratch_root=str(scratch))

    def fake_cloner(url: str, dest: str) -> None:
        assert url == "https://example.com/repo.git"  # the VALIDATED url
        Path(dest, ".git").mkdir(parents=True)

    connector._cloner = fake_cloner  # type: ignore[attr-defined]

    await connector.discover(_config("https://example.com/repo.git"))

    assert seen, "the read path must have run"
    _args, repo = seen[0]
    assert repo != "https://example.com/repo.git", "an UNACQUIRED url reached git -C"
    assert repo.startswith(str(scratch.resolve())), "the repo must be inside the scratch root"
    assert SCRATCH_PREFIX in repo


async def test_with_no_acquisition_available_the_source_is_refused(tmp_path: Path) -> None:
    """No cloner ⇒ refuse loudly; no subprocess, no partial state."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    connector, seen = _recording_connector(scratch_root=str(scratch))

    with pytest.raises(GitSourceRefused) as excinfo:
        await connector.discover(_config("https://example.com/repo.git"))

    assert seen == [], "a refused source must never reach a subprocess"
    assert "acquiring" in str(excinfo.value)
    # The scratch root is left empty — no partial clone to trip over later.
    assert list(scratch.iterdir()) == []


def test_the_clone_argv_is_bounded_and_option_safe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The one clone this adapter runs is capped, non-interactive and `--`-guarded."""
    import subprocess

    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["cmd"] = list(cmd)
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    connector = GitConnector()
    connector._clone("https://example.com/repo.git", "/tmp/dest")

    cmd = recorded["cmd"]  # type: ignore[assignment]
    assert cmd[:2] == ["git", "clone"]
    assert "--depth" in cmd and cmd[cmd.index("--depth") + 1] == "1", "history must not transfer"
    assert "--no-checkout" in cmd, "no working tree may be materialised"
    assert "--no-recurse-submodules" in cmd, "no submodule may pull a second repo"
    assert cmd[-3] == "--", "the URL must be option-guarded"
    assert cmd[-2:] == ["https://example.com/repo.git", "/tmp/dest"]

    kwargs = recorded["kwargs"]  # type: ignore[assignment]
    assert kwargs["timeout"] > 0, "the clone must be time-bounded"
    assert kwargs["env"]["GIT_TERMINAL_PROMPT"] == "0", "never block on auth"
    assert int(kwargs["env"]["GIT_HTTP_LOW_SPEED_LIMIT"]) > 0, "a stalled transfer must abort"


def test_acquisition_can_never_block_on_a_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression (T-V1-040): an ``ssh://`` source must fail fast, not hang.

    ``GIT_TERMINAL_PROMPT=0`` only tames git's own prompt — an ssh transport
    would still sit on host-key confirmation or a key passphrase and block the
    worker for the whole clone timeout. BatchMode + a closed stdin close both.
    """
    import subprocess

    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.delenv("GIT_SSH_COMMAND", raising=False)
    GitConnector()._clone("ssh://git@example.com/repo.git", "/tmp/dest")

    kwargs = recorded["kwargs"]  # type: ignore[assignment]
    assert kwargs["stdin"] is subprocess.DEVNULL, "nothing may read a prompt"
    ssh = kwargs["env"]["GIT_SSH_COMMAND"]
    assert "BatchMode=yes" in ssh, "ssh must never prompt for a passphrase"
    assert "StrictHostKeyChecking" in ssh, "ssh must never prompt for host keys"


def test_an_operator_ssh_command_is_not_overridden(monkeypatch: pytest.MonkeyPatch) -> None:
    """A deploy key / jump host configured by the operator must survive."""
    import subprocess

    recorded: dict[str, object] = {}

    def fake_run(cmd, **kwargs):
        recorded["kwargs"] = kwargs
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setenv("GIT_SSH_COMMAND", "ssh -i /keys/deploy")
    GitConnector()._clone("ssh://git@example.com/repo.git", "/tmp/dest")

    kwargs = recorded["kwargs"]  # type: ignore[assignment]
    assert kwargs["env"]["GIT_SSH_COMMAND"] == "ssh -i /keys/deploy"


async def test_an_oversized_acquisition_is_refused_and_removed(tmp_path: Path) -> None:
    """The size bound bites, and a refused clone leaves nothing behind."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    connector = GitConnector(scratch_root=str(scratch))

    def greedy_cloner(url: str, dest: str) -> None:
        big = Path(dest, "big.bin")
        big.write_bytes(b"x" * (MAX_ACQUIRED_BYTES + 1))

    connector._cloner = greedy_cloner  # type: ignore[attr-defined]

    with pytest.raises(GitSourceRefused) as excinfo:
        connector._acquire("https://example.com/repo.git")

    assert "exceeds" in str(excinfo.value)
    assert list(scratch.iterdir()) == [], "the oversized clone must be deleted"


# --------------------------------------------------------------------------------------
# 4. End-to-end: a remote source syncs through REAL git, local paths unchanged
# --------------------------------------------------------------------------------------


def _make_real_repo(path: Path) -> dict[str, bytes]:
    """A real git repository with three tracked files."""
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(path)], check=True, capture_output=True)
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
    }
    files = {
        "README.md": b"readme\n",
        "docs/a.md": b"# Alpha\nhello\n",
        "docs/b.md": b"# Beta\nsecond\n",
    }
    for rel, content in files.items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True, capture_output=True, env=env)
    subprocess.run(
        ["git", "commit", "-q", "-m", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        env=env,
    )
    return files


async def test_a_remote_source_syncs_end_to_end(tmp_path: Path) -> None:
    """CHECKLIST 1: a remote syncs — with only the network hop faked.

    Acquisition is injected (no network), but ``ls-tree``/``cat-file`` run
    against a **real** repository, so discovery, paging, blob integrity and
    content round-trip are all genuine.
    """
    origin_path = tmp_path / "origin"
    files = _make_real_repo(origin_path)
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    connector = GitConnector(scratch_root=str(scratch))
    # The "network": acquisition copies the fetched objects into the dest.
    connector._cloner = lambda url, dest: shutil.copytree(  # type: ignore[attr-defined]
        origin_path, dest, symlinks=True, dirs_exist_ok=True
    )

    config = _config("https://example.com/repo.git")
    discovery = await connector.discover(config)

    names = {item.external_id for item in discovery.items}
    assert names == set(files), f"unexpected tree: {names}"

    for item in discovery.items:
        content = await connector.fetch(config, item)
        assert content == files[item.external_id], f"{item.external_id} did not round-trip"

    result = await connector.sync(config)
    assert result.discovered == len(files)
    assert result.fetched == len(files)
    assert result.errors == []


async def test_a_local_path_still_works_unchanged(tmp_path: Path) -> None:
    """CHECKLIST 3: local sources keep working, and byte-identically."""
    checkout = tmp_path / "checkout"
    files = _make_real_repo(checkout)
    connector = GitConnector()

    config = _config(str(checkout))
    # Byte-identical: the operator's own path comes back verbatim — no
    # scratch, no clone, no resolve() rewriting it.
    assert connector._repo(config) == str(checkout)

    discovery = await connector.discover(config)
    assert {item.external_id for item in discovery.items} == set(files)
    for item in discovery.items:
        assert await connector.fetch(config, item) == files[item.external_id]


async def test_a_local_path_is_never_sent_to_a_cloner(tmp_path: Path) -> None:
    """Local must not silently start acquiring — that would be a regression."""
    checkout = tmp_path / "checkout"
    _make_real_repo(checkout)
    calls: list[str] = []
    connector = GitConnector(cloner=lambda url, dest: calls.append(url))

    await connector.discover(_config(str(checkout)))

    assert calls == [], "a local path must never trigger acquisition"


async def test_an_unauthorized_path_source_is_rejected(tmp_path: Path) -> None:
    """CHECKLIST 2: an unauthorized path cannot be reached through this seam."""
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    secret = tmp_path / "secret.git"
    _make_real_repo(secret)
    connector = GitConnector(scratch_root=str(scratch))

    # The hostile shape: an acquired repo whose real location is outside the
    # scratch root. Containment must refuse it rather than read it.
    scratch_repo = scratch / "repo"
    scratch_repo.symlink_to(secret, target_is_directory=True)

    with pytest.raises(GitSourceRefused):
        contain_path(str(scratch), str(scratch_repo))

    # And the connector's own acquisition path refuses the same shape.
    def symlink_cloner(url: str, dest: str) -> None:
        # A hostile "acquisition" that parks the repo OUTSIDE the scratch root
        # behind a plausible-looking name inside it.
        os.rmdir(dest)
        os.symlink(secret, dest, target_is_directory=True)

    connector._cloner = symlink_cloner  # type: ignore[attr-defined]
    with pytest.raises(GitSourceRefused):
        await connector.discover(_config("https://example.com/repo.git"))


def test_the_subcommand_whitelist_still_blocks_clone_on_the_read_path() -> None:
    """T-V1-034's guarantee survives T-V1-040: reads cannot acquire."""
    connector = GitConnector()
    with pytest.raises(ValueError, match="not allowed"):
        connector._run(["clone", "x", "y"], "/srv/repo")
    with pytest.raises(ValueError, match="not allowed"):
        connector._run(["fetch"], "/srv/repo")
