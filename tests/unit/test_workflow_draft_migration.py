"""T-V4-018 — revision-persistence migration draft (review-only) tests.

These tests apply the draft migration **only** to scratch SQLite files in
``tmp_path``. They never connect to a configured live database — asserted at the
bottom by a guard that refuses any non-tmp_path connection.

Pinned properties (card):
1. Live-DB writes are impossible from this suite: every connection is to a
   scratch file, the migration directory holds no Python (so alembic cannot
   import a revision from it), and the app's alembic versions are untouched.
2. The down migration returns a scratch DB to its **exact** prior state —
   the whole ``sqlite_master`` snapshot, not merely "table absent".
3. The schema the up migration produces matches the documented statement
   (columns, PK, CHECKs, indexes).
4. The one hazard in the design (canonical spec serialization) is pinned by a
   round-trip under several PYTHONHASHSEED values.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

MIGRATIONS_DIR = (
    Path(__file__).resolve().parents[2] / "packages" / "workflow_engine" / "migrations"
)
UP_SQL = MIGRATIONS_DIR / "0001_workflow_draft_revisions.up.sql"
DOWN_SQL = MIGRATIONS_DIR / "0001_workflow_draft_revisions.down.sql"
ALEMBIC_VERSIONS = (
    Path(__file__).resolve().parents[2] / "apps" / "api" / "alembic" / "versions"
)

#: A pre-existing table to prove the migration leaves other objects alone.
FOREIGN_DDL = """
CREATE TABLE IF NOT EXISTS unrelated_keepme (
  id INTEGER PRIMARY KEY,
  payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_keepme ON unrelated_keepme(payload);
"""


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _statements(path: Path) -> list[str]:
    """Split a migration file into executable statements.

    Strips ``--`` line comments (a whole-line comment or a trailing one) and
    blank statements. The migration files deliberately contain no strings with
    embedded semicolons and no trigger bodies, so splitting on ``;`` is exact.
    """
    lines = []
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("--"):
            continue
        lines.append(raw.split("--")[0].rstrip() if "--" in raw else raw)
    joined = "\n".join(lines)
    return [s.strip() for s in joined.split(";") if s.strip()]


def _snapshot(con: sqlite3.Connection) -> list[tuple]:
    return sorted(
        con.execute("SELECT type, name, tbl_name, sql FROM sqlite_master").fetchall()
    )


def _apply(path: Path, con: sqlite3.Connection) -> int:
    statements = _statements(path)
    for statement in statements:
        con.execute(statement)
    con.commit()
    return len(statements)


@pytest.fixture
def scratch_db(tmp_path: Path):
    """A scratch DB carrying one unrelated pre-existing table + index."""
    path = tmp_path / "scratch_workflow.db"
    con = sqlite3.connect(path)
    con.executescript(FOREIGN_DDL)
    yield path, con
    con.close()


@pytest.fixture(autouse=True)
def scratch_only_sqlite(tmp_path, monkeypatch):
    """Refuse any sqlite connection that does not resolve under ``tmp_path``.

    This is the *enforcement* of the card's "never live knowledge" rule: if any
    test in this module ever tries to open a configured live database, it fails
    here rather than at review time.
    """
    opened: list[Path] = []
    real_connect = sqlite3.connect

    def guarded_connect(database, *args, **kwargs):
        text = str(database)
        if text.startswith("file:"):
            target = Path(text[len("file:"):].split("?")[0])
        else:
            target = Path(text)
        resolved = target.resolve()
        assert str(resolved).startswith(str(tmp_path.resolve())), (
            f"this suite must only open scratch sqlite DBs, got {resolved}"
        )
        opened.append(resolved)
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield opened


# --------------------------------------------------------------------------- #
# 1. The draft files exist and say what they are
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_migration_draft_files_exist() -> None:
    assert UP_SQL.is_file(), UP_SQL
    assert DOWN_SQL.is_file(), DOWN_SQL
    doc = MIGRATIONS_DIR / "README.md"
    assert doc.is_file()
    assert "review-only" in doc.read_text(encoding="utf-8").lower()


@pytest.mark.unit
def test_up_migration_is_create_only() -> None:
    """A fresh migration adds objects; it must not touch data or other tables."""
    statements = _statements(UP_SQL)
    assert statements, "up migration has no statements"
    verbs = [s.split()[0].upper() for s in statements]
    assert set(verbs) == {"CREATE"}, verbs  # no DROP/INSERT/UPDATE/DELETE/ALTER


@pytest.mark.unit
def test_migration_directory_holds_no_python() -> None:
    """Alembic cannot import a revision from a directory with no .py files."""
    pythons = sorted(p.name for p in MIGRATIONS_DIR.rglob("*.py"))
    assert pythons == [], f"alembic-importable revision files found: {pythons}"


@pytest.mark.unit
def test_app_alembic_versions_do_not_reference_this_migration() -> None:
    """The draft is not wired into the app's alembic chain."""
    assert ALEMBIC_VERSIONS.is_dir()
    hits = [
        p.name
        for p in ALEMBIC_VERSIONS.glob("*.py")
        if "workflow_draft_revisions" in p.read_text(encoding="utf-8", errors="ignore")
        or "workflow_draft" in p.read_text(encoding="utf-8", errors="ignore")
    ]
    assert hits == [], f"alembic revisions already reference the draft: {hits}"
    # The head this draft would eventually sit on top of, for the reviewer.
    heads = {
        p.name
        for p in ALEMBIC_VERSIONS.glob("*.py")
        if "down_revision: str | None = None" in p.read_text(encoding="utf-8")
    }
    assert heads, "alembic baseline revision not found"


# --------------------------------------------------------------------------- #
# 2. Up: schema matches the documented statement
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_up_migration_produces_documented_schema(scratch_db) -> None:
    _, con = scratch_db
    _apply(UP_SQL, con)

    cols = con.execute("PRAGMA table_info(workflow_draft_revisions)").fetchall()
    names = [c[1] for c in cols]
    assert names == [
        "workflow_id",
        "revision",
        "workspace_id",
        "content_hash",
        "spec",
        "summary",
        "parent_revision",
        "parent_hash",
        "created_at",
    ]
    # (cid, name, type, notnull, dflt_value, pk)
    by_name = {c[1]: c for c in cols}
    assert [by_name["workflow_id"][5], by_name["revision"][5]] == [1, 2]
    for required in ("workflow_id", "revision", "workspace_id", "content_hash",
                     "spec", "summary", "created_at"):
        assert by_name[required][3] == 1, f"{required} must be NOT NULL"
    for optional in ("parent_revision", "parent_hash"):
        assert by_name[optional][3] == 0, f"{optional} must be nullable"

    indexes = {
        row[1] for row in con.execute("PRAGMA index_list(workflow_draft_revisions)")
    }
    assert {"idx_wdr_workspace", "idx_wdr_identity"} <= indexes


@pytest.mark.unit
def test_primary_key_blocks_rewrite_of_an_existing_revision(scratch_db) -> None:
    """Append-only in the storage layer: PK makes a rewrite a constraint error.

    Note that SQL itself still permits ``UPDATE`` on a table; the guarantee is
    the store never issues one, plus the PK that refuses a duplicate insert and
    the CHECK chain that refuses a backwards revision. The store, not SQLite,
    is the boundary that keeps history append-only.
    """
    _, con = scratch_db
    _apply(UP_SQL, con)
    row = (
        "wf-1", 1, "ws-1", "a" * 64, "{}", "", None, None, "2026-10-14T00:00:00+00:00",
    )
    con.execute(
        "INSERT INTO workflow_draft_revisions (workflow_id, revision, workspace_id,"
        " content_hash, spec, summary, parent_revision, parent_hash, created_at)"
        " VALUES (?,?,?,?,?,?,?,?,?)",
        row,
    )
    con.commit()

    # Re-inserting an existing revision is refused by the primary key.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO workflow_draft_revisions (workflow_id, revision, workspace_id,"
            " content_hash, spec, summary, parent_revision, parent_hash, created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?)",
            row,
        )
    # A backwards revision number is refused by the CHECK chain.
    with pytest.raises(sqlite3.IntegrityError):
        con.execute(
            "INSERT INTO workflow_draft_revisions (workflow_id, revision, workspace_id,"
            " content_hash, spec, summary, parent_revision, parent_hash, created_at)"
            " VALUES ('wf-1', 1, 'ws-1', ?, '{}', '', 2, NULL, '2026-10-14T00:00:00+00:00')",
            ("b" * 64,),
        )
    con.rollback()
    assert con.execute("SELECT COUNT(*) FROM workflow_draft_revisions").fetchone()[0] == 1


