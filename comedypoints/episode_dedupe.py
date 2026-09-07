import datetime
from contextlib import contextmanager
from dataclasses import dataclass
import sqlite3


UTC = datetime.timezone.utc
CLAIM_RETENTION = datetime.timedelta(days=30)


@dataclass(frozen=True)
class EpisodeClaim:
    claimed: bool
    normalized_title: str
    display_title: str
    source: str
    source_id: str
    claimed_at: datetime.datetime
    posted_at: datetime.datetime | None = None
    message_id: int | None = None


class EpisodeClaimStore:
    def __init__(self, path: str):
        self.path = path

    def initialize(self) -> None:
        with self._connect() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS episode_claims (
                    normalized_title TEXT PRIMARY KEY,
                    display_title TEXT NOT NULL,
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    published_at TEXT,
                    claimed_at TEXT NOT NULL,
                    posted_at TEXT,
                    message_id INTEGER,
                    UNIQUE (source, source_id)
                )
                """
            )
            db.execute("""
                CREATE TABLE IF NOT EXISTS episode_sources (
                    source TEXT NOT NULL,
                    source_id TEXT NOT NULL,
                    normalized_title TEXT NOT NULL REFERENCES
                        episode_claims(normalized_title) ON DELETE CASCADE,
                    PRIMARY KEY (source, source_id)
                )
            """)
            db.execute("""
                INSERT OR IGNORE INTO episode_sources
                SELECT source, source_id, normalized_title FROM episode_claims
            """)

    def claim(
        self,
        *,
        normalized_title: str,
        display_title: str,
        source: str,
        source_id: str,
        published_at: datetime.datetime | None,
        now: datetime.datetime | None = None,
    ) -> EpisodeClaim:
        now = _as_utc(now or datetime.datetime.now(UTC))
        cutoff = now - CLAIM_RETENTION
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute(
                "DELETE FROM episode_claims "
                "WHERE posted_at < ?",
                (cutoff.isoformat(),),
            )
            # Stable source identity wins over a changed/reused title. Remember
            # every source, including the loser of a cross-source race.
            row = db.execute("""
                SELECT c.normalized_title, c.display_title, c.source, c.source_id,
                       c.claimed_at, c.posted_at, c.message_id
                FROM episode_claims c
                WHERE c.normalized_title = COALESCE(
                    (SELECT normalized_title FROM episode_sources
                     WHERE source = ? AND source_id = ?), ?)
            """, (source, source_id, normalized_title)).fetchone()
            if row is not None:
                db.execute(
                    "INSERT OR IGNORE INTO episode_sources VALUES (?, ?, ?)",
                    (source, source_id, row[0]),
                )
                return EpisodeClaim(
                    False, *row[:4], _parse_datetime(row[4]),
                    _parse_datetime(row[5]), row[6],
                )
            db.execute(
                """
                INSERT INTO episode_claims (
                    normalized_title,
                    display_title,
                    source,
                    source_id,
                    published_at,
                    claimed_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    normalized_title,
                    display_title,
                    source,
                    source_id,
                    _isoformat(published_at),
                    now.isoformat(),
                ),
            )
            db.execute(
                "INSERT INTO episode_sources VALUES (?, ?, ?)",
                (source, source_id, normalized_title),
            )
            return EpisodeClaim(
                True, normalized_title, display_title, source, source_id, now,
            )

    def complete(
        self,
        *,
        normalized_title: str,
        source: str,
        source_id: str,
        message_id: int,
        posted_at: datetime.datetime | None = None,
    ) -> None:
        posted_at = _as_utc(posted_at or datetime.datetime.now(UTC))
        with self._connect() as db:
            cursor = db.execute(
                """
                UPDATE episode_claims
                SET posted_at = ?, message_id = ?
                WHERE normalized_title = ? AND source = ? AND source_id = ?
                """,
                (
                    posted_at.isoformat(),
                    message_id,
                    normalized_title,
                    source,
                    source_id,
                ),
            )
            if cursor.rowcount != 1:
                raise RuntimeError("episode claim disappeared before completion")

    def release(
        self, *, normalized_title: str, source: str, source_id: str
    ) -> None:
        with self._connect() as db:
            db.execute(
                """
                DELETE FROM episode_claims
                WHERE normalized_title = ? AND source = ? AND source_id = ?
                    AND posted_at IS NULL
                """,
                (normalized_title, source, source_id),
            )

    def recent_post_times(
        self, *, now: datetime.datetime | None = None
    ) -> tuple[datetime.datetime, ...]:
        now = _as_utc(now or datetime.datetime.now(UTC))
        cutoff = now - CLAIM_RETENTION
        with self._connect() as db:
            rows = db.execute(
                """
                SELECT posted_at
                FROM episode_claims
                WHERE posted_at IS NOT NULL AND posted_at >= ?
                ORDER BY posted_at
                """,
                (cutoff.isoformat(),),
            ).fetchall()
        return tuple(_parse_datetime(row[0]) for row in rows)

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.execute("PRAGMA foreign_keys = ON")
        db.execute("PRAGMA busy_timeout = 15000")
        try:
            with db:
                yield db
        finally:
            db.close()


