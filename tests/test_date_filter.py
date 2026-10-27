from datetime import date, datetime

import pytest

from vk_export.dates import find_header_date, in_range, parse_vk_datetime


@pytest.mark.parametrize("text, expected", [
    ("Иван Петров, 21 сен 2026 в 10:46:37", datetime(2026, 9, 21, 10, 46, 37)),
    ("Вы, 15 мая 2025 в 13:16:00", datetime(2025, 5, 15, 13, 16, 0)),
    ("Имя, 1 янв 2014 в 0:05:01", datetime(2014, 1, 1, 0, 5, 1)),
    ("Имя, 31 дек 2016 в 23:59:59", datetime(2016, 12, 31, 23, 59, 59)),
    ("Имя, 8 мар 2015 в 7:00:00 (ред.)", datetime(2015, 3, 8, 7, 0, 0)),
    ("Имя, 3 июня 2016 г. в 12:30", datetime(2016, 6, 3, 12, 30)),
    ("Имя, 14 сентября 2016 в 9:05:10", datetime(2016, 9, 14, 9, 5, 10)),
    ("Name, 21 Sep 2026 at 10:46:37", datetime(2026, 9, 21, 10, 46, 37)),
    ("Name, Sep 21, 2026 at 10:46:37 pm", datetime(2026, 9, 21, 22, 46, 37)),
    ("Name, Jan 1, 2016 at 12:05:00 am", datetime(2016, 1, 1, 0, 5, 0)),
    ("Имя, 21.09.2016 в 10:46:37", datetime(2016, 9, 21, 10, 46, 37)),
])
def test_parse_russian_and_other_formats(text, expected):
    assert parse_vk_datetime(text) == expected


def test_author_with_comma_is_split_correctly():
    text = "Иванов, Иван, 5 фев 2015 в 10:00:00"
    dt, pos = find_header_date(text)
    assert dt == datetime(2015, 2, 5, 10, 0, 0)
    assert text[:pos].rstrip(", ") == "Иванов, Иван"


@pytest.mark.parametrize("text", ["", "Вы", "Имя, 32 янв 2016 в 10:00:00", "Имя, 5 xyz 2016 в 10:00"])
def test_unparseable(text):
    assert parse_vk_datetime(text) is None


START, END = date(2014, 1, 1), date(2016, 12, 31)


@pytest.mark.parametrize("dt, inside", [
    (datetime(2014, 1, 1, 0, 0, 0), True),
    (datetime(2013, 12, 31, 23, 59, 59), False),
    (datetime(2016, 12, 31, 0, 0, 0), True),
    (datetime(2016, 12, 31, 23, 59, 59), True),     # последний момент периода включается
    (datetime(2017, 1, 1, 0, 0, 0), False),
    (datetime(2015, 6, 15, 12, 0, 0), True),
    (None, False),
])
def test_in_range_inclusive(dt, inside):
    assert in_range(dt, START, END) is inside


def test_open_ended_range():
    assert in_range(datetime(2030, 1, 1), START, None)
    assert in_range(datetime(2000, 1, 1), None, END)
    assert in_range(datetime(2000, 1, 1), None, None)
