"""Connections to the vector index: hold no lock you are not using, and address
the file by a URI that survives the vault's path.

Two bug classes, both found 2026-09-01:

* `embed._connect()` ran `INSERT OR IGNORE INTO meta ...` and never committed, so
  under sqlite3's legacy transaction control every connection held sqlite's
  RESERVED write lock from connect to close. A *read* (the matrix load behind every
  recall) held it for its whole BLOB scan; `sync()` held it through the vault walk
  and the first chunk's embedding rather than for one commit; and concurrent
  recalls from the MCP server and the CLI serialised on a 30s timeout.

* The read-only URIs were f-strings over the raw path (`f"file:{idx}?mode=ro"`).
  A `#` or `?` in the vault path truncates the URI, a `%` is misread as an escape,
  and sqlite quietly opens *some other file* — reproduced: a directory named
  `uri test#1` yielded an empty database and "no such table".
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
import time
from pathlib import Path

import pytest

from brain_mcp import embed

PACKAGE = Path(embed.__file__).resolve().parent


# ---------- transactions ----------

def test_connect_returns_with_no_transaction_open(vault_dir: Path) -> None:
    """Fresh index, then a populated and fully-migrated one: neither may return a
    connection that is already inside a write transaction."""
    conn = embed._connect()
    try:
        assert conn.in_transaction is False, "fresh-index _connect() left a transaction open"
        conn.execute(
            "INSERT INTO embeddings(path, mtime, vector) VALUES ('user/x.md', 1.0, ?)", (b"\x00" * 16,)
        )
        conn.commit()
    finally:
        conn.close()

    # Second connection: schema exists, recipe stamped, path_format stamped — the
    # steady state every real connection sees. This is the case that leaked.
    conn = embed._connect()
    try:
        assert conn.in_transaction is False, (
            "_connect() on a populated index holds the RESERVED lock from connect "
            "to close — every reader and writer serialises behind it"
        )
    finally:
        conn.close()


def test_a_writer_can_commit_while_a_query_connection_is_open(vault_dir: Path) -> None:
    """The read path must not hold the write lock.

    Sets up an index, opens the read-only connection the query path uses and reads
    from it, then — with that connection still open — writes and commits through a
    second connection with a *short* timeout. Before the fix, the reader was a
    `_connect()` writer inside an uncommitted transaction and this blocked for the
    full busy timeout, then raised "database is locked".
    """
    seed = embed._connect()
    try:
        seed.execute("INSERT INTO embeddings(path, mtime, vector) VALUES ('user/a.md', 1.0, ?)", (b"\x00" * 16,))
        seed.commit()
    finally:
        seed.close()

    reader = embed._connect_ro()
    try:
        rows = reader.execute("SELECT path FROM embeddings").fetchall()
        assert rows == [("user/a.md",)]
        assert reader.in_transaction is False

        writer = sqlite3.connect(embed._index_path(), timeout=0.2)
        try:
            start = time.monotonic()
            writer.execute("INSERT INTO embeddings(path, mtime, vector) VALUES ('user/b.md', 2.0, ?)", (b"\x00" * 16,))
            writer.commit()
            assert time.monotonic() - start < 1.0
        finally:
            writer.close()

        assert reader.execute("SELECT COUNT(*) FROM embeddings").fetchone()[0] == 2
    finally:
        reader.close()


def test_query_connection_is_read_only(vault_dir: Path) -> None:
    embed._connect().close()  # create the file
    conn = embed._connect_ro()
    try:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("INSERT INTO meta VALUES ('x', 'y')")
    finally:
        conn.close()


def test_matrix_load_does_not_create_or_write_the_index(vault_dir: Path) -> None:
    """A query against a vault with no index yet is 'no vectors', not 'make one'."""
    pytest.importorskip("numpy")
    assert not embed._index_path().exists()
    paths, mat = embed.EmbedIndex._normalized_matrix(None)
    assert (paths, mat) == ([], None)
    assert not embed._index_path().exists(), "the read path created the index file"


# ---------- backlog under a lock ----------

def test_backlog_raises_index_busy_rather_than_reporting_zero(vault_dir: Path) -> None:
    """0 means 'up to date'. A locked index is *unknown*, and must say so."""
    embed._connect().close()
    holder = sqlite3.connect(embed._index_path(), timeout=30)
    holder.execute("BEGIN EXCLUSIVE")
    try:
        start = time.monotonic()
        with pytest.raises(embed.IndexBusy):
            embed.EmbedIndex.backlog(timeout=0.05)
        assert time.monotonic() - start < 1.0
    finally:
        holder.rollback()
        holder.close()
    # And once the lock is gone the same call answers normally.
    assert embed.EmbedIndex.backlog(timeout=0.05) == 0


# ---------- URI encoding ----------

def _hostile_dir_chars() -> str:
    # `?` is illegal in a Windows filename; everything else here is legal on every
    # platform the vault syncs to, and each one breaks a naive `file:` URI.
    return "uri test#1%2" if os.name == "nt" else "uri test#1%2?q"


def test_index_uri_survives_url_significant_characters_in_the_path(tmp_path: Path) -> None:
    d = tmp_path / _hostile_dir_chars()
    d.mkdir()
    idx = d / "embeddings.sqlite"
    conn = sqlite3.connect(idx)
    conn.execute("CREATE TABLE t (x)")
    conn.execute("INSERT INTO t VALUES (1)")
    conn.commit()
    conn.close()

    ro = sqlite3.connect(embed.index_uri(idx), uri=True, timeout=1)
    try:
        assert ro.execute("SELECT x FROM t").fetchone() == (1,), (
            "the URI opened a different file than the index"
        )
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO t VALUES (2)")  # mode=ro reached sqlite intact
    finally:
        ro.close()


def test_index_uri_encodes_the_path_component() -> None:
    uri = embed.index_uri(Path("/v/a b#c%d/e.sqlite"))
    assert uri.startswith("file:")
    assert uri.endswith("?mode=ro")
    body = uri[len("file:"):-len("?mode=ro")]
    assert "#" not in body and " " not in body
    assert "%23" in body and "%20" in body and "%25" in body
    assert "?" not in body


def test_every_sqlite_uri_in_the_package_is_built_from_an_encoded_path() -> None:
    """Invariant: no `sqlite3.connect("file:...")` may interpolate a raw path.

    Checked as text across every module, because the bug lived at five sites at
    once (three in embed, two in doctor — plus transcript's cherryd reader) and a
    fix at one of N sites is this repo's signature failure. A URI argument must
    come from `index_uri(` or be built with `quote(`.
    """
    offenders = []
    for source in sorted(PACKAGE.glob("*.py")):
        text = source.read_text(encoding="utf-8")
        for match in re.finditer(r"sqlite3\.connect\(", text):
            call = _balanced_call(text, match.end())
            if "file:" not in call and "uri=True" not in call:
                continue  # a plain path connect, not a URI
            if "index_uri(" in call or "quote(" in call:
                continue
            line = text[: match.start()].count("\n") + 1
            offenders.append(f"{source.name}:{line}: sqlite3.connect({call.strip()})")
    assert not offenders, (
        "sqlite URI built from a raw path — a '#', '?' or '%' in the vault path "
        "truncates it and opens a different file:\n" + "\n".join(offenders)
    )


def _balanced_call(text: str, start: int) -> str:
    depth = 1
    for i in range(start, len(text)):
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return text[start:i]
    return text[start:]