class WebSubInbox(EpisodeClaimStore):
    """Durable, single-worker delivery queue; completed hashes absorb replays."""

    def initialize(self):
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS websub_settings (
                key TEXT PRIMARY KEY, value TEXT NOT NULL)""")
            db.execute(
                "INSERT OR IGNORE INTO websub_settings VALUES ('activated_at', ?)",
                (datetime.datetime.now(UTC).isoformat(),),
            )
            db.execute("""CREATE TABLE IF NOT EXISTS websub_inbox (
                digest TEXT PRIMARY KEY, payload BLOB NOT NULL,
                received_at TEXT NOT NULL, retry_at TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0, completed_at TEXT)""")

    def activated_at(self):
        with self._connect() as db:
            return _parse_datetime(db.execute(
                "SELECT value FROM websub_settings WHERE key = 'activated_at'"
            ).fetchone()[0])

    def enqueue(self, digest, payload, received_at):
        with self._connect() as db:
            db.execute("""INSERT OR IGNORE INTO websub_inbox
                (digest, payload, received_at, retry_at) VALUES (?, ?, ?, ?)""",
                (digest, payload, received_at.isoformat(), received_at.isoformat()))

    def next_delivery(self, now=None):
        now = now or datetime.datetime.now(UTC)
        with self._connect() as db:
            db.execute("DELETE FROM websub_inbox WHERE completed_at < ?",
                       ((now - CLAIM_RETENTION).isoformat(),))
            return db.execute("""SELECT digest, payload, received_at, attempts
                FROM websub_inbox WHERE completed_at IS NULL AND retry_at <= ?
                ORDER BY retry_at LIMIT 1""", (now.isoformat(),)).fetchone()

    def finish_delivery(self, digest):
        with self._connect() as db:
            db.execute("UPDATE websub_inbox SET completed_at = ? WHERE digest = ?",
                       (datetime.datetime.now(UTC).isoformat(), digest))

    def retry_delivery(self, digest, attempts):
        delay = min(900, 5 * 2 ** min(attempts, 8))
        with self._connect() as db:
            db.execute("""UPDATE websub_inbox SET attempts = attempts + 1,
                retry_at = ? WHERE digest = ?""",
                ((datetime.datetime.now(UTC) + datetime.timedelta(seconds=delay))
                 .isoformat(), digest))
        return delay


def _as_utc(value: datetime.datetime) -> datetime.datetime:
    if value.tzinfo is None:
        raise ValueError("episode claim timestamps must be timezone-aware")
    return value.astimezone(UTC)


def _isoformat(value: datetime.datetime | None) -> str | None:
    return _as_utc(value).isoformat() if value is not None else None


def _parse_datetime(value: str | None) -> datetime.datetime | None:
    return datetime.datetime.fromisoformat(value) if value is not None else None
