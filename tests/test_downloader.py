"""Тесты загрузчика и сквозной тест CLI против локального HTTP-сервера."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import json
import threading
from collections import Counter
from datetime import date, datetime
from pathlib import Path

import pytest
from aiohttp import web

from conftest import attachment, make_archive, message, page

import main as cli
from vk_export.downloader import Downloader, sniff_image
from vk_export.models import AttachmentKind, AttachmentRecord, Status
from vk_export.storage import Store, safe_name


def jpeg(tag: str) -> bytes:
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00" + tag.encode() * 50 + b"\xff\xd9"


PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class FakeVK:
    """Локальный сервер, имитирующий ответы userapi.com и vk.com."""

    def __init__(self):
        self.hits: Counter = Counter()
        self.base = ""
        app = web.Application()
        app.router.add_get("/img/{name}", self.img)
        app.router.add_get("/same/{name}", self.same)
        app.router.add_get("/s/{code}", self.status)
        app.router.add_get("/flaky", self.flaky)
        app.router.add_get("/soft404", self.soft404)
        app.router.add_get("/ratelimit", self.ratelimit)
        app.router.add_get("/html.jpg", self.html)
        app.router.add_get("/fake.jpg", self.fake)
        app.router.add_get("/login", self.login)
        app.router.add_get("/redirect", self.redirect)
        app.router.add_get("/doc/{kind}", self.doc)
        app.router.add_get("/doc.png", self.docfile)
        self.app = app

    def start(self):
        ready = threading.Event()

        def run():
            self.loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self.loop)
            self.runner = web.AppRunner(self.app)
            self.loop.run_until_complete(self.runner.setup())
            site = web.TCPSite(self.runner, "127.0.0.1", 0)
            self.loop.run_until_complete(site.start())
            port = site._server.sockets[0].getsockname()[1]
            self.base = f"http://127.0.0.1:{port}"
            ready.set()
            self.loop.run_forever()

        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        ready.wait(10)

    def stop(self):
        asyncio.run_coroutine_threadsafe(self.runner.cleanup(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    async def img(self, request):
        self.hits[request.path] += 1
        return web.Response(body=jpeg(request.match_info["name"]), content_type="image/jpeg")

    async def same(self, request):
        self.hits[request.path] += 1
        return web.Response(body=jpeg("same"), content_type="image/jpeg")

    async def status(self, request):
        code = int(request.match_info["code"])
        self.hits[request.path] += 1
        return web.Response(status=code, text="<html>error</html>", content_type="text/html")

    async def flaky(self, request):
        self.hits[request.path] += 1
        if self.hits[request.path] <= 2:
            return web.Response(status=503, text="busy")
        return web.Response(body=jpeg("flaky"), content_type="image/jpeg")

    async def soft404(self, request):
        # Так ведёт себя CDN VK: ложный 404, затем фото.
        self.hits[request.path] += 1
        if self.hits[request.path] <= 2:
            return web.Response(status=404, text="<html>404</html>", content_type="text/html")
        return web.Response(body=jpeg("soft"), content_type="image/jpeg")

    async def ratelimit(self, request):
        self.hits[request.path] += 1
        if self.hits[request.path] == 1:
            return web.Response(status=429, headers={"Retry-After": "0"})
        return web.Response(body=jpeg("rl"), content_type="image/jpeg")

    async def html(self, request):
        return web.Response(text="<html>login</html>", content_type="text/html")

    async def fake(self, request):
        # Заголовок говорит «картинка», а внутри HTML.
        return web.Response(body=b"<!DOCTYPE html><html><body>nope" + b" " * 100, content_type="image/jpeg")

    async def login(self, request):
        raise web.HTTPFound("https://login.vk.com/?to=abc")

    async def redirect(self, request):
        raise web.HTTPFound("/img/redirected")

    async def doc(self, request):
        kind = request.match_info["kind"]
        if kind == "public":
            body = ('<html><title>file.png</title><script>Docs.initDoc({"docOwnerId":-1,"docExt":"png",'
                    f'"docUrl":"{self.base}/doc.png","docTitle":"file.png"}});</script></html>')
        elif kind == "pdf":
            body = ('<html><script>Docs.initDoc({"docExt":"pdf",'
                    f'"docUrl":"{self.base}/doc.png"}});</script></html>')
        else:
            body = ('<html><title>Ошибка | ВКонтакте</title><div class="message_page_body">'
                    'Файл недоступен или удалён</div></html>')
        return web.Response(body=body.encode("cp1251"), content_type="text/html", charset="windows-1251")

    async def docfile(self, request):
        return web.Response(body=PNG, content_type="image/png")


@pytest.fixture(scope="module")
def server():
    s = FakeVK()
    s.start()
    yield s
    s.stop()


def add(store: Store, peer: int, msg: int, url: str, kind=AttachmentKind.PHOTO, idx: int = 0,
        dt=datetime(2016, 5, 1, 12, 0, 0)):
    store.upsert_conversation(peer, f"Чат {peer}")
    store.add_attachments([AttachmentRecord(peer, msg, idx, dt, "Автор", 1, kind, "Фотография",
                                            url, None, "messages/x.html")])
    store.commit()


def run(store, out, **kw):
    kw.setdefault("soft_retries", 0)
    d = Downloader(store, out, concurrency=3, max_retries=3, backoff_base=0.01, soft_delay=0.01,
                   show_progress=False, **kw)
    return asyncio.run(d.run(store.jobs(None, None, False)))


def statuses(store):
    return {r["message_id"]: (r["status"], r["reason"]) for r in store.db.execute("SELECT * FROM attachments")}


def test_sniff_image():
    assert sniff_image(jpeg("x")[:32]) == ("jpg", "image/jpeg")
    assert sniff_image(PNG[:32]) == ("png", "image/png")
    assert sniff_image(b"GIF89a" + b"\x00" * 26)[0] == "gif"
    assert sniff_image(b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 16)[0] == "webp"
    assert sniff_image(b"<!DOCTYPE html><html>" + b" " * 16) is None


def test_http_outcomes(server, tmp_path):
    store = Store(tmp_path / "p.db")
    b = server.base
    add(store, 1, 1, f"{b}/img/a.jpg")
    add(store, 1, 2, f"{b}/s/404")
    add(store, 1, 3, f"{b}/s/403")
    add(store, 1, 4, f"{b}/flaky")
    add(store, 1, 5, f"{b}/ratelimit")
    add(store, 1, 6, f"{b}/html.jpg")
    add(store, 1, 7, f"{b}/fake.jpg")
    add(store, 1, 8, f"{b}/s/500")
    add(store, 1, 9, f"{b}/login", kind=AttachmentKind.PHOTO_PAGE)
    add(store, 1, 10, f"{b}/redirect")
    add(store, 1, 11, f"{b}/doc/public", kind=AttachmentKind.DOC_PAGE)
    add(store, 1, 12, f"{b}/doc/private", kind=AttachmentKind.DOC_PAGE)
    add(store, 1, 13, f"{b}/doc/pdf", kind=AttachmentKind.DOC_PAGE)
    add(store, 1, 14, f"{b}/html.jpg", kind=AttachmentKind.DOC_IMAGE)

    run(store, tmp_path)
    st = statuses(store)
    assert st[1] == ("downloaded", None)
    assert st[2] == ("failed", "not_found")
    assert st[3] == ("failed", "forbidden")
    assert st[4] == ("downloaded", None) and server.hits["/flaky"] == 3       # 2×503, затем успех
    assert st[5] == ("downloaded", None) and server.hits["/ratelimit"] == 2   # 429 → повтор
    assert st[6][0] == "failed" and st[6][1].startswith("not_image_response:text/html")
    assert st[7][0] == "failed" and st[7][1].startswith("not_image_response")
    assert st[8] == ("failed", "http_500") and server.hits["/s/500"] == 4     # 1 + 3 повтора
    assert st[9] == ("failed", "requires_auth")
    assert st[10] == ("downloaded", None)
    assert st[11] == ("downloaded", None)
    assert st[12] == ("failed", "doc_unavailable_or_private")
    assert st[13] == ("skipped", "not_image_doc:pdf")
    assert st[14][0] == "skipped"                                              # «Файл», но не картинка

    retryable = {r["message_id"]: r["retryable"] for r in store.db.execute("SELECT * FROM attachments")}
    assert retryable[8] == 1 and retryable[2] == 0

    doc = store.db.execute("SELECT * FROM attachments WHERE message_id = 11").fetchone()
    assert doc["resolved_url"].endswith("/doc.png") and doc["file_path"].endswith(".png")

    # Каждый сохранённый файл — настоящее изображение с верным SHA-256.
    for r in store.db.execute("SELECT * FROM attachments WHERE status = 'downloaded'"):
        data = (tmp_path / r["file_path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == r["sha256"] and len(data) == r["file_size"]
        assert sniff_image(data[:32]) is not None
    assert not list(tmp_path.rglob("*.part")) and not (tmp_path / ".tmp").exists()


def test_duplicates_share_file_and_keep_all_records(server, tmp_path):
    store = Store(tmp_path / "p.db")
    b = server.base
    # Одна и та же ссылка в двух сообщениях одной переписки, и в другой переписке.
    add(store, 10, 1, f"{b}/img/dup.jpg")
    add(store, 10, 2, f"{b}/img/dup.jpg")
    add(store, 20, 3, f"{b}/img/dup.jpg")
    # Разные ссылки, одинаковое содержимое.
    add(store, 10, 4, f"{b}/same/x1.jpg")
    add(store, 10, 5, f"{b}/same/x2.jpg")
    run(store, tmp_path)

    rows = {r["message_id"]: r for r in store.db.execute("SELECT * FROM attachments")}
    assert all(r["status"] == "downloaded" for r in rows.values())
    assert server.hits["/img/dup.jpg"] == 1                                   # одна загрузка на URL
    assert rows[1]["file_path"] == rows[2]["file_path"] and rows[2]["is_duplicate"] == 1
    assert rows[3]["file_path"] != rows[1]["file_path"]                       # своя папка переписки
    assert rows[3]["sha256"] == rows[1]["sha256"]
    assert rows[4]["file_path"] == rows[5]["file_path"]
    files = [p for p in tmp_path.rglob("*.jpg")]
    assert len(files) == 3


def test_resume_does_not_redownload_and_requeues_missing(server, tmp_path):
    store = Store(tmp_path / "p.db")
    add(store, 1, 1, f"{server.base}/img/r1.jpg")
    add(store, 1, 2, f"{server.base}/img/r2.jpg")
    run(store, tmp_path)
    assert server.hits["/img/r1.jpg"] == 1

    stats = run(store, tmp_path)
    assert stats.groups == 0 and server.hits["/img/r1.jpg"] == 1

    path = store.db.execute("SELECT file_path FROM attachments WHERE message_id = 2").fetchone()[0]
    (tmp_path / path).unlink()
    assert store.reset_stale(tmp_path) == 1
    run(store, tmp_path)
    assert server.hits["/img/r2.jpg"] == 2 and (tmp_path / path).is_file()


def test_soft_404_is_retried(server, tmp_path):
    store = Store(tmp_path / "p.db")
    add(store, 1, 1, f"{server.base}/soft404")
    add(store, 1, 2, f"{server.base}/s/410")
    run(store, tmp_path, soft_retries=3)
    st = statuses(store)
    assert st[1] == ("downloaded", None) and server.hits["/soft404"] == 3
    assert st[2] == ("failed", "gone") and server.hits["/s/410"] == 4       # 1 + 3 мягких повтора


def test_failed_never_overwrites_downloaded(tmp_path):
    store = Store(tmp_path / "p.db")
    add(store, 1, 1, "http://x/a.jpg")
    att_id = store.db.execute("SELECT id FROM attachments").fetchone()[0]
    store.mark_downloaded(att_id, path="a.jpg", size=1, sha256="0", content_type="image/jpeg",
                          resolved_url=None, http_status=200, is_duplicate=False)
    store.mark_result(att_id, Status.FAILED, "not_found", http_status=404)
    assert statuses(store)[1] == ("downloaded", None)


def test_existing_file_is_adopted_when_server_fails(server, tmp_path):
    store = Store(tmp_path / "p.db")
    add(store, 1, 1, f"{server.base}/s/404", dt=datetime(2016, 5, 1, 12, 0, 0))
    folder = tmp_path / "Personal" / "Чат_1_1" / "2016"
    folder.mkdir(parents=True)
    (folder / "20160501_120000_1_0.jpg").write_bytes(jpeg("left-from-crash"))
    run(store, tmp_path)
    row = store.db.execute("SELECT * FROM attachments").fetchone()
    assert row["status"] == "downloaded" and row["file_path"] == "Personal/Чат_1_1/2016/20160501_120000_1_0.jpg"
    assert row["sha256"] == hashlib.sha256(jpeg("left-from-crash")).hexdigest()


def test_redownload_of_own_file_is_not_marked_duplicate(server, tmp_path):
    store = Store(tmp_path / "p.db")
    add(store, 1, 1, f"{server.base}/img/own.jpg")
    run(store, tmp_path)
    # Имитация повторной обработки той же записи (например, параллельный/прерванный запуск).
    store.db.execute("UPDATE attachments SET status='pending'")
    store.commit()
    run(store, tmp_path)
    row = store.db.execute("SELECT * FROM attachments").fetchone()
    assert row["status"] == "downloaded" and row["is_duplicate"] == 0
    assert len(list(tmp_path.rglob("*.jpg"))) == 1


def test_output_lock_prevents_second_run(tmp_path):
    from vk_export.lock import OutputLock, OutputLocked
    with OutputLock(tmp_path):
        with pytest.raises(OutputLocked):
            OutputLock(tmp_path).acquire()
    with OutputLock(tmp_path):      # после освобождения снова доступна
        pass


def test_safe_name():
    assert safe_name('Батальон "Тест" <>|?*') == "Батальон_Тест"
    assert safe_name("CON") == "_CON"
    assert safe_name("   ...  ") == "untitled"
    assert safe_name(None) == "untitled"
    assert len(safe_name("я" * 500)) <= 60
    assert safe_name("a/b\\c:d") == "a_b_c_d"


# --- сквозной тест: архив → CLI → файлы и отчёты --------------------------------

def build_archive(tmp_path: Path, base: str) -> Path:
    pages = {
        "messages/2000000053/messages0.html": page("Домашнее задание", [
            message(10, "1 янв 2017 в 0:00:00", author="А", author_href="https://vk.ru/id5",
                    attachments=[attachment("Фотография", f"{base}/img/out2017.jpg")]),
            message(9, "31 дек 2016 в 23:59:59", author="А", author_href="https://vk.ru/id5",
                    attachments=[attachment("Фотография", f"{base}/img/e1.jpg"),
                                 attachment("Фотография", f"{base}/img/e2.jpg")]),
            message(8, "5 мар 2015 в 10:00:00", attachments=[attachment("Фотография", f"{base}/img/e1.jpg")]),
            message(7, "2 фев 2015 в 9:00:00", author="Б", author_href="https://vk.ru/id6",
                    attachments=[attachment("3 прикреплённых сообщения")]),
        ]),
        "messages/2000000053/messages50.html": page("Домашнее задание", [
            message(6, "1 янв 2014 в 0:00:00", author="Б", author_href="https://vk.ru/id6",
                    attachments=[attachment("Фотография", f"{base}/s/404")]),
            message(5, "31 дек 2013 в 23:59:59", author="Б", author_href="https://vk.ru/id6",
                    attachments=[attachment("Фотография", f"{base}/img/old2013.jpg")]),
        ]),
        "messages/100000004/messages0.html": page("DELETED", [
            message(4, "14 июн 2016 в 18:00:00", author="DELETED", author_href="https://vk.ru/id100000004",
                    attachments=[attachment("Фотография", f"{base}/img/p1.jpg")]),
        ]),
        "messages/100000003/messages0.html": page("DELETED", [
            message(3, "15 июн 2016 в 18:00:00", author="DELETED", author_href="https://vk.ru/id100000003",
                    attachments=[attachment("Фотография", f"{base}/img/p2.jpg")]),
        ]),
    }
    return make_archive(tmp_path / "Archive.zip", pages)


def read_csv(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def test_cli_end_to_end_and_rerun(server, tmp_path, capsys):
    archive = build_archive(tmp_path, server.base)
    out = tmp_path / "VK_Photos"
    args = ["--archive", str(archive), "--output", str(out), "--start-date", "2014-01-01",
            "--end-date", "2016-12-31", "--no-progress", "--concurrency", "2", "--retries", "1",
            "--soft-retries", "0"]

    assert cli.main(args[:4] + args[4:8] + ["--scan-only", "--no-progress"]) == 0
    assert "Итого" in capsys.readouterr().out

    hits_404 = server.hits["/s/404"]
    assert cli.main(args) == 0
    summary = json.loads((out / "reports" / "summary.json").read_text(encoding="utf-8"))
    assert summary["photo_attachments_total"] == 6
    assert summary["photo_attachments_by_year"]["2014"]["group_chat"] == 1
    assert summary["photo_attachments_by_year"]["2016"] == {
        "personal": 2, "group_chat": 2, "community": 0, "total": 4}
    assert summary["results"]["downloaded_records"] == 5
    assert summary["results"]["failed_by_reason"] == {"not_found": 1}
    assert server.hits["/img/out2017.jpg"] == 0 and server.hits["/img/old2013.jpg"] == 0

    files = sorted(p.relative_to(out).as_posix() for p in out.rglob("*.jpg"))
    assert files == [
        "Group_Chats/Домашнее_задание_2000000053/2015/20150305_100000_8_0.jpg",
        "Group_Chats/Домашнее_задание_2000000053/2016/20161231_235959_9_0.jpg",
        "Group_Chats/Домашнее_задание_2000000053/2016/20161231_235959_9_1.jpg",
        "Personal/DELETED_100000003/2016/20160615_180000_3_0.jpg",
        "Personal/DELETED_100000004/2016/20160614_180000_4_0.jpg",
    ]
    downloaded = read_csv(out / "reports" / "downloaded.csv")
    assert len(downloaded) == 5 and {r["author_name"] for r in downloaded} >= {"Вы", "А", "DELETED"}
    assert len(read_csv(out / "reports" / "failed.csv")) == 1
    assert [r["kind"] for r in read_csv(out / "reports" / "skipped.csv")] == ["forwarded"]

    hits_before = sum(server.hits.values())
    assert cli.main(args) == 0                                  # повторный запуск
    assert sum(server.hits.values()) == hits_before             # ничего не скачано заново
    assert sorted(p.relative_to(out).as_posix() for p in out.rglob("*.jpg")) == files
    assert len(read_csv(out / "reports" / "downloaded.csv")) == 5

    assert cli.main(args + ["--retry-failed"]) == 0             # повтор окончательно недоступных
    assert server.hits["/s/404"] == hits_404 + 2               # 404 не повторяется без флага
