"""Единый доступ к архиву VK: ZIP-файл или уже распакованная директория.

Файлы читаются по одному, вся история в память не загружается.
"""

from __future__ import annotations

import base64
import json
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

# messages/<peer_id>/messages<offset>.html, возможно с префиксом корневой папки.
_PAGE_RE = re.compile(r"(?:^|/)messages/(-?\d+)/messages(\d+)\.html?$", re.IGNORECASE)
_JD_RE = re.compile(rb'<meta\s+name="jd"\s+content="([^"]+)"', re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class PageRef:
    peer_id: int
    offset: int
    path: str  # путь внутри архива (через '/')


class ArchiveReader:
    def __init__(self, source: str | Path):
        self.source = Path(source)
        if not self.source.exists():
            raise FileNotFoundError(f"Архив не найден: {self.source}")
        self._zip: zipfile.ZipFile | None = None
        if self.source.is_file():
            if not zipfile.is_zipfile(self.source):
                raise ValueError(f"Файл не является ZIP-архивом: {self.source}")
            self._zip = zipfile.ZipFile(self.source)

    def close(self) -> None:
        if self._zip is not None:
            self._zip.close()

    def __enter__(self) -> "ArchiveReader":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def _names(self) -> Iterator[str]:
        if self._zip is not None:
            yield from self._zip.namelist()
        else:
            for p in self.source.rglob("*.htm*"):
                yield p.relative_to(self.source).as_posix()

    def message_pages(self) -> list[PageRef]:
        """Все страницы переписок, упорядоченные по пиру и смещению."""
        pages = []
        for name in self._names():
            m = _PAGE_RE.search(name)
            if m:
                pages.append(PageRef(int(m.group(1)), int(m.group(2)), name))
        pages.sort(key=lambda p: (p.peer_id, p.offset))
        return pages

    def read(self, path: str) -> bytes:
        if self._zip is not None:
            return self._zip.read(path)
        return (self.source / path).read_bytes()

    def owner_id(self, sample_page: PageRef | None = None) -> int | None:
        """id владельца архива из метатега jd (base64-JSON с полем user_id)."""
        if sample_page is None:
            pages = self.message_pages()
            if not pages:
                return None
            sample_page = pages[0]
        m = _JD_RE.search(self.read(sample_page.path)[:4096])
        if not m:
            return None
        raw = m.group(1).decode("ascii", "ignore")
        try:
            data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
            return int(data["user_id"])
        except (ValueError, KeyError, TypeError):
            return None
