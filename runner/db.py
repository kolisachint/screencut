"""SQLite (architecture.md §5.4).

Media and artifacts are files, in per-job directories. Records are rows, here.
**Never put media in the database.** The reason for having one at all is that both
the learner and the review UI want queries — "median `zoom_factor` over the last 20
accepted jobs in `shorts_9x16`" is a query, and retrofitting a database once the
golden set matters is worse than starting with one.

Six tables, none of them large. `pref_changes` still has no writer — it waits for
the learner (§10) — and was created with the first migration for the same reason
`accepted_specs` was: a migration that adds a table to a database with history is
ordinary, and one that adds it under pressure while the learner is being debugged
is not. `reviews` is phase 7's own, because what a reviewer changed is a different
record from what they accepted: a rejection changes no spec and still says
something, and §10.1 has to be able to ask both questions.
"""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

DEFAULT_DB_PATH = Path("data") / "screencut.db"

MIGRATIONS: list[tuple[str, str]] = [
    (
        "0001_initial",
        """
        CREATE TABLE jobs (
            job_id        TEXT PRIMARY KEY,
            status        TEXT NOT NULL,
            spec_version  INTEGER NOT NULL,
            job_dir       TEXT NOT NULL,
            degradations  TEXT NOT NULL DEFAULT '[]',
            created_at    TEXT NOT NULL,
            updated_at    TEXT NOT NULL
        );

        CREATE TABLE stage_cache (
            cache_key     TEXT PRIMARY KEY,
            job_id        TEXT NOT NULL,
            stage         TEXT NOT NULL,
            stage_version INTEGER NOT NULL,
            profile       TEXT,
            path          TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX stage_cache_by_job ON stage_cache (job_id, stage);

        CREATE TABLE accepted_specs (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id      TEXT NOT NULL,
            profile     TEXT NOT NULL,
            spec_json   TEXT NOT NULL,
            accepted_at TEXT NOT NULL
        );
        CREATE INDEX accepted_specs_by_profile ON accepted_specs (profile, accepted_at);

        CREATE TABLE pref_changes (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            profile     TEXT NOT NULL,
            key         TEXT NOT NULL,
            old_value   TEXT,
            new_value   TEXT NOT NULL,
            caused_by   TEXT NOT NULL,
            changed_at  TEXT NOT NULL
        );
        """,
    ),
    (
        "0002_verifications",
        """
        CREATE TABLE verifications (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id        TEXT NOT NULL,
            profile       TEXT NOT NULL,
            passed        INTEGER NOT NULL,
            findings_json TEXT NOT NULL,
            created_at    TEXT NOT NULL
        );
        CREATE INDEX verifications_by_job ON verifications (job_id, profile);
        """,
    ),
    (
        "0003_reviews",
        """
        CREATE TABLE reviews (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            job_id     TEXT NOT NULL,
            decision   TEXT NOT NULL,
            diff_json  TEXT NOT NULL,
            created_at TEXT NOT NULL
        );
        CREATE INDEX reviews_by_job ON reviews (job_id, id);
        """,
    ),
]


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@contextmanager
def connect(path: Path | str = DEFAULT_DB_PATH) -> Iterator[sqlite3.Connection]:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    try:
        migrate(connection)
        yield connection
        connection.commit()
    finally:
        connection.close()


def migrate(connection: sqlite3.Connection) -> list[str]:
    """Apply pending migrations in order, recording each by name.

    By name rather than by number, so a migration that was renamed is a visible
    problem rather than a silent re-run.
    """
    connection.execute(
        "CREATE TABLE IF NOT EXISTS schema_migrations (name TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
    )
    applied = {row["name"] for row in connection.execute("SELECT name FROM schema_migrations")}
    fresh: list[str] = []
    for name, script in MIGRATIONS:
        if name in applied:
            continue
        connection.executescript(script)
        connection.execute(
            "INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (name, now())
        )
        fresh.append(name)
    connection.commit()
    return fresh


# --- jobs --------------------------------------------------------------------


def upsert_job(
    connection: sqlite3.Connection,
    *,
    job_id: str,
    job_dir: str,
    spec_version: int,
    status: str,
    degradations: list[str] | None = None,
) -> None:
    stamp = now()
    connection.execute(
        """
        INSERT INTO jobs (job_id, status, spec_version, job_dir, degradations, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET
            status = excluded.status,
            spec_version = excluded.spec_version,
            job_dir = excluded.job_dir,
            degradations = excluded.degradations,
            updated_at = excluded.updated_at
        """,
        (job_id, status, spec_version, job_dir, json.dumps(degradations or []), stamp, stamp),
    )