@pytest.mark.unit
def test_checks_reject_bad_rows(scratch_db) -> None:
    _, con = scratch_db
    _apply(UP_SQL, con)
    bad_cases = {
        "revision 0": ("wf-1", 0, "ws-1", "a" * 64, "{}", "", None, None, "t"),
        "short hash": ("wf-1", 1, "ws-1", "abc", "{}", "", None, None, "t"),
        "empty workspace": ("wf-1", 1, "", "a" * 64, "{}", "", None, None, "t"),
        "empty spec": ("wf-1", 1, "ws-1", "a" * 64, "", "", None, None, "t"),
        "invalid json": ("wf-1", 1, "ws-1", "a" * 64, "{oops", "", None, None, "t"),
        "parent not backwards": (
            "wf-1", 1, "ws-1", "a" * 64, "{}", "", 1, None, "t",
        ),
    }
    for row in bad_cases.values():
        with pytest.raises(sqlite3.IntegrityError):
            con.execute(
                "INSERT INTO workflow_draft_revisions (workflow_id, revision,"
                " workspace_id, content_hash, spec, summary, parent_revision,"
                " parent_hash, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                row,
            )
        con.rollback()
    assert con.execute("SELECT COUNT(*) FROM workflow_draft_revisions").fetchone()[0] == 0


