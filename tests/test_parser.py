from datetime import datetime

from conftest import OWNER_ID, attachment, message, page

from vk_export.models import AttachmentKind, RawAttachment
from vk_export.parser import classify, parse_page, quick_dates

IMPG = ("https://sun9-8.userapi.com/impg/HjRredCExrqwNS/Ykbtr1Cdnqg.jpg"
        "?size=1536x2048&amp;quality=96&amp;sign=b566a9d29b&amp;type=album")
IMPG2 = "https://sun9-9.userapi.com/impg/c849032/v849032428/273bf/uPLcWWSkd3s.jpg?size=780x1040&amp;sign=62f6"
OLD = "https://sun9-39.userapi.com/c440/u118984102/125240992/x_a5daa202.jpg"
PAGE = "https://vk.ru/im?sel=100000001&amp;z=photo100000001_456239096%2Fmail709983"


def sample_page() -> bytes:
    return page("Батальон &quot;Тест&quot;", [
        message(1566113, "21 сен 2016 в 10:46:37", author="Иван", author_href="https://vk.ru/id100000002",
                text="да норм", attachments=[attachment("Фотография", IMPG), attachment("Фотография", IMPG2)]),
        message(1566112, "31 дек 2016 в 23:59:59", attachments=[attachment("Фотография", OLD)], edited=True),
        message(1566111, "30 мая 2016 в 14:57:22", author="DELETED", author_href="https://vk.ru/id100000003",
                attachments=[attachment("2 прикреплённых сообщения")]),
        message(1566110, "1 янв 2014 в 0:00:01", author="Паблик", author_href="https://vk.ru/public200000002",
                attachments=[attachment("Фотография", PAGE),
                             attachment("Файл", "https://vk.ru/doc-200000001_437794728")]),
        message(1566109, "12 мая 2015 в 23:38:04", author="Клуб", author_href="https://vk.ru/club39109721",
                attachments=[attachment("Видеозапись", "https://vk.ru/video-48512305_456283988"),
                             attachment("Запись на стене")]),
    ])


def test_parse_page_messages_and_authors():
    p = parse_page(sample_page())
    assert p.title == 'Батальон "Тест"'
    assert [m.message_id for m in p.messages] == [1566113, 1566112, 1566111, 1566110, 1566109]

    m0, m1, m2, m3, m4 = p.messages
    assert m0.dt == datetime(2016, 9, 21, 10, 46, 37)
    assert (m0.author_name, m0.author_id, m0.is_owner) == ("Иван", 100000002, False)
    assert len(m0.attachments) == 2                          # несколько фото в одном сообщении
    assert m0.attachments[0].href.startswith("https://sun9-8.userapi.com/impg/")
    assert "&quality=96&sign=" in m0.attachments[0].href     # HTML-сущности раскодированы

    assert m1.is_owner and m1.author_name == "Вы" and m1.edited
    assert m1.dt == datetime(2016, 12, 31, 23, 59, 59)
    assert (m2.author_name, m2.author_id) == ("DELETED", 100000003)  # удалённый пользователь
    assert m3.author_id == -200000002                           # сообщество → отрицательный id
    assert m4.author_id == -39109721


def test_classify_attachments():
    p = parse_page(sample_page())
    kinds = [[classify(a)[0] for a in m.attachments] for m in p.messages]
    assert kinds == [
        [AttachmentKind.PHOTO, AttachmentKind.PHOTO],
        [AttachmentKind.PHOTO],
        [AttachmentKind.FORWARDED],
        [AttachmentKind.PHOTO_PAGE, AttachmentKind.DOC_PAGE],
        [AttachmentKind.OTHER, AttachmentKind.WALL_POST],
    ]
    kind, url, ref = classify(p.messages[3].attachments[0])
    assert ref == "photo100000001_456239096"
    assert classify(p.messages[3].attachments[1])[2] == "doc-200000001_437794728"


def test_classify_files():
    voice = RawAttachment("Файл", "https://psv4.userapi.com/s/v1/amsg/8Ho5FBb7.ogg", "https://vk.ru/doc100000001_438992118")
    assert classify(voice)[0] is AttachmentKind.VOICE
    assert classify(voice)[2] == "doc100000001_438992118"
    img = RawAttachment("Файл", "https://psv4.vkuserphoto.ru/s/v1/d/abc/photo.PNG", "https://vk.ru/doc1_2")
    assert classify(img)[0] is AttachmentKind.DOC_IMAGE
    pdf = RawAttachment("Файл", "https://psv4.vkuserphoto.ru/s/v1/d/abc/book.pdf", None)
    assert classify(pdf)[0] is AttachmentKind.DOC_OTHER
    assert classify(RawAttachment("Фотография", None, None))[0] is AttachmentKind.PHOTO_NO_LINK
    # Архив на другом языке: описание неизвестно, но ссылка — фото VK.
    assert classify(RawAttachment("Photo", OLD, OLD))[0] is AttachmentKind.PHOTO
    assert classify(RawAttachment("Something", OLD, OLD))[0] is AttachmentKind.PHOTO
    assert classify(RawAttachment("1 прикреплённое сообщение", None, None))[0] is AttachmentKind.FORWARDED
    assert classify(RawAttachment("17 прикреплённых сообщений", None, None))[0] is AttachmentKind.FORWARDED


def test_quick_dates_matches_full_parse():
    raw = sample_page()
    count, dates = quick_dates(raw)
    assert count == 5
    assert dates == [m.dt for m in parse_page(raw).messages]


def test_utf8_page_is_supported():
    raw = sample_page().decode("cp1251").replace('charset="windows-1251"', 'charset="utf-8"').encode("utf-8")
    p = parse_page(raw)
    assert p.messages[0].author_name == "Иван"
    assert p.messages[0].dt == datetime(2016, 9, 21, 10, 46, 37)


def test_malformed_messages_are_tolerated():
    raw = page("X", [
        '<div class="item"><div class="message" data-id="abc"><div class="message__header">Вы, 1 янв 2015 в 1:00:00</div></div></div>',
        '<div class="item"><div class="message" data-id="5"><div class="message__header">Вы, когда-то</div></div></div>',
        message(6, "2 янв 2015 в 1:00:00", attachments=[attachment("Фотография", OLD)]),
    ])
    msgs = parse_page(raw).messages
    assert [m.message_id for m in msgs] == [5, 6]
    assert msgs[0].dt is None and msgs[1].dt == datetime(2015, 1, 2, 1, 0, 0)


def test_owner_id_from_meta(tmp_path):
    from conftest import make_archive
    from vk_export.archive_reader import ArchiveReader
    z = make_archive(tmp_path / "a.zip", {"messages/42/messages0.html": sample_page()})
    with ArchiveReader(z) as r:
        pages = r.message_pages()
        assert [(p.peer_id, p.offset) for p in pages] == [(42, 0)]
        assert r.owner_id() == OWNER_ID
