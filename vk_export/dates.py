"""Разбор дат из заголовков сообщений архива VK и фильтрация по периоду."""

from __future__ import annotations

import re
from datetime import date, datetime

# Ключ — первые три буквы названия месяца (в нижнем регистре).
_MONTHS = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "май": 5, "мая": 5, "мае": 5,
    "июн": 6, "июл": 7, "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

_TIME = r"(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?(?:\s*(?P<ampm>[ap]\.?m\.?))?"
_EDITED = r"(?:\s*\((?:ред\.?|edited)\))?"

# «21 сен 2026 в 10:46:37», «1 января 2014 г. в 0:05», «21 Sep 2026 at 10:46:37»
_DMY = re.compile(
    r"(?P<d>\d{1,2})\s+(?P<mon>[A-Za-zА-Яа-яЁё]{3,})\.?\s+(?P<y>\d{4})(?:\s*г\.?)?"
    r"(?:\s*(?:в|at|,)?\s*" + _TIME + r")?" + _EDITED + r"\s*$",
    re.IGNORECASE,
)
# «Sep 21, 2026 at 10:46:37 am»
_MDY = re.compile(
    r"(?P<mon>[A-Za-z]{3,})\.?\s+(?P<d>\d{1,2}),?\s+(?P<y>\d{4})"
    r"(?:\s*(?:at|,)?\s*" + _TIME + r")?" + _EDITED + r"\s*$",
    re.IGNORECASE,
)
# «21.09.2026 в 10:46:37» / «21.09.2026, 10:46»
_NUMERIC = re.compile(
    r"(?P<d>\d{1,2})\.(?P<mo>\d{1,2})\.(?P<y>\d{4})"
    r"(?:\s*(?:в|at|,)?\s*" + _TIME + r")?" + _EDITED + r"\s*$",
    re.IGNORECASE,
)

_PATTERNS = (_DMY, _MDY, _NUMERIC)


def _month(token: str) -> int | None:
    return _MONTHS.get(token[:3].lower())


def find_header_date(text: str) -> tuple[datetime, int] | None:
    """Находит дату в конце заголовка «Автор, дата».

    Возвращает (дата, позиция начала даты), чтобы вызывающий мог отделить имя
    автора (оно само может содержать запятые). None — если дата не распознана.
    """
    text = text.strip()
    for pattern in _PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        gd = m.groupdict()
        month = int(gd["mo"]) if gd.get("mo") else _month(gd["mon"])
        if not month:
            continue
        hour = int(gd["h"]) if gd["h"] else 0
        if gd.get("ampm"):
            ampm = gd["ampm"].lower().replace(".", "")
            if ampm == "pm" and hour < 12:
                hour += 12
            elif ampm == "am" and hour == 12:
                hour = 0
        try:
            dt = datetime(int(gd["y"]), month, int(gd["d"]), hour,
                          int(gd["mi"] or 0), int(gd["s"] or 0))
        except ValueError:
            continue
        return dt, m.start()
    return None


def parse_vk_datetime(text: str) -> datetime | None:
    found = find_header_date(text)
    return found[0] if found else None


def in_range(dt: datetime | None, start: date | None, end: date | None) -> bool:
    """Включительная проверка по календарной дате сообщения.

    Сообщение от 31.12.2016 23:59:59 попадает в период, заканчивающийся 2016-12-31.
    """
    if dt is None:
        return False
    d = dt.date()
    if start and d < start:
        return False
    if end and d > end:
        return False
    return True


def parse_cli_date(value: str) -> date:
    return date.fromisoformat(value)