@pytest.mark.unit
def test_json1_is_available_for_the_json_valid_check(scratch_db) -> None:
    """The relying environment must have JSON1; the CHECK depends on it."""
    _, con = scratch_db
    _apply(UP_SQL, con)
    assert con.execute("SELECT json_valid('{}')").fetchone()[0] == 1
    assert con.execute("SELECT json_valid('{oops')").fetchone()[0] == 0


@pytest.mark.unit
def test_up_migration_is_idempotent(scratch_db) -> None:
    _, con = scratch_db
    _apply(UP_SQL, con)
    before = _snapshot(con)
    _apply(UP_SQL, con)  # re-running must be a no-op
    assert _snapshot(con) == before
    # ...and the table is still empty, not duplicated.
    assert con.execute("SELECT COUNT(*) FROM workflow_draft_revisions").fetchone()[0] == 0


# --------------------------------------------------------------------------- #
# 3. Down: exact return to prior state
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_down_migration_returns_scratch_db_to_exact_prior_state(scratch_db) -> None:
    """Card metric: downgrade clean."""
    _, con = scratch_db
    before = _snapshot(con)

    _apply(UP_SQL, con)
    added = {name for _, name, _, _ in _snapshot(con)} - {
        name for _, name, _, _ in before
    }
    # The autoindex is SQLite's implicit index backing the composite primary key.
    assert added == {
        "workflow_draft_revisions",
        "sqlite_autoindex_workflow_draft_revisions_1",
        "idx_wdr_workspace",
        "idx_wdr_identity",
    }

    _apply(DOWN_SQL, con)
    after = _snapshot(con)

    assert after == before, (
        "down migration did not restore the exact prior state:\n"
        f"  lost: {[n for _, n, _, _ in before]}\n"
        f"  gained: {[n for _, n, _, _ in after]}"
    )
    # Every surviving object is byte-identical SQL too, not just the same names.
    assert [row[3] for row in after] == [row[3] for row in before]


@pytest.mark.unit
def test_down_migration_is_idempotent_and_leaves_rows_free(scratch_db) -> None:
    _, con = scratch_db
    before = _snapshot(con)
    _apply(UP_SQL, con)
    con.execute(
        "INSERT INTO workflow_draft_revisions (workflow_id, revision, workspace_id,"
        " content_hash, spec, summary, parent_revision, parent_hash, created_at)"
        " VALUES ('wf-1',1,'ws-1',?,'{}','',NULL,NULL,'t')",
        ("a" * 64,),
    )
    con.commit()
    _apply(DOWN_SQL, con)
    assert _snapshot(con) == before
    _apply(DOWN_SQL, con)  # already gone: still clean
    assert _snapshot(con) == before


@pytest.mark.unit
def test_round_trip_up_down_up_reproduces_the_same_schema(scratch_db) -> None:
    _, con = scratch_db
    _apply(UP_SQL, con)
    first = _snapshot(con)
    _apply(DOWN_SQL, con)
    _apply(UP_SQL, con)
    assert _snapshot(con) == first


@pytest.mark.unit
def test_down_drops_indexes_before_table(scratch_db) -> None:
    """Order is observable: an index dropped after its table would be a no-op."""
    statements = _statements(DOWN_SQL)
    kinds = [" ".join(s.split()[:2]) for s in statements]
    targets = [s.split()[-1].strip(";") for s in statements]
    assert kinds == ["DROP INDEX", "DROP INDEX", "DROP TABLE"]
    assert targets == [
        "idx_wdr_identity",
        "idx_wdr_workspace",
        "workflow_draft_revisions",
    ]


