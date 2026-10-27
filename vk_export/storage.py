"""Хранилище прогресса (SQLite) и безопасные имена файлов/папок."""

from __future__ import annotations

import re
import sqlite3
import unicodedata
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path

from .models import AttachmentRecord, ConversationKind, Status

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS conversations (
    peer_id INTEGER PRIMARY KEY,
    kind    TEXT NOT NULL,
    title   TEXT,
    folder  TEXT NOT NULL            -- относительный путь, фиксируется при первом сканировании
);
CREATE TABLE IF NOT EXISTS attachments (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    peer_id       INTEGER NOT NULL,
    message_id    INTEGER NOT NULL,
    att_index     INTEGER NOT NULL,
    msg_date      TEXT NOT NULL,     -- ISO, локальное время из архива
    author_name   TEXT,
    author_id     INTEGER,
    kind          TEXT NOT NULL,
    description   TEXT,
    url           TEXT,              -- исходная ссылка из архива
    photo_ref     TEXT,              -- photoX_Y / docX_Y, если удалось извлечь
    source_page   TEXT,
    resolved_url  TEXT,              -- фактический URL изображения (для документов)
    status        TEXT NOT NULL DEFAULT 'pending',
    reason        TEXT,
    http_status   INTEGER,
    attempts      INTEGER NOT NULL DEFAULT 0,
    retryable     INTEGER NOT NULL DEFAULT 0,
    file_path     TEXT,              -- относительно папки вывода
    file_size     INTEGER,
    sha256        TEXT,
    content_type  TEXT,
    is_duplicate  INTEGER NOT NULL DEFAULT 0,
    updated_at    TEXT,
    UNIQUE (peer_id, message_id, att_index)
);
CREATE INDEX IF NOT EXISTS ix_att_status ON attachments(status);
CREATE INDEX IF NOT EXISTS ix_att_url ON attachments(url);
CREATE INDEX IF NOT EXISTS ix_att_date ON attachments(msg_date);
CREATE TABLE IF NOT EXISTS files (
    folder  TEXT NOT NULL,
    sha256  TEXT NOT NULL,
    path    TEXT NOT NULL,
    size    INTEGER NOT NULL,
    PRIMARY KEY (folder, sha256)
);
"""

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f\x7f]')
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)),
             *(f"LPT{i}" for i in range(1, 10))}


def safe_name(name: str | None, max_len: int = 60) -> str:
    """Имя папки, допустимое в Windows и Linux."""
    name = unicodedata.normalize("NFC", name or "")
    name = _INVALID_CHARS.sub("_", name)
    name = re.sub(r"\s+", "_", name.strip())
    name = re.sub(r"_+", "_", name)
    name = name[:max_len].strip(" ._")
    if not name or name.upper().split(".")[0] in _RESERVED:
        name = f"_{name}" if name else "untitled"
    return name


def conversation_folder(kind: ConversationKind, title: str | None, peer_id: int) -> str:
    # id всегда в имени: в архиве бывают одинаковые названия (например, 25 «DELETED»).
    return f"{kind.folder}/{safe_name(title)}_{peer_id}"


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Store:
    def __init__(self, path: str | Path):
        self.path = str(path)
        self.db = sqlite3.connect(self.path)
        self.db.row_factory = sqlite3.Row
        if self.path != ":memory:":
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=NORMAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def close(self) -> None:
        self.db.commit()
        self.db.close()

    # --- meta -------------------------------------------------------------
    def set_meta(self, key: str, value: str) -> None:
        self.db.execute("INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)", (key, value))
        self.db.commit()

    def get_meta(self, key: str) -> str | None:
        row = self.db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    # --- сканирование -----------------------------------------------------
    def upsert_conversation(self, peer_id: int, title: str | None) -> None:
        kind = ConversationKind.from_peer_id(peer_id)
        row = self.db.execute("SELECT title FROM conversations WHERE peer_id = ?", (peer_id,)).fetchone()
        if row is None:
            self.db.execute(
                "INSERT INTO conversations(peer_id, kind, title, folder) VALUES (?, ?, ?, ?)",
                (peer_id, kind.value, title, conversation_folder(kind, title, peer_id)))
        elif title and row["title"] != title:
            # Папку не переименовываем, чтобы не терять уже скачанные файлы.
            self.db.execute("UPDATE conversations SET title = ? WHERE peer_id = ?", (title, peer_id))

    def add_attachments(self, records: Iterable[AttachmentRecord]) -> int:
        """Добавляет вложения; уже известные (и их статус) не трогает."""
        rows = []
        for r in records:
            status = Status.PENDING if r.kind.downloadable else Status.SKIPPED
            reason = None if r.kind.downloadable else r.kind.value
            rows.append((r.peer_id, r.message_id, r.att_index, r.msg_date.isoformat(),
                         r.author_name, r.author_id, r.kind.value, r.description, r.url,
                         r.photo_ref, r.source_page, status.value, reason, _now()))
        cur = self.db.executemany(
            """INSERT OR IGNORE INTO attachments(peer_id, message_id, att_index, msg_date,
                   author_name, author_id, kind, description, url, photo_ref, source_page,
                   status, reason, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""", rows)
        return cur.rowcount

    def commit(self) -> None:
        self.db.commit()

    # --- скачивание -------------------------------------------------------
    @staticmethod
    def _range_sql(start: date | None, end: date | None) -> tuple[str, list]:
        clauses, params = [], []
        if start:
            clauses.append("a.msg_date >= ?")
            params.append(start.isoformat())
        if end:
            clauses.append("a.msg_date < ?")
            params.append(date.fromordinal(end.toordinal() + 1).isoformat())
        return (" AND ".join(clauses) or "1"), params

    def reset_stale(self, out_dir: Path) -> int:
        """Записи «скачано», чей файл пропал с диска, возвращает в очередь."""
        missing = []
        for row in self.db.execute(
                "SELECT id, file_path FROM attachments WHERE status = ? AND file_path IS NOT NULL",
                (Status.DOWNLOADED.value,)):
            if not (out_dir / row["file_path"]).is_file():
                missing.append(row["id"])
        if missing:
            self.db.executemany(
                "UPDATE attachments SET status='pending', file_path=NULL, sha256=NULL, file_size=NULL, "
                "is_duplicate=0, reason='file_missing_requeued' WHERE id = ?", [(i,) for i in missing])
            self.db.execute("DELETE FROM files WHERE NOT EXISTS (SELECT 1 FROM attachments a"
                            " WHERE a.file_path = files.path AND a.status = 'downloaded')")
            self.db.commit()
        return len(missing)

    def jobs(self, start: date | None, end: date | None,
             retry_failed: bool) -> dict[tuple[str, str], list[sqlite3.Row]]:
        """Вложения к скачиванию, сгруппированные по (URL, тип): один URL — один запрос."""
        where, params = self._range_sql(start, end)
        status_sql = "a.status = 'pending' OR (a.status = 'failed' AND a.retryable = 1)"
        if retry_failed:
            status_sql = "a.status IN ('pending', 'failed')"
        rows = self.db.execute(
            f"""SELECT a.*, c.folder, c.kind AS conv_kind, c.title AS conv_title
                FROM attachments a JOIN conversations c ON c.peer_id = a.peer_id
                WHERE ({status_sql}) AND {where}
                  AND a.kind IN ('photo', 'photo_page', 'doc_page', 'doc_image')
                ORDER BY a.msg_date, a.peer_id, a.message_id, a.att_index""", params).fetchall()
        groups: dict[tuple[str, str], list[sqlite3.Row]] = {}
        for r in rows:
            groups.setdefault((r["url"], r["kind"]), []).append(r)
        return groups

    def find_file(self, folder: str, sha256: str) -> str | None:
        row = self.db.execute("SELECT path FROM files WHERE folder = ? AND sha256 = ?",
                              (folder, sha256)).fetchone()
        return row[0] if row else None

    def add_file(self, folder: str, sha256: str, path: str, size: int) -> None:
        self.db.execute("INSERT OR REPLACE INTO files(folder, sha256, path, size) VALUES (?, ?, ?, ?)",
                        (folder, sha256, path, size))

    def mark_downloaded(self, att_id: int, *, path: str, size: int, sha256: str, content_type: str,
                        resolved_url: str | None, http_status: int, is_duplicate: bool) -> None:
        self.db.execute(
            """UPDATE attachments SET status='downloaded', reason=NULL, file_path=?, file_size=?,
                   sha256=?, content_type=?, resolved_url=?, http_status=?, is_duplicate=?,
                   retryable=0, attempts=attempts+1, updated_at=? WHERE id=?""",
            (path, size, sha256, content_type, resolved_url, http_status, int(is_duplicate), _now(), att_id))

    def mark_result(self, att_id: int, status: Status, reason: str, *, http_status: int | None = None,
                    retryable: bool = False, resolved_url: str | None = None) -> None:
        self.db.execute(
            """UPDATE attachments SET status=?, reason=?, http_status=?, retryable=?,
                   resolved_url=COALESCE(?, resolved_url), attempts=attempts+1, updated_at=?
               WHERE id=? AND status <> 'downloaded'""",  # успешный результат не перетираем
            (status.value, reason, http_status, int(retryable), resolved_url, _now(), att_id))

    # --- отчёты -----------------------------------------------------------
    def report_rows(self, start: date | None, end: date | None, statuses: Iterable[str]) -> list[sqlite3.Row]:
        where, params = self._range_sql(start, end)
        statuses = list(statuses)
        marks = ",".join("?" * len(statuses))
        return self.db.execute(
            f"""SELECT a.*, c.kind AS conv_kind, c.title AS conv_title, c.folder
                FROM attachments a JOIN conversations c ON c.peer_id = a.peer_id
                WHERE a.status IN ({marks}) AND {where}
                ORDER BY a.peer_id, a.msg_date, a.message_id, a.att_index""",
            statuses + params).fetchall()

    def all_rows(self, start: date | None, end: date | None) -> list[sqlite3.Row]:
        return self.report_rows(start, end, [s.value for s in Status])
