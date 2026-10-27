"""Разбор HTML-страниц переписок из архива VK.

Структура страницы (проверено на реальном архиве):

    <div class="message" data-id="1563404">
      <div class="message__header"><a href="https://vk.ru/id100000001">Имя</a>, 8 сен 2025 в 19:43:56</div>
      <div>текст<div class="kludges">
        <div class="attachment">
          <div class="attachment__description">Фотография</div>
          <a class='attachment__link' href='https://sun9-8.userapi.com/impg/...'>...</a>
        </div>
      </div></div>
    </div>

Сообщения владельца архива подписаны «Вы» без ссылки. Пересланные сообщения
представлены только строкой «N прикреплённых сообщений» — их содержимого в архиве нет.
"""

from __future__ import annotations

import html as html_lib
import re
from urllib.parse import parse_qs, unquote, urlsplit

from lxml import html as lxml_html

from .dates import find_header_date
from .models import AttachmentKind, Message, Page, RawAttachment

_CHARSET_RE = re.compile(rb'<meta[^>]+charset=["\']?([\w-]+)', re.IGNORECASE)
_HEADER_RE = re.compile(r'class="message__header"[^>]*>(.*?)</div>', re.DOTALL)
_MESSAGE_RE = re.compile(r'class="message"')
_TAG_RE = re.compile(r"<[^>]+>")

_OWNER_LABELS = {"вы", "you"}
_PHOTO_DESCS = {"фотография", "фото", "photo", "photograph"}
_FILE_DESCS = {"файл", "документ", "file", "document", "doc"}
_WALL_DESCS = {"запись на стене", "wall post", "post"}
_FORWARDED_RE = re.compile(r"прикрепл[её]нн|пересланн|forwarded|attached message", re.IGNORECASE)

IMAGE_EXTS = {"jpg", "jpeg", "png", "gif", "webp", "bmp", "tif", "tiff", "heic", "heif", "avif"}
_AUDIO_EXTS = {"ogg", "opus", "mp3", "m4a", "wav", "oga", "aac", "webm"}
_VK_IMAGE_HOSTS = ("userapi.com", "vkuserphoto.ru", "vk.me", "vk-cdn.net", "vkuseraudio.net")
_VK_SITE_RE = re.compile(r"(^|\.)(vk\.com|vk\.ru|vkontakte\.ru|m\.vk\.com|m\.vk\.ru)$")
_PHOTO_REF_RE = re.compile(r"photo(-?\d+_\d+)")
_DOC_REF_RE = re.compile(r"doc(-?\d+_\d+)")
_AUTHOR_ID_RE = re.compile(r"/(id|public|club|event)(\d+)/?$")


def detect_encoding(raw: bytes) -> str:
    m = _CHARSET_RE.search(raw[:2048])
    if m:
        enc = m.group(1).decode("ascii", "ignore").lower()
        try:
            "".encode(enc)
            return enc
        except LookupError:
            pass
    try:
        raw.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1251"


def quick_dates(raw: bytes, encoding: str | None = None) -> tuple[int, list]:
    """Быстро (без построения DOM) извлекает даты всех сообщений страницы.

    Возвращает (число сообщений, список дат); дата = None, если не распознана.
    Используется как предфильтр: страницы, где нет сообщений из нужного периода,
    полностью не разбираются.
    """
    text = raw.decode(encoding or detect_encoding(raw), errors="replace")
    count = len(_MESSAGE_RE.findall(text))
    dates = []
    for header in _HEADER_RE.findall(text):
        plain = html_lib.unescape(_TAG_RE.sub("", header))
        found = find_header_date(plain)
        dates.append(found[0] if found else None)
    return count, dates


def _author_id(href: str | None) -> int | None:
    if not href:
        return None
    m = _AUTHOR_ID_RE.search(href)
    if not m:
        return None
    value = int(m.group(2))
    return value if m.group(1) == "id" else -value


def _norm_space(s: str) -> str:
    return " ".join(s.split())


