"""Полный цикл «анализ → скачивание → отчёты». Общий для командной строки и графического окна."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from .archive_reader import ArchiveReader
from .downloader import Downloader
from .lock import OutputLock, OutputLocked
from .reports import build_summary, format_summary, write_reports
from .scanner import scan
from .storage import Store

# on_progress(этап, сделано, всего); этап — "scan" или "download".
ProgressCallback = Callable[[str, int, int], None]


class ExportError(Exception):
    """Ошибка, понятная пользователю (нет архива, папка занята и т.п.)."""


class Cancelled(Exception):
    pass


@dataclass
class ExportOptions:
    archive: Path
    output: Path | None = None
    start: date | None = None
    end: date | None = None
    scan_only: bool = False
    concurrency: int = 5
    retries: int = 5
    soft_retries: int = 3
    timeout: float = 60.0
    retry_failed: bool = False
    limit: int | None = None
    hardlinks: bool = True


@dataclass
class ExportResult:
    summary: dict
    text: str
    out_dir: Path | None
    cancelled: bool = False


def run_export(opts: ExportOptions, *, log: Callable[[str], None] = print,
               on_progress: ProgressCallback | None = None, show_progress: bool = True,
               stop_event: threading.Event | None = None) -> ExportResult:
    if opts.start and opts.end and opts.start > opts.end:
        raise ExportError("Начало периода позже конца")
    try:
        reader = ArchiveReader(opts.archive)
    except (FileNotFoundError, ValueError) as e:
        raise ExportError(str(e)) from e

    out_dir = Path(opts.output or "VK_Photos")
    persist = not opts.scan_only or opts.output is not None
    lock = OutputLock(out_dir)
    if persist:
        out_dir.mkdir(parents=True, exist_ok=True)
        try:
            lock.acquire()
        except OutputLocked as e:
            reader.close()
            raise ExportError(f"{e}. Дождитесь его завершения.") from e
    try:
        return _run(opts, reader, out_dir if persist else None, log, on_progress, show_progress, stop_event)
    finally:
        lock.release()


def _run(opts: ExportOptions, reader: ArchiveReader, out_dir: Path | None, log, on_progress,
         show_progress: bool, stop_event: threading.Event | None) -> ExportResult:
    store = Store(out_dir / "progress.db" if out_dir else ":memory:")
    try:
        store.set_meta("archive", str(Path(opts.archive).resolve()))
        with reader:
            stats = scan(reader, store, opts.start, opts.end, show_progress,
                         on_progress=on_progress, stop_event=stop_event)
        extra = {
            "archive": str(Path(opts.archive).resolve()),
            "owner_id": store.get_meta("owner_id"),
            "pages_total": stats.pages_total,
            "conversations_total": stats.conversations_total,
            "pages_parsed": stats.pages_parsed,
            "messages_in_range": stats.messages_in_range,
            "undated_messages": stats.undated_messages,
            "undated_examples": stats.undated_examples,
        }
        if stats.cancelled:
            raise Cancelled()

        if opts.scan_only:
            summary = (write_reports(store, out_dir, opts.start, opts.end, extra) if out_dir
                       else build_summary(store, opts.start, opts.end, extra))
            return ExportResult(summary, format_summary(summary, scan_only=True), out_dir)

        requeued = store.reset_stale(out_dir)
        if requeued:
            log(f"Файлов, пропавших с диска и поставленных в очередь заново: {requeued}")
        groups = store.jobs(opts.start, opts.end, opts.retry_failed)
        if opts.limit is not None:
            groups = dict(list(groups.items())[:opts.limit])
        log(f"К обработке: {len(groups)} уникальных ссылок ({sum(len(v) for v in groups.values())} вложений)")

        downloader = Downloader(store, out_dir, concurrency=opts.concurrency, max_retries=opts.retries,
                                timeout=opts.timeout, soft_retries=opts.soft_retries,
                                show_progress=show_progress, use_hardlinks=opts.hardlinks,
                                on_progress=on_progress, stop_event=stop_event)
        cancelled = False
        try:
            asyncio.run(downloader.run(groups))
        except KeyboardInterrupt:
            cancelled = True
        cancelled = cancelled or bool(stop_event and stop_event.is_set())

        summary = write_reports(store, out_dir, opts.start, opts.end, extra)
        return ExportResult(summary, format_summary(summary, scan_only=False), out_dir, cancelled)
    finally:
        store.close()
