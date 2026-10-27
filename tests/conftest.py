"""Общие помощники для тестов: генерация страниц в формате архива VK."""

from __future__ import annotations

import base64
import json
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

OWNER_ID = 100000000


def _jd() -> str:
    raw = json.dumps({"user_id": OWNER_ID, "time_current": 1793015238}, separators=(",", ":"))
    return base64.b64encode(raw.encode()).decode().rstrip("=")


def attachment(desc: str, href: str | None = None, text: str | None = None) -> str:
    link = f"\n  <a class='attachment__link' href='{href}'>{text or href}</a>" if href else "\n  "
    return f'<div class="attachment">\n  <div class="attachment__description">{desc}</div>{link}\n</div>'


def message(msg_id: int, date: str, *, author: str | None = None, author_href: str | None = None,
            text: str = "", attachments: list[str] = (), edited: bool = False) -> str:
    who = f'<a href="{author_href}">{author}</a>' if author_href else (author or "Вы")
    ed = ' <span class="message-edited">(ред.)</span>' if edited else ""
    return (f'<div class="item">\n  <div class=\'item__main\'><div class="message" data-id="{msg_id}">\n'
            f'  <div class="message__header">{who}, {date}{ed}</div>\n'
            f'  <div>{text}<div class="kludges">{"".join(attachments)}</div></div>\n</div></div>\n</div>')


def page(title: str, messages: list[str]) -> bytes:
    html = f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="windows-1251">
  <meta name="jd" content="{_jd()}">
  <title>VK</title>
</head>
<body>
  <div class="wrap">
    <div class="page_content page_block">
      <h2 class="page_block_h2">
<div class="page_block_header clear_fix">
  <div class="page_block_header_inner _header_inner" data-testid="header"><a class="ui_crumb" href="../../index.html">Профиль</a><div class="ui_crumb_sep"></div><a class="ui_crumb" href="../index-messages.html">Сообщения</a><div class="ui_crumb_sep"></div><div class="ui_crumb" >{title}</div></div>
</div>
</h2>
      <div class="wrap_page_content">{"".join(messages)}</div>
    </div>
  </div>
</body>
</html>"""
    return html.encode("cp1251")


def make_archive(path: Path, pages: dict[str, bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("messages/index-messages.html", "<html></html>")
        for name, data in pages.items():
            z.writestr(name, data)
    return path
