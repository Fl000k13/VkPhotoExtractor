"""Асинхронное скачивание изображений с повторами, проверкой содержимого и дедупликацией.

Без авторизации: запросы идут без cookies и токенов. Если VK требует вход
(редирект на login.vk.*) — вложение помечается как недоступное, обхода нет.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
import re
import shutil
import sqlite3
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from tqdm import tqdm
from yarl import URL

from .models import AttachmentKind, Status
from .parser import IMAGE_EXTS
from .storage import Store

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

# Коды, при которых повтор имеет смысл. 424 VK отдаёт при сбое хранилища старых фото.
RETRYABLE_STATUSES = {408, 424, 425, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
PERMANENT_REASONS = {404: "not_found", 410: "gone", 403: "forbidden", 401: "unauthorized", 451: "blocked"}
# Проверено на реальном архиве: CDN VK примерно в 10–15% случаев отвечает 404/403 на
# существующее фото, а через несколько секунд отдаёт его. Такие ответы повторяем
# ограниченное число раз (--soft-retries).
SOFT_STATUSES = {403, 404, 410}

_INIT_DOC_RE = re.compile(r"Docs\.initDoc\((\{.*?\})\)", re.DOTALL)
_LOGIN_HOST_RE = re.compile(r"(^|\.)login\.vk\.(com|ru)$|(^|\.)id\.vk\.(com|ru)$")


def sniff_image(head: bytes) -> tuple[str, str] | None:
    """По сигнатуре возвращает (расширение, MIME) или None, если это не изображение."""
    if head.startswith(b"\xff\xd8\xff"):
        return "jpg", "image/jpeg"
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png", "image/png"
    if head[:6] in (b"GIF87a", b"GIF89a"):
        return "gif", "image/gif"
    if head[:4] == b"RIFF" and head[8:12] == b"WEBP":
        return "webp", "image/webp"
    if head[:2] == b"BM" and len(head) > 14:
        return "bmp", "image/bmp"
    if head[:4] in (b"II*\x00", b"MM\x00*"):
        return "tif", "image/tiff"
    if head[4:8] == b"ftyp":
        brand = head[8:12]
        if brand in (b"avif", b"avis"):
            return "avif", "image/avif"
        if brand in (b"heic", b"heix", b"hevc", b"heim", b"heis", b"mif1", b"msf1"):
            return "heic", "image/heic"
    return None


class FetchError(Exception):
    def __init__(self, reason: str, *, http_status: int | None = None, retryable: bool = False,
                 retry_after: float | None = None, status: Status = Status.FAILED, soft: bool = False):
        super().__init__(reason)
        self.reason = reason
        self.http_status = http_status
        self.retryable = retryable
        # «Мягкая» ошибка: 404/403 от CDN VK, которые иногда оказываются временными.
        self.soft = soft
        self.retry_after = retry_after
        self.status = status


@dataclass
class Fetched:
    temp_path: Path
    size: int
    sha256: str
    ext: str
    content_type: str
    http_status: int
    final_url: str


@dataclass
class DownloadStats:
    groups: int = 0
    downloaded_files: int = 0
    reused_files: int = 0
    failed: int = 0
    skipped: int = 0


def _retry_after(headers) -> float | None:
    value = headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        try:
            return max(0.0, (parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
        except (TypeError, ValueError):
            return None


def _is_login_redirect(location: str | None) -> bool:
    if not location:
        return False
    return bool(_LOGIN_HOST_RE.search(urlsplit(location).netloc.lower()))


class Downloader:
    def __init__(self, store: Store, out_dir: Path, *, concurrency: int = 5, max_retries: int = 5,
                 timeout: float = 60.0, connect_timeout: float = 15.0, backoff_base: float = 1.0,
                 backoff_max: float = 60.0, show_progress: bool = True, use_hardlinks: bool = True,
                 soft_retries: int = 3, soft_delay: float = 3.0, on_progress=None,
                 stop_event: threading.Event | None = None):
        self.store = store
        self.out_dir = out_dir
        self.tmp_dir = out_dir / ".tmp"
        self.concurrency = max(1, concurrency)
        self.max_retries = max(0, max_retries)
        self.timeout = aiohttp.ClientTimeout(total=timeout, connect=connect_timeout, sock_read=timeout / 2)
        self.backoff_base = backoff_base
        self.backoff_max = backoff_max
        self.show_progress = show_progress
        self.use_hardlinks = use_hardlinks
        self.soft_retries = max(0, soft_retries)
        self.soft_delay = soft_delay
        self.on_progress = on_progress
        self.stop_event = stop_event
        self.stats = DownloadStats()
        # Число одновременных запросов ограничено числом воркеров (= concurrency).
        # Страницы vk.com запрашиваем бережнее, чтобы не нарваться на ограничения.
        self._page_sem = asyncio.Semaphore(min(2, self.concurrency))
        self._db_lock = asyncio.Lock()

    # --- подготовка -------------------------------------------------------
    def cleanup_temp(self) -> None:
        """Удаляет недокачанные временные файлы прошлого (прерванного) запуска."""
        if self.tmp_dir.exists():
            shutil.rmtree(self.tmp_dir, ignore_errors=True)
        self.tmp_dir.mkdir(parents=True, exist_ok=True)

    # --- сеть -------------------------------------------------------------
    def _backoff(self, attempt: int, retry_after: float | None) -> float:
        if retry_after is not None:
            return min(retry_after, self.backoff_max * 5)
        delay = self.backoff_base * (2 ** attempt)
        return min(self.backoff_max, delay) * (0.75 + random.random() * 0.5)

    async def _with_retries(self, func, *args):
        attempt = 0
        soft_attempt = 0
        while True:
            try:
                return await func(*args)
            except FetchError as e:
                if e.soft and soft_attempt < self.soft_retries:
                    await asyncio.sleep(self.soft_delay * (soft_attempt + 1) * (0.75 + random.random() * 0.5))
                    soft_attempt += 1
                    continue
                if not e.retryable or attempt >= self.max_retries:
                    raise
                await asyncio.sleep(self._backoff(attempt, e.retry_after))
            except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
                if attempt >= self.max_retries:
                    raise FetchError(f"network:{type(e).__name__}", retryable=True) from e
                await asyncio.sleep(self._backoff(attempt, None))
            attempt += 1

    @staticmethod
    def _check_status(resp: aiohttp.ClientResponse) -> None:
        if resp.status in (301, 302, 303, 307, 308) and _is_login_redirect(resp.headers.get("Location")):
            raise FetchError("requires_auth", http_status=resp.status)
        if 200 <= resp.status < 300:
            return
        if resp.status in RETRYABLE_STATUSES:
            raise FetchError(f"http_{resp.status}", http_status=resp.status, retryable=True,
                             retry_after=_retry_after(resp.headers) if resp.status in (429, 503) else None)
        reason = PERMANENT_REASONS.get(resp.status, f"http_{resp.status}")
        raise FetchError(reason, http_status=resp.status, soft=resp.status in SOFT_STATUSES)

    async def _fetch_image(self, session: aiohttp.ClientSession, url: str) -> Fetched:
        # Редиректы обрабатываем вручную, чтобы не ходить на страницу входа VK.
        for _ in range(6):
            async with session.get(url, allow_redirects=False) as resp:
                location = resp.headers.get("Location")
                if resp.status in (301, 302, 303, 307, 308) and location \
                        and not _is_login_redirect(location):
                    url = str(resp.url.join(URL(location)))
                    continue
                self._check_status(resp)
                return await self._store_body(resp)
        raise FetchError("too_many_redirects")

    async def _store_body(self, resp: aiohttp.ClientResponse) -> Fetched:
        content_type = resp.headers.get("Content-Type", "").split(";")[0].strip().lower()
        if content_type.startswith("text/") or content_type in ("application/json", "application/xhtml+xml"):
            raise FetchError(f"not_image_response:{content_type}", http_status=resp.status)
        expected = resp.content_length
        fd, tmp_name = tempfile.mkstemp(dir=self.tmp_dir, suffix=".part")
        tmp = Path(tmp_name)
        sha = hashlib.sha256()
        size = 0
        head = b""
        try:
            with os.fdopen(fd, "wb") as f:
                async for chunk in resp.content.iter_chunked(64 * 1024):
                    if len(head) < 32:
                        head += chunk[:32 - len(head)]
                        if len(head) >= 32 and sniff_image(head) is None:
                            raise FetchError(f"not_image_response:{content_type or 'unknown'}",
                                             http_status=resp.status)
                    f.write(chunk)
                    sha.update(chunk)
                    size += len(chunk)
            if size == 0:
                raise FetchError("empty_response", http_status=resp.status, retryable=True)
            if expected is not None and size != expected:
                raise FetchError("truncated_response", http_status=resp.status, retryable=True)
            sniffed = sniff_image(head)
            if sniffed is None:
                raise FetchError(f"not_image_response:{content_type or 'unknown'}", http_status=resp.status)
            return Fetched(tmp, size, sha.hexdigest(), sniffed[0], sniffed[1], resp.status, str(resp.url))
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise

    async def _resolve_doc(self, session: aiohttp.ClientSession, url: str) -> tuple[str, str]:
        """Страница документа → (прямой URL, расширение). Работает только для публичных документов."""
        async with self._page_sem:
            async with session.get(url, allow_redirects=False) as resp:
                self._check_status(resp)
                raw = await resp.read()
        text = raw.decode(resp.charset or "cp1251", errors="replace")
        m = _INIT_DOC_RE.search(text)
        if not m:
            if "message_page_body" in text or "Ошибка" in text[:5000]:
                raise FetchError("doc_unavailable_or_private", http_status=resp.status)
            raise FetchError("doc_page_unrecognized", http_status=resp.status)
        try:
            info = json.loads(m.group(1))
        except json.JSONDecodeError:
            raise FetchError("doc_page_unrecognized", http_status=resp.status)
        doc_url = info.get("docUrl")
        ext = str(info.get("docExt", "")).lower()
        if not doc_url:
            raise FetchError("doc_url_missing", http_status=resp.status)
        if ext and ext not in IMAGE_EXTS:
            raise FetchError(f"not_image_doc:{ext}", http_status=resp.status, status=Status.SKIPPED)
        return doc_url, ext

    async def _probe_photo_page(self, session: aiohttp.ClientSession, url: str) -> None:
        """Ссылки-страницы на фото: проверяем, доступны ли без входа. Практически всегда — нет."""
        async with self._page_sem:
            async with session.get(url, allow_redirects=False) as resp:
                self._check_status(resp)
                raise FetchError("photo_page_no_public_image", http_status=resp.status)

    # --- раскладка файлов ---------------------------------------------------
    def _target_name(self, row: sqlite3.Row, ext: str) -> str:
        dt = row["msg_date"].replace("-", "").replace(":", "").replace("T", "_")
        return f"{dt}_{row['message_id']}_{row['att_index']}.{ext}"

    def _materialize(self, fetched: Fetched, rows: list[sqlite3.Row]) -> list[tuple[sqlite3.Row, str, bool]]:
        """Кладёт файл в папку каждой переписки/года. Возвращает (row, путь, дубликат?)."""
        results = []
        first_abs: Path | None = None
        temp_used = False
        for row in rows:
            folder = f"{row['folder']}/{row['msg_date'][:4]}"
            own_name = self._target_name(row, fetched.ext)
            existing = self.store.find_file(folder, fetched.sha256)
            if existing and (self.out_dir / existing).is_file():
                # Дубликат — если файл был сохранён для другого вложения, а не для этого же.
                own_stem = own_name.rsplit(".", 1)[0]
                results.append((row, existing, not existing.rsplit("/", 1)[-1].startswith(own_stem)))
                continue
            rel = f"{folder}/{own_name}"
            dest = self.out_dir / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            if dest.exists() and _sha256_file(dest) != fetched.sha256:
                dest = dest.with_stem(dest.stem + "_" + fetched.sha256[:8])
                rel = dest.relative_to(self.out_dir).as_posix()
            if not dest.exists():
                if not temp_used:
                    os.replace(fetched.temp_path, dest)
                    temp_used = True
                    first_abs = dest
                else:
                    self._link_or_copy(first_abs, dest)
            self.store.add_file(folder, fetched.sha256, rel, fetched.size)
            results.append((row, rel, False))
        if not temp_used:
            fetched.temp_path.unlink(missing_ok=True)
        return results

    def _adopt_existing(self, row: sqlite3.Row) -> bool:
        """Файл этого вложения уже лежит на диске (прошлый запуск успел записать файл, но не
        базу), а сейчас сервер ответил ошибкой. Принимаем файл, если это целое изображение."""
        folder = f"{row['folder']}/{row['msg_date'][:4]}"
        stem = self._target_name(row, "x")[:-2]
        for path in sorted((self.out_dir / folder).glob(f"{stem}.*")):
            data = path.read_bytes()
            sniffed = sniff_image(data[:32])
            if not sniffed or path.suffix.lower() != f".{sniffed[0]}":
                continue
            sha = hashlib.sha256(data).hexdigest()
            rel = path.relative_to(self.out_dir).as_posix()
            self.store.add_file(folder, sha, rel, len(data))
            self.store.mark_downloaded(row["id"], path=rel, size=len(data), sha256=sha,
                                       content_type=sniffed[1], resolved_url=None, http_status=0,
                                       is_duplicate=False)
            self.stats.downloaded_files += 1
            return True
        return False

    def _link_or_copy(self, src: Path, dest: Path) -> None:
        tmp = dest.with_name(dest.name + ".part")
        if self.use_hardlinks:
            try:
                os.link(src, tmp)
                os.replace(tmp, dest)
                return
            except OSError:
                tmp.unlink(missing_ok=True)
        shutil.copy2(src, tmp)
        os.replace(tmp, dest)

    # --- обработка одной группы (один URL) ---------------------------------
    async def _process(self, session: aiohttp.ClientSession, rows: list[sqlite3.Row]) -> None:
        url = rows[0]["url"]
        kind = AttachmentKind(rows[0]["kind"])
        resolved = None
        try:
            if kind is AttachmentKind.PHOTO_PAGE:
                await self._with_retries(self._probe_photo_page, session, url)
            if kind is AttachmentKind.DOC_PAGE:
                resolved, _ = await self._with_retries(self._resolve_doc, session, url)
            fetched = await self._with_retries(self._fetch_image, session, resolved or url)
        except FetchError as e:
            status = e.status
            reason = e.reason
            # Документ «Файл», оказавшийся не изображением, — это пропуск, а не ошибка.
            if kind in (AttachmentKind.DOC_IMAGE, AttachmentKind.DOC_PAGE) and reason.startswith("not_image"):
                status = Status.SKIPPED
            async with self._db_lock:
                for row in rows:
                    if status is Status.FAILED and self._adopt_existing(row):
                        continue
                    self.store.mark_result(row["id"], status, reason, http_status=e.http_status,
                                           retryable=e.retryable, resolved_url=resolved)
                    if status is Status.SKIPPED:
                        self.stats.skipped += 1
                    else:
                        self.stats.failed += 1
                self.store.commit()
            return

        async with self._db_lock:
            try:
                placed = self._materialize(fetched, rows)
            except OSError as e:
                fetched.temp_path.unlink(missing_ok=True)
                for row in rows:
                    self.store.mark_result(row["id"], Status.FAILED, f"write_error:{e}", retryable=True)
                self.store.commit()
                self.stats.failed += len(rows)
                return
            for row, rel, dup in placed:
                self.store.mark_downloaded(row["id"], path=rel, size=fetched.size, sha256=fetched.sha256,
                                           content_type=fetched.content_type,
                                           resolved_url=resolved or (fetched.final_url if fetched.final_url != url else None),
                                           http_status=fetched.http_status, is_duplicate=dup)
                if dup:
                    self.stats.reused_files += 1
                else:
                    self.stats.downloaded_files += 1
            self.store.commit()

    async def run(self, groups: dict[tuple[str, str], list[sqlite3.Row]]) -> DownloadStats:
        self.stats.groups = len(groups)
        if not groups:
            return self.stats
        self.cleanup_temp()
        connector = aiohttp.TCPConnector(limit=self.concurrency * 2, ttl_dns_cache=300)
        headers = {"User-Agent": USER_AGENT, "Accept": "image/avif,image/webp,image/*,*/*;q=0.8",
                   "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8"}
        bar = tqdm(total=len(groups), desc="Скачивание", unit="url", disable=not self.show_progress)
        queue: asyncio.Queue = asyncio.Queue()
        for rows in groups.values():
            queue.put_nowait(rows)

        total = len(groups)
        done = 0
        if self.on_progress is not None:
            self.on_progress("download", 0, total)

        async with aiohttp.ClientSession(headers=headers, timeout=self.timeout, connector=connector,
                                         cookie_jar=aiohttp.DummyCookieJar()) as session:
            async def worker():
                nonlocal done
                while True:
                    # Остановка: новые ссылки не берём, начатые доводим до конца и сохраняем.
                    if self.stop_event is not None and self.stop_event.is_set():
                        return
                    try:
                        rows = queue.get_nowait()
                    except asyncio.QueueEmpty:
                        return
                    try:
                        await self._process(session, rows)
                    except asyncio.CancelledError:
                        raise
                    except Exception as e:  # одна сбойная ссылка не должна останавливать весь прогон
                        async with self._db_lock:
                            for row in rows:
                                self.store.mark_result(row["id"], Status.FAILED,
                                                       f"error:{type(e).__name__}:{e}"[:200], retryable=True)
                            self.store.commit()
                        self.stats.failed += len(rows)
                    finally:
                        done += 1
                        if self.on_progress is not None:
                            self.on_progress("download", done, total)
                        bar.update(1)
                        bar.set_postfix(ok=self.stats.downloaded_files + self.stats.reused_files,
                                        fail=self.stats.failed, skip=self.stats.skipped, refresh=False)

            workers = [asyncio.create_task(worker()) for _ in range(self.concurrency)]
            try:
                await asyncio.gather(*workers)
            finally:
                for w in workers:
                    w.cancel()
                bar.close()
        shutil.rmtree(self.tmp_dir, ignore_errors=True)
        return self.stats


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
