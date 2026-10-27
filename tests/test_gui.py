"""Проверка графического окна: заполнение полей, скачивание, остановка."""

from __future__ import annotations

import time

import pytest

tk = pytest.importorskip("tkinter")

from test_downloader import build_archive, server  # noqa: F401 — фикстура сервера

import gui


@pytest.fixture
def app():
    try:
        root = tk.Tk()
    except tk.TclError:
        pytest.skip("нет графической среды")
    root.withdraw()
    a = gui.App(root)
    yield a
    try:
        root.destroy()
    except tk.TclError:
        pass


def pump(app, timeout=30.0):
    """Крутит цикл событий окна, пока фоновая работа не закончится."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.root.update()
        if app.worker and not app.worker.is_alive() and app.events.empty():
            app.root.update()
            return
        time.sleep(0.02)
    raise AssertionError("окно не дождалось завершения работы")


def log_text(app) -> str:
    return app.log.get("1.0", "end")


def test_download_from_gui(app, server, tmp_path):  # noqa: F811
    archive = build_archive(tmp_path, server.base)
    app.set_archive(archive)
    assert app.output_var.get() == str(tmp_path / "VK_Photos")   # по умолчанию — рядом с архивом
    app.year_from.set("2014")
    app.year_to.set("2016")

    app.start(scan_only=True)
    pump(app)
    assert "Найдено фото и изображений за период: 6" in app.result_var.get()
    assert not (tmp_path / "VK_Photos").exists()                 # анализ ничего не создаёт

    app.start(scan_only=False)
    assert str(app.download_btn["state"]) == "disabled"
    pump(app)
    assert app.result_var.get().startswith("Скачано: 5 из 6")
    assert "Готово" in app.status_var.get()
    assert len(list((tmp_path / "VK_Photos").rglob("*.jpg"))) == 5
    assert str(app.download_btn["state"]) == "normal"
    assert "Итого" in log_text(app)


def test_year_validation_and_missing_archive(app, monkeypatch):
    shown = []
    monkeypatch.setattr(gui.messagebox, "showwarning", lambda *a: shown.append(a[1]))
    monkeypatch.setattr(gui.messagebox, "showerror", lambda *a: shown.append(a[1]))
    app.archive_var.set("")
    app.start(scan_only=False)
    app.archive_var.set("Z:/нет/такого/Archive.zip")
    app.start(scan_only=False)
    assert app.worker is None and len(shown) == 2


def test_stop_keeps_progress(app, server, tmp_path):  # noqa: F811
    archive = build_archive(tmp_path, server.base)
    app.set_archive(archive)
    app.year_from.set("2014")
    app.year_to.set("2016")
    app.stop_event.set()            # «Стоп» нажат сразу: анализ прерывается до скачивания
    app.start(scan_only=False)
    app.stop()
    pump(app)
    assert "Остановлено" in app.status_var.get()
    assert str(app.download_btn["state"]) == "normal"

    app.start(scan_only=False)       # повторный запуск продолжает и доводит до конца
    pump(app)
    assert app.result_var.get().startswith("Скачано: 5 из 6")
