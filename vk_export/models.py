"""Модели данных и константы, общие для всех модулей."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

# Пиры с id >= 2e9 — групповые беседы, отрицательные — сообщества.
CHAT_PEER_OFFSET = 2_000_000_000


class ConversationKind(StrEnum):
    PERSONAL = "personal"
    GROUP_CHAT = "group_chat"
    COMMUNITY = "community"

    @classmethod
    def from_peer_id(cls, peer_id: int) -> "ConversationKind":
        if peer_id >= CHAT_PEER_OFFSET:
            return cls.GROUP_CHAT
        if peer_id < 0:
            return cls.COMMUNITY
        return cls.PERSONAL

    @property
    def folder(self) -> str:
        return {
            ConversationKind.PERSONAL: "Personal",
            ConversationKind.GROUP_CHAT: "Group_Chats",
            ConversationKind.COMMUNITY: "Communities",
        }[self]


class AttachmentKind(StrEnum):
    """Классификация вложения по результатам разбора HTML."""

    PHOTO = "photo"                  # прямая ссылка на изображение (userapi.com / vkuserphoto.ru)
    PHOTO_PAGE = "photo_page"        # ссылка на страницу фото в VK (без прямого URL)
    DOC_PAGE = "doc_page"            # «Файл» со ссылкой на страницу документа vk.com/docX_Y
    DOC_IMAGE = "doc_image"          # «Файл» с прямой ссылкой на файл-изображение
    VOICE = "voice"                  # «Файл» — голосовое сообщение (.ogg и т.п.)
    DOC_OTHER = "doc_other"          # «Файл» с прямой ссылкой на не-изображение
    FORWARDED = "forwarded"          # пересланные сообщения: содержимое в архив не выгружается
    WALL_POST = "wall_post"          # запись на стене: ссылки на вложения в архиве нет
    PHOTO_NO_LINK = "photo_no_link"  # «Фотография» без ссылки
    OTHER = "other"                  # видео, аудио, стикеры, ссылки и т.п. — не интересуют

    @property
    def downloadable(self) -> bool:
        return self in (AttachmentKind.PHOTO, AttachmentKind.PHOTO_PAGE,
                        AttachmentKind.DOC_PAGE, AttachmentKind.DOC_IMAGE)

    @property
    def tracked(self) -> bool:
        """Сохранять ли вложение в базе (для скачивания или для отчёта skipped)."""
        return self is not AttachmentKind.OTHER


class Status(StrEnum):
    PENDING = "pending"
    DOWNLOADED = "downloaded"
    FAILED = "failed"
    SKIPPED = "skipped"


@dataclass(slots=True)
class RawAttachment:
    description: str
    href: str | None
    link_text: str | None


@dataclass(slots=True)
class Message:
    message_id: int
    dt: datetime | None
    dt_raw: str
    author_name: str
    author_id: int | None      # id пользователя (>0) или сообщества (<0); None если неизвестен
    is_owner: bool
    edited: bool
    attachments: list[RawAttachment] = field(default_factory=list)


@dataclass(slots=True)
class Page:
    title: str | None
    messages: list[Message]


@dataclass(slots=True)
class AttachmentRecord:
    """Вложение, подготовленное к записи в базу."""

    peer_id: int
    message_id: int
    att_index: int
    msg_date: datetime
    author_name: str
    author_id: int | None
    kind: AttachmentKind
    description: str
    url: str | None
    photo_ref: str | None   # например photo100000001_456239096 или doc-200000001_437794728
    source_page: str