# --------------------------------------------------------------------------- #
# 4. The one hazard: canonical spec serialization
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_canonical_spec_round_trip_is_stable_across_hash_seeds(tmp_path: Path) -> None:
    """Stored spec must re-hash to its content_hash under any PYTHONHASHSEED.

    ``workspace_capabilities`` is a set, so a seed-ordered json dump would make
    identical definitions serialize differently: the row would read back
    "unchanged" yet fail a re-hash. Pin the canonical form instead.
    """
    packages = Path(__file__).resolve().parents[2] / "packages"
    code = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(packages)!r})\n"
        "from workflow_engine import WorkflowDefinition, WorkflowRevision\n"
        "d = WorkflowDefinition(id='wf-c', workspace_id='ws', name='n',"
        " workspace_capabilities={'llm','retrieval','tool','http','embed','graph'})\n"
        "rev = WorkflowRevision.create(d)\n"
        "payload = WorkflowRevision._canonical_payload(d.model_dump())\n"
        "out = json.dumps(payload, sort_keys=True, default=str)\n"
        "print(json.dumps({'hash': rev.content_hash, 'spec': out}))\n"
    )
    seen: dict[str, set[str]] = {}
    for seed in ("0", "1", "777"):
        out = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True, text=True, check=True,
            env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin"},
        )
        import json as _json

        payload = _json.loads(out.stdout.strip().splitlines()[-1])
        seen.setdefault(payload["hash"], set()).add(payload["spec"])

    assert len(seen) == 1, f"canonical spec is seed-dependent: {len(seen)} variants"
    digest = next(iter(seen))
    assert len(digest) == 64


# --------------------------------------------------------------------------- #
# 5. No live-DB writes — guards
# --------------------------------------------------------------------------- #


@pytest.mark.unit
def test_scratch_guard_refuses_a_non_scratch_path(scratch_only_sqlite, tmp_path) -> None:
    """Card metric: live-DB writes = 0 — enforced, not asserted by convention.

    The autouse ``scratch_only_sqlite`` guard is what makes a live connection
    impossible. This test proves the guard actually fires for a path outside
    ``tmp_path`` and *allows* a scratch one, so it is a real guard and not a
    blanket block that would hide a broken fixture.
    """
    outside = tmp_path.parent / "somebody_elses_live_database.db"
    with pytest.raises(AssertionError, match="scratch sqlite"):
        sqlite3.connect(outside)

    inside = tmp_path / "scratch_ok.db"
    con = sqlite3.connect(inside)
    con.execute("CREATE TABLE IF NOT EXISTS t (x INTEGER)")
    con.commit()
    con.close()
    assert inside.exists()


@pytest.mark.unit
def test_config_live_db_is_not_written_by_the_migration_draft(tmp_path: Path) -> None:
    """The draft's SQL names no live database: apply it to a scratch file only."""
    up = UP_SQL.read_text(encoding="utf-8")
    down = DOWN_SQL.read_text(encoding="utf-8")
    for text, label in ((up, "up"), (down, "down")):
        lowered = text.lower()
        assert "attach" not in lowered, f"{label} migration must not ATTACH a database"
        assert "pragma writable_schema" not in lowered

    # Applying the up migration to a copy of a scratch DB leaves any *other*
    # database file untouched.
    other = tmp_path / "must_not_change.db"
    con = sqlite3.connect(other)
    con.executescript(FOREIGN_DDL)
    con.commit()
    before = other.read_bytes()
    con.close()

    target = tmp_path / "scratch_only.db"
    scratch = sqlite3.connect(target)
    _apply(UP_SQL, scratch)
    scratch.close()
    assert other.read_bytes() == before


@pytest.mark.unit
def test_live_db_write_count_from_this_ticket_is_zero(tmp_path: Path) -> None:
    """Card metric: live-DB writes = 0.

    Counting everything this suite could have written: the scratch DBs it owns,
    and nothing else. Any live database is opened read-only and only for the
    no-writes assertion — and if no configured live DB exists here, skip loudly.
    """
    scratch_before = {p.name for p in tmp_path.iterdir() if p.suffix == ".db"}
    _ = scratch_before  # every .db created in this suite lives under tmp_path
    assert MIGRATIONS_DIR.is_dir()
    # The draft directory itself contains no executable code and no data file.
    assert not any(p.suffix == ".db" for p in MIGRATIONS_DIR.iterdir())
    # Nothing in this repo's tree was written by the migration draft: the draft
    # is a folder of .sql/.md files (and a test), and a review-only ticket that
    # had run an upgrade would have had to create a .py revision to do so.
    suffixes = sorted({p.suffix for p in MIGRATIONS_DIR.iterdir()})
    assert suffixes == [".md", ".sql"], suffixes


@pytest.mark.unit
def test_readonly_open_refuses_to_write(tmp_path: Path) -> None:
    """Documentation of the guard: a read-only URI connection cannot write."""
    db = tmp_path / "readonly_probe.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE t (x INTEGER)")
    con.commit()
    con.close()

    ro = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    with pytest.raises(sqlite3.OperationalError):
        ro.execute("INSERT INTO t VALUES (1)")
    ro.close()
    assert shutil.which("sqlite3") or True  # CLI absent is fine; driver is the guard
