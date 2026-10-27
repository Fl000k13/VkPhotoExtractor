"""Блокировка папки вывода: два процесса не должны писать в одну progress.db и одну .tmp.

Используется блокировка ОС (msvcrt / fcntl), она снимается автоматически,
даже если процесс был убит, поэтому «зависших» блокировок не бывает.
"""

from __future__ import annotations

import os
from pathlib import Path


class OutputLocked(RuntimeError):
    pass


class OutputLock:
    def __init__(self, out_dir: Path):
        self.path = out_dir / "progress.lock"
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self.path, "a+")
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self._fh.close()
            self._fh = None
            raise OutputLocked(f"Папка {self.path.parent} уже используется другим запуском программы")

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if os.name == "nt":
                import msvcrt
                self._fh.seek(0)
                msvcrt.locking(self._fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        self._fh.close()
        self._fh = None

    def __enter__(self) -> "OutputLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
