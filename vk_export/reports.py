"""Отчёты: summary.json, downloaded.csv, failed.csv, skipped.csv и вывод статистики в консоль."""

from __future__ import annotations

import csv
import json
from collections import Counter, defaultdict
from datetime import date, datetime
from pathlib import Path

from .models import AttachmentKind, ConversationKind, Status
from .storage import Store

CSV_COLUMNS = [
    ("peer_id", "peer_id"), ("conversation_kind", "conv_kind"), ("conversation_title", "conv_title"),
    ("message_id", "message_id"), ("att_index", "att_index"), ("message_date", "msg_date"),
    ("author_name", "author_name"), ("author_id", "author_id"), ("kind", "kind"),
    ("source_url", "url"), ("resolved_url", "resolved_url"), ("vk_ref", "photo_ref"),
    ("status", "status"), ("reason", "reason"), ("http_status", "http_status"),
    ("file_path", "file_path"), ("file_size", "file_size"), ("sha256", "sha256"),
    ("is_duplicate", "is_duplicate"), ("attempts", "attempts"), ("source_page", "source_page"),
]

PHOTO_KINDS = {AttachmentKind.PHOTO.value, AttachmentKind.PHOTO_PAGE.value, AttachmentKind.PHOTO_NO_LINK.value}
DOWNLOADABLE = {k.value for k in AttachmentKind if k.downloadable}

KIND_LABELS = {
    "photo": "Фото (прямая ссылка)",
    "photo_page": "Фото (ссылка на страницу VK)",
    "photo_no_link": "Фото без ссылки",
    "doc_page": "Документ (страница vk.com/doc)",
    "doc_image": "Документ (прямая ссылка)",
    "voice": "Голосовое сообщение",
    "doc_other": "Документ, не изображение",
    "forwarded": "Пересланные сообщения",
    "wall_post": "Запись на стене",
}
CONV_LABELS = {"personal": "Личные", "group_chat": "Беседы", "community": "Сообщества"}