def get_job(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()


# --- stage cache -------------------------------------------------------------


def record_artifact(
    connection: sqlite3.Connection,
    *,
    cache_key: str,
    job_id: str,
    stage: str,
    stage_version: int,
    path: str,
    profile: str | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO stage_cache (cache_key, job_id, stage, stage_version, profile, path, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cache_key) DO UPDATE SET path = excluded.path
        """,
        (cache_key, job_id, stage, stage_version, profile, path, now()),
    )


def lookup_artifact(connection: sqlite3.Connection, cache_key: str) -> str | None:
    """Cache lookup is a query, not a directory walk (§5.4)."""
    row = connection.execute(
        "SELECT path FROM stage_cache WHERE cache_key = ?", (cache_key,)
    ).fetchone()
    return row["path"] if row else None


def forget_artifact(connection: sqlite3.Connection, cache_key: str) -> None:
    connection.execute("DELETE FROM stage_cache WHERE cache_key = ?", (cache_key,))


# --- verification (§9) -------------------------------------------------------


def record_verification(
    connection: sqlite3.Connection, *, job_id: str, profile: str, passed: bool, findings_json: str
) -> None:
    """A report per render, on the job record (§9.1).

    A table rather than a column because §10.1's first rule — never learn from a
    job that failed verification — is a query, and a broken render's corrections
    poison the defaults.
    """
    connection.execute(
        "INSERT INTO verifications (job_id, profile, passed, findings_json, created_at) VALUES (?, ?, ?, ?, ?)",
        (job_id, profile, int(passed), findings_json, now()),
    )


def latest_verification(connection: sqlite3.Connection, job_id: str, profile: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM verifications WHERE job_id = ? AND profile = ? ORDER BY id DESC LIMIT 1",
        (job_id, profile),
    ).fetchone()


def rendered_profiles(connection: sqlite3.Connection, job_id: str) -> list[str]:
    """Profiles this job has actually been rendered and verified for.

    Which profiles a job has is not in the job record, because §4.1 puts them
    outside the spec: one `EditSpec` x N `RenderProfile`, chosen per run. What was
    rendered is therefore a fact about the runs, and the report per render is
    where it is written down.
    """
    rows = connection.execute(
        "SELECT DISTINCT profile FROM verifications WHERE job_id = ? ORDER BY profile", (job_id,)
    ).fetchall()
    return [row["profile"] for row in rows]


def verified_jobs(connection: sqlite3.Connection, profile: str) -> list[str]:
    """Jobs whose latest report for this profile passed — the learner's corpus (§10.1)."""
    rows = connection.execute(
        """
        SELECT job_id, passed FROM verifications v
        WHERE profile = ? AND id = (
            SELECT MAX(id) FROM verifications WHERE job_id = v.job_id AND profile = v.profile
        )
        """,
        (profile,),
    ).fetchall()
    return [row["job_id"] for row in rows if row["passed"]]


# --- the learning corpus (phases 7 and 10) -----------------------------------


def record_accepted_spec(
    connection: sqlite3.Connection, *, job_id: str, profile: str, spec_json: str
) -> None:
    """An accepted spec, with the profile it was accepted under (§5.4).

    Per profile because preferences are learned per profile: caption Y in vertical
    is a different number from caption Y in widescreen.
    """
    connection.execute(
        "INSERT INTO accepted_specs (job_id, profile, spec_json, accepted_at) VALUES (?, ?, ?, ?)",
        (job_id, profile, spec_json, now()),
    )


def record_review(
    connection: sqlite3.Connection, *, job_id: str, decision: str, diff_json: str
) -> int:
    """One review decision, with the proposed -> corrected diff that produced it (§8).

    The diff rather than the corrected spec, because §10 learns from *differences*:
    "this reviewer reinstates silences shorter than a second" is a statement about a
    diff, and recovering it by comparing two whole specs later means keeping both
    forever. The accepted spec is stored separately, per profile, by
    `record_accepted_spec` — the two answer different questions and a rejection
    answers only this one.
    """
    cursor = connection.execute(
        "INSERT INTO reviews (job_id, decision, diff_json, created_at) VALUES (?, ?, ?, ?)",
        (job_id, decision, diff_json, now()),
    )
    return int(cursor.lastrowid or 0)


def latest_review(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row | None:
    return connection.execute(
        "SELECT * FROM reviews WHERE job_id = ? ORDER BY id DESC LIMIT 1", (job_id,)
    ).fetchone()


def list_jobs(connection: sqlite3.Connection, limit: int = 100) -> list[sqlite3.Row]:
    """Every job the pipeline has run, newest first — the review index (§8)."""
    return connection.execute(
        "SELECT * FROM jobs ORDER BY updated_at DESC, job_id LIMIT ?", (limit,)
    ).fetchall()


def record_pref_change(
    connection: sqlite3.Connection,
    *,
    profile: str,
    key: str,
    old_value: Any,
    new_value: Any,
    caused_by: list[str],
) -> None:
    """Every learned-default move, with the jobs that caused it (§10.1).

    "It got worse and nobody can explain why" is the characteristic failure of
    self-tuning systems, and this table is the whole answer to it.
    """
    connection.execute(
        """
        INSERT INTO pref_changes (profile, key, old_value, new_value, caused_by, changed_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (profile, key, json.dumps(old_value), json.dumps(new_value), json.dumps(caused_by), now()),
    )
