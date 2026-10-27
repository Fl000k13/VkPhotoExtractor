"""Сканирование архива: страницы → вложения за период → записи в базе."""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass, field
from datetime import date

from tqdm import tqdm

from .archive_reader import ArchiveReader
from .dates import in_range
from .models import AttachmentRecord
from .parser import classify, detect_encoding, parse_page, quick_dates
from .storage import Store


@dataclass
class ScanStats:
    pages_total: int = 0
    pages_parsed: int = 0
    conversations_total: int = 0
    messages_in_range: int = 0
    undated_messages: int = 0
    undated_examples: list[str] = field(default_factory=list)
    descriptions: Counter = field(default_factory=Counter)
    cancelled: bool = False


def scan(reader: ArchiveReader, store: Store, start: date | None, end: date | None,
         show_progress: bool = True, on_progress=None,
         stop_event: threading.Event | None = None) -> ScanStats:
    stats = ScanStats()
    pages = reader.message_pages()
    stats.pages_total = len(pages)
    stats.conversations_total = len({p.peer_id for p in pages})
    if not pages:
        return stats

    owner_id = reader.owner_id(pages[0])
    if owner_id is not None:
        store.set_meta("owner_id", str(owner_id))

    for done, page_ref in enumerate(tqdm(pages, desc="Сканирование", unit="стр", disable=not show_progress)):
        if stop_event is not None and stop_event.is_set():
            stats.cancelled = True
            break
        if on_progress is not None and done % 50 == 0:
            on_progress("scan", done, len(pages))
        raw = reader.read(page_ref.path)
        encoding = detect_encoding(raw)
        count, dates = quick_dates(raw, encoding)
        if count and len(dates) == count and all(d is not None for d in dates) \
                and not any(in_range(d, start, end) for d in dates):
            continue  # на странице нет сообщений из периода — полный разбор не нужен

        stats.pages_parsed += 1
        page = parse_page(raw)
        records: list[AttachmentRecord] = []
        for msg in page.messages:
            if msg.dt is None:
                stats.undated_messages += 1
                if len(stats.undated_examples) < 10:
                    stats.undated_examples.append(f"{page_ref.path} #{msg.message_id}: {msg.dt_raw[:80]}")
                continue
            if not in_range(msg.dt, start, end):
                continue
            stats.messages_in_range += 1
            author_id = owner_id if msg.is_owner else msg.author_id
            for index, att in enumerate(msg.attachments):
                kind, url, ref = classify(att)
                stats.descriptions[att.description] += 1
                if not kind.tracked:
                    continue
                records.append(AttachmentRecord(
                    peer_id=page_ref.peer_id, message_id=msg.message_id, att_index=index,
                    msg_date=msg.dt, author_name=msg.author_name, author_id=author_id,
                    kind=kind, description=att.description, url=url, photo_ref=ref,
                    source_page=page_ref.path))
        if records:
            store.upsert_conversation(page_ref.peer_id, page.title)
            store.add_attachments(records)
            store.commit()
    store.commit()
    if on_progress is not None and not stats.cancelled:
        on_progress("scan", len(pages), len(pages))
    return stats
