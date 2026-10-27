"""Выгрузка фотографий из переписок архива ВКонтакте.

Примеры:
    python main.py --archive Archive.zip --output VK_Photos --start-date 2014-01-01 --end-date 2016-12-31
    python main.py --archive Archive.zip --start-date 2014-01-01 --end-date 2016-12-31 --scan-only
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from vk_export import __version__
from vk_export.dates import parse_cli_date
from vk_export.runner import Cancelled, ExportError, ExportOptions, run_export


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="Скачивание фотографий из личных переписок и бесед архива VK (без токена и авторизации).")
    p.add_argument("--archive", required=True, help="Путь к Archive.zip или к распакованной папке архива")
    p.add_argument("--output", default=None, help="Папка для фотографий и отчётов (по умолчанию VK_Photos)")
    p.add_argument("--start-date", type=parse_cli_date, default=None, help="Начало периода, ГГГГ-ММ-ДД (включительно)")
    p.add_argument("--end-date", type=parse_cli_date, default=None, help="Конец периода, ГГГГ-ММ-ДД (включительно)")
    p.add_argument("--scan-only", action="store_true", help="Только анализ архива и статистика, без скачивания")
    p.add_argument("--concurrency", type=int, default=5, help="Число одновременных загрузок (по умолчанию 5)")
    p.add_argument("--retries", type=int, default=5, help="Повторов при временных ошибках (по умолчанию 5)")
    p.add_argument("--soft-retries", type=int, default=3,
                   help="Повторов при ответах 404/403: CDN VK иногда отдаёт их временно (по умолчанию 3)")
    p.add_argument("--timeout", type=float, default=60.0, help="Тайм-аут одного запроса, сек (по умолчанию 60)")
    p.add_argument("--retry-failed", action="store_true",
                   help="Повторить и окончательно недоступные (404/403 и т.п.), а не только временные ошибки")
    p.add_argument("--limit", type=int, default=None, help="Обработать не более N ссылок (для пробного запуска)")
    p.add_argument("--no-hardlinks", action="store_true",
                   help="Копировать файл вместо жёсткой ссылки, если одно фото есть в нескольких папках")
    p.add_argument("--no-progress", action="store_true", help="Не показывать прогресс-бары")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except AttributeError:
            pass

    args = build_parser().parse_args(argv)
    opts = ExportOptions(
        archive=Path(args.archive), output=Path(args.output) if args.output else None,
        start=args.start_date, end=args.end_date, scan_only=args.scan_only,
        concurrency=args.concurrency, retries=args.retries, soft_retries=args.soft_retries,
        timeout=args.timeout, retry_failed=args.retry_failed, limit=args.limit,
        hardlinks=not args.no_hardlinks)
    try:
        result = run_export(opts, show_progress=not args.no_progress)
    except ExportError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 2
    except (Cancelled, KeyboardInterrupt):
        print("\nПрервано пользователем.", file=sys.stderr)
        return 130

    print(result.text)
    if result.out_dir is not None:
        if not opts.scan_only:
            print(f"\nФотографии: {result.out_dir.resolve()}")
        print(f"Отчёты:     {(result.out_dir / 'reports').resolve()}")
    if result.cancelled:
        print("\nПрервано пользователем. Прогресс сохранён — запустите ту же команду, чтобы продолжить.")
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