def _write_csv(path: Path, rows) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".part")
    # utf-8-sig — чтобы Excel в Windows корректно открыл кириллицу.
    with open(tmp, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow([name for name, _ in CSV_COLUMNS])
        for r in rows:
            w.writerow(["" if r[col] is None else r[col] for _, col in CSV_COLUMNS])
    tmp.replace(path)


def build_summary(store: Store, start: date | None, end: date | None, extra: dict | None = None) -> dict:
    rows = store.all_rows(start, end)
    photos_by_year = defaultdict(Counter)       # год → тип переписки → число фото-вложений
    kinds = Counter()
    status_by_kind = defaultdict(Counter)
    failed_reasons = Counter()
    skipped_reasons = Counter()
    conversations: dict[int, dict] = {}
    urls = set()
    files = set()
    sha = set()
    downloaded_records = 0
    duplicate_records = 0
    bytes_total = 0

    for r in rows:
        kinds[r["kind"]] += 1
        status_by_kind[r["kind"]][r["status"]] += 1
        conv = conversations.setdefault(r["peer_id"], {
            "peer_id": r["peer_id"], "kind": r["conv_kind"], "title": r["conv_title"],
            "folder": r["folder"], "photos": 0, "candidates": 0, "downloaded": 0, "failed": 0, "years": Counter()})
        if r["kind"] in PHOTO_KINDS:
            photos_by_year[r["msg_date"][:4]][r["conv_kind"]] += 1
            conv["photos"] += 1
            conv["years"][r["msg_date"][:4]] += 1
        if r["kind"] in DOWNLOADABLE:
            conv["candidates"] += 1
            if r["url"]:
                urls.add(r["url"])
        if r["status"] == Status.DOWNLOADED:
            downloaded_records += 1
            conv["downloaded"] += 1
            duplicate_records += r["is_duplicate"]
            if r["file_path"] not in files:
                files.add(r["file_path"])
                bytes_total += r["file_size"] or 0
            sha.add(r["sha256"])
        elif r["status"] == Status.FAILED:
            failed_reasons[r["reason"]] += 1
            conv["failed"] += 1
        elif r["status"] == Status.SKIPPED and r["kind"] in DOWNLOADABLE:
            skipped_reasons[r["reason"]] += 1

    candidates = sum(v for k, v in kinds.items() if k in DOWNLOADABLE)
    pending = sum(c[Status.PENDING] for k, c in status_by_kind.items() if k in DOWNLOADABLE)
    conv_list = sorted((c for c in conversations.values() if c["candidates"] or c["photos"]),
                       key=lambda c: -c["photos"])
    for c in conv_list:
        c["years"] = dict(sorted(c["years"].items()))

    return {
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "period": {"start": start.isoformat() if start else None, "end": end.isoformat() if end else None},
        **(extra or {}),
        "photo_attachments_by_year": {
            y: {**{k: c.get(k, 0) for k in ConversationKind}, "total": sum(c.values())}
            for y, c in sorted(photos_by_year.items())},
        "photo_attachments_total": sum(sum(c.values()) for c in photos_by_year.values()),
        "attachments_by_kind": dict(kinds.most_common()),
        "download_candidates": candidates,
        "unique_source_urls": len(urls),
        "conversations_with_photos": len([c for c in conv_list if c["photos"]]),
        "group_chats_with_photos": len([c for c in conv_list if c["photos"] and c["kind"] == "group_chat"]),
        "results": {
            "downloaded_records": downloaded_records,
            "downloaded_records_reusing_existing_file": duplicate_records,
            "unique_files_on_disk": len(files),
            "unique_images_sha256": len(sha),
            "bytes_on_disk": bytes_total,
            "failed_records": sum(failed_reasons.values()),
            "failed_by_reason": dict(failed_reasons.most_common()),
            "skipped_candidates_by_reason": dict(skipped_reasons.most_common()),
            "pending_records": pending,
        },
        "status_by_kind": {k: dict(v) for k, v in status_by_kind.items()},
        "conversations": conv_list,
    }


def write_reports(store: Store, out_dir: Path, start: date | None, end: date | None,
                  extra: dict | None = None) -> dict:
    reports = out_dir / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    summary = build_summary(store, start, end, extra)
    _write_csv(reports / "downloaded.csv", store.report_rows(start, end, [Status.DOWNLOADED.value]))
    _write_csv(reports / "failed.csv", store.report_rows(start, end, [Status.FAILED.value, Status.PENDING.value]))
    _write_csv(reports / "skipped.csv", store.report_rows(start, end, [Status.SKIPPED.value]))
    tmp = reports / "summary.json.part"
    tmp.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(reports / "summary.json")
    return summary


def format_summary(summary: dict, scan_only: bool) -> str:
    out = []
    p = summary["period"]
    out.append(f"\nПериод: {p['start'] or '…'} — {p['end'] or '…'}")
    if "pages_total" in summary:
        out.append(f"Страниц в архиве: {summary['pages_total']}, переписок: {summary['conversations_total']}, "
                   f"разобрано страниц с сообщениями периода: {summary['pages_parsed']}, "
                   f"сообщений в периоде: {summary['messages_in_range']}")
        if summary.get("undated_messages"):
            out.append(f"ВНИМАНИЕ: сообщений с нераспознанной датой: {summary['undated_messages']}")

    out.append("\nВложения «Фотография» по годам:")
    header = f"{'Год':<6}{'Личные':>10}{'Беседы':>10}{'Сообщества':>12}{'Всего':>10}"
    out.append(header)
    out.append("-" * len(header))
    totals = Counter()
    for year, c in summary["photo_attachments_by_year"].items():
        out.append(f"{year:<6}{c['personal']:>10}{c['group_chat']:>10}{c['community']:>12}{c['total']:>10}")
        totals.update({k: c[k] for k in ("personal", "group_chat", "community", "total")})
    out.append("-" * len(header))
    out.append(f"{'Итого':<6}{totals['personal']:>10}{totals['group_chat']:>10}"
               f"{totals['community']:>12}{totals['total']:>10}")

    out.append(f"\nПереписок с фото: {summary['conversations_with_photos']} "
               f"(из них бесед: {summary['group_chats_with_photos']})")
    out.append(f"Кандидатов на скачивание: {summary['download_candidates']}, "
               f"уникальных ссылок: {summary['unique_source_urls']}")
    out.append("\nВложения по типам:")
    for kind, n in summary["attachments_by_kind"].items():
        out.append(f"  {KIND_LABELS.get(kind, kind):<34}{n:>7}")

    if not scan_only:
        r = summary["results"]
        out.append("\nРезультат:")
        out.append(f"  Скачано записей:            {r['downloaded_records']} "
                   f"(файлов на диске: {r['unique_files_on_disk']}, уникальных изображений: "
                   f"{r['unique_images_sha256']}, {r['bytes_on_disk'] / 1048576:.1f} МБ)")
        out.append(f"  Из них повторные отправки:  {r['downloaded_records_reusing_existing_file']}")
        out.append(f"  Недоступно:                 {r['failed_records']}")
        for reason, n in r["failed_by_reason"].items():
            out.append(f"      {reason:<36}{n:>7}")
        if r["skipped_candidates_by_reason"]:
            out.append("  Пропущено (не изображения):")
            for reason, n in r["skipped_candidates_by_reason"].items():
                out.append(f"      {reason:<36}{n:>7}")
        if r["pending_records"]:
            out.append(f"  Ещё не обработано:          {r['pending_records']}")
    return "\n".join(out)