def parse_page(raw: bytes) -> Page:
    encoding = detect_encoding(raw)
    doc = lxml_html.fromstring(raw, parser=lxml_html.HTMLParser(encoding=encoding))

    crumbs = doc.xpath(
        '//*[contains(concat(" ", normalize-space(@class), " "), " ui_crumb ")]')
    title = _norm_space(crumbs[-1].text_content()) if crumbs else None

    messages: list[Message] = []
    for node in doc.xpath('//div[contains(concat(" ", normalize-space(@class), " "), " message ")]'):
        msg_id_raw = node.get("data-id")
        try:
            msg_id = int(msg_id_raw)
        except (TypeError, ValueError):
            continue
        headers = node.xpath('./div[contains(@class, "message__header")]')
        header = headers[0] if headers else None
        header_text = _norm_space(header.text_content()) if header is not None else ""
        found = find_header_date(header_text)
        dt, author_part = (found[0], header_text[:found[1]]) if found else (None, header_text)
        author_part = author_part.rstrip().rstrip(",").strip()

        link = header.xpath("./a") if header is not None else []
        author_href = link[0].get("href") if link else None
        author_name = _norm_space(link[0].text_content()) if link else author_part
        is_owner = not link and author_name.lower() in _OWNER_LABELS

        atts = []
        for att in node.xpath('.//div[contains(concat(" ", normalize-space(@class), " "), " attachment ")]'):
            desc_nodes = att.xpath('./div[contains(@class, "attachment__description")]')
            desc = _norm_space(desc_nodes[0].text_content()) if desc_nodes else ""
            links = att.xpath("./a[@href]") or att.xpath(".//a[@href]")
            if links:
                for a in links:
                    atts.append(RawAttachment(desc, a.get("href").strip(), _norm_space(a.text_content())))
            else:
                atts.append(RawAttachment(desc, None, None))

        messages.append(Message(
            message_id=msg_id,
            dt=dt,
            dt_raw=header_text,
            author_name=author_name or "?",
            author_id=_author_id(author_href),
            is_owner=is_owner,
            edited=bool(header is not None and header.xpath('.//*[contains(@class, "message-edited")]')),
            attachments=atts,
        ))
    return Page(title=title, messages=messages)


def _ext(path: str) -> str:
    name = unquote(path).rsplit("/", 1)[-1]
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


def _is_vk_site(host: str) -> bool:
    return bool(_VK_SITE_RE.search(host))


def _is_vk_cdn(host: str) -> bool:
    return any(host == h or host.endswith("." + h) for h in _VK_IMAGE_HOSTS)


def _photo_ref(url: str) -> str | None:
    parts = urlsplit(url)
    for value in [parts.path] + [v for vs in parse_qs(parts.query).values() for v in vs]:
        m = _PHOTO_REF_RE.search(unquote(value))
        if m:
            return "photo" + m.group(1)
    return None


def classify(att: RawAttachment) -> tuple[AttachmentKind, str | None, str | None]:
    """Определяет тип вложения. Возвращает (тип, URL для скачивания, ссылка VK вида photoX_Y/docX_Y)."""
    desc = att.description.lower()
    href = att.href
    is_photo = desc in _PHOTO_DESCS
    is_file = desc in _FILE_DESCS

    if _FORWARDED_RE.search(desc):
        return AttachmentKind.FORWARDED, None, None
    if desc in _WALL_DESCS:
        return AttachmentKind.WALL_POST, href, None

    if not href or not href.lower().startswith(("http://", "https://", "//")):
        return (AttachmentKind.PHOTO_NO_LINK if is_photo else AttachmentKind.OTHER), None, None
    if href.startswith("//"):
        href = "https:" + href

    parts = urlsplit(href)
    host = parts.netloc.lower().split(":")[0]
    ext = _ext(parts.path)

    if is_photo:
        if _is_vk_site(host):
            return AttachmentKind.PHOTO_PAGE, href, _photo_ref(href)
        return AttachmentKind.PHOTO, href, _photo_ref(att.link_text or "")

    if is_file:
        doc_ref = None
        for candidate in (att.link_text or "", href):
            m = _DOC_REF_RE.search(candidate)
            if m:
                doc_ref = "doc" + m.group(1)
                break
        if _is_vk_site(host):
            return AttachmentKind.DOC_PAGE, href, doc_ref
        if ext in IMAGE_EXTS:
            return AttachmentKind.DOC_IMAGE, href, doc_ref
        if ext in _AUDIO_EXTS or "/amsg/" in parts.path:
            return AttachmentKind.VOICE, href, doc_ref
        if ext:
            return AttachmentKind.DOC_OTHER, href, doc_ref
        # Прямая ссылка без расширения — тип выяснится по содержимому ответа.
        return AttachmentKind.DOC_IMAGE, href, doc_ref

    # Неизвестное описание (например, архив на другом языке), но ссылка явно на картинку VK.
    if _is_vk_cdn(host) and (ext in IMAGE_EXTS or "/impg/" in parts.path):
        return AttachmentKind.PHOTO, href, None
    return AttachmentKind.OTHER, None, None
