"""Графическое окно для выгрузки фотографий из архива VK.

Запуск: python gui.py (или собранный VKPhotoExporter.exe).
"""

from __future__ import annotations

import os
import queue
import subprocess
import sys
import threading
import traceback
import tkinter as tk
from datetime import date
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from vk_export import __version__
from vk_export.runner import Cancelled, ExportError, ExportOptions, run_export

FIRST_YEAR = 2006  # ВКонтакте запущен в 2006 году
APP_TITLE = "VK Photo Exporter"
STAGE_LABELS = {"scan": "Анализ архива", "download": "Скачивание фото"}


def _enable_high_dpi() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _open_path(path: Path) -> None:
    if sys.platform == "win32":
        os.startfile(path)  # noqa: S606 — открываем папку в Проводнике
    elif sys.platform == "darwin":
        subprocess.run(["open", str(path)], check=False)
    else:
        subprocess.run(["xdg-open", str(path)], check=False)


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.events: queue.Queue = queue.Queue()
        self.worker: threading.Thread | None = None
        self.stop_event = threading.Event()
        self.output_touched = False
        self.closing = False

        root.title(f"{APP_TITLE} {__version__}")
        root.minsize(640, 560)
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Accent.TButton", font=("Segoe UI", 10, "bold"))
        style.configure("Result.TLabel", font=("Segoe UI", 11, "bold"))

        years = [str(y) for y in range(FIRST_YEAR, date.today().year + 1)]
        self.archive_var = tk.StringVar()
        self.output_var = tk.StringVar()
        self.year_from = tk.StringVar(value=years[0])
        self.year_to = tk.StringVar(value=years[-1])
        self.retry_var = tk.BooleanVar(value=False)
        self.status_var = tk.StringVar(value="Выберите архив VK (Archive.zip), период и нажмите «Скачать фото».")
        self.result_var = tk.StringVar()

        pad = {"padx": 12, "pady": (10, 0)}
        main = ttk.Frame(root, padding=(4, 4, 4, 12))
        main.pack(fill="both", expand=True)

        # 1. Архив
        box = ttk.LabelFrame(main, text=" 1. Архив ВКонтакте ", padding=10)
        box.pack(fill="x", **pad)
        ttk.Entry(box, textvariable=self.archive_var).grid(row=0, column=0, sticky="ew")
        ttk.Button(box, text="Выбрать ZIP…", width=14, command=self.pick_zip).grid(row=0, column=1, padx=(8, 0))
        ttk.Button(box, text="Папка…", command=self.pick_folder).grid(row=0, column=2, padx=(6, 0))
        ttk.Label(box, foreground="#666",
                  text="Архив запрашивается в VK: Настройки → Защита данных → «Запросить архив».\n"
                       "Подойдёт и сам ZIP-файл, и распакованная папка.").grid(
            row=1, column=0, columnspan=3, sticky="w", pady=(6, 0))
        box.columnconfigure(0, weight=1)

        # 2. Период
        box = ttk.LabelFrame(main, text=" 2. Период (по дате сообщения) ", padding=10)
        box.pack(fill="x", **pad)
        ttk.Label(box, text="с").pack(side="left")
        ttk.Combobox(box, textvariable=self.year_from, values=years, width=7, state="readonly").pack(
            side="left", padx=(6, 12))
        ttk.Label(box, text="по").pack(side="left")
        ttk.Combobox(box, textvariable=self.year_to, values=years, width=7, state="readonly").pack(
            side="left", padx=(6, 12))
        ttk.Label(box, text="год включительно", foreground="#666").pack(side="left")

        # 3. Куда сохранить
        box = ttk.LabelFrame(main, text=" 3. Куда сохранить фото ", padding=10)
        box.pack(fill="x", **pad)
        out_entry = ttk.Entry(box, textvariable=self.output_var)
        out_entry.grid(row=0, column=0, sticky="ew")
        out_entry.bind("<Key>", lambda e: setattr(self, "output_touched", True))
        ttk.Button(box, text="Изменить…", command=self.pick_output).grid(row=0, column=1, padx=(8, 0))
        ttk.Checkbutton(box, variable=self.retry_var,
                        text="Ещё раз проверить фото, которые в прошлый раз были недоступны").grid(
            row=1, column=0, columnspan=2, sticky="w", pady=(6, 0))
        box.columnconfigure(0, weight=1)

        # Кнопки
        buttons = ttk.Frame(main)
        buttons.pack(fill="x", **pad)
        self.download_btn = ttk.Button(buttons, text="Скачать фото", style="Accent.TButton",
                                       command=lambda: self.start(scan_only=False))
        self.download_btn.pack(side="left", ipadx=10, ipady=3)
        self.scan_btn = ttk.Button(buttons, text="Только анализ", command=lambda: self.start(scan_only=True))
        self.scan_btn.pack(side="left", padx=(8, 0), ipady=3)
        self.stop_btn = ttk.Button(buttons, text="Стоп", command=self.stop, state="disabled")
        self.stop_btn.pack(side="left", padx=(8, 0), ipady=3)
        self.open_btn = ttk.Button(buttons, text="Открыть папку с фото", command=self.open_output)
        self.open_btn.pack(side="right", ipady=3)

        # Прогресс
        prog = ttk.Frame(main)
        prog.pack(fill="x", **pad)
        self.progress = ttk.Progressbar(prog, mode="determinate", maximum=1)
        self.progress.pack(fill="x")
        ttk.Label(prog, textvariable=self.status_var, wraplength=600).pack(fill="x", pady=(4, 0))
        ttk.Label(prog, textvariable=self.result_var, style="Result.TLabel").pack(fill="x", pady=(4, 0))

        # Журнал
        logbox = ttk.Frame(main)
        logbox.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(logbox, height=12, wrap="word", font=("Consolas", 9), state="disabled",
                           relief="solid", borderwidth=1)
        scroll = ttk.Scrollbar(logbox, command=self.log.yview)
        self.log.configure(yscrollcommand=scroll.set)
        self.log.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

        self._autofill_archive()
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.poll_events)

    # --- выбор путей -------------------------------------------------------
    def _autofill_archive(self) -> None:
        """Если Archive.zip лежит рядом с программой — подставляем его сразу."""
        base = Path(sys.executable).parent if getattr(sys, "frozen", False) else Path(__file__).parent
        for candidate in (base / "Archive.zip", Path.cwd() / "Archive.zip"):
            if candidate.is_file():
                self.set_archive(candidate)
                return

    def set_archive(self, path: Path) -> None:
        self.archive_var.set(str(path))
        if not self.output_touched:
            # Рядом с архивом: для ZIP — в его папке, для распакованной папки — рядом с ней.
            self.output_var.set(str(path.parent / "VK_Photos"))

    def pick_zip(self) -> None:
        path = filedialog.askopenfilename(title="Выберите архив VK", filetypes=[("ZIP-архив", "*.zip"),
                                                                              ("Все файлы", "*.*")])
        if path:
            self.set_archive(Path(path))

    def pick_folder(self) -> None:
        path = filedialog.askdirectory(title="Выберите распакованную папку архива VK")
        if path:
            self.set_archive(Path(path))

    def pick_output(self) -> None:
        path = filedialog.askdirectory(title="Куда сохранить фото")
        if path:
            self.output_touched = True
            self.output_var.set(str(Path(path) / "VK_Photos") if not path.endswith("VK_Photos") else path)

    def open_output(self) -> None:
        out = Path(self.output_var.get().strip() or ".")
        if out.is_dir():
            _open_path(out)
        else:
            messagebox.showinfo(APP_TITLE, "Папка ещё не создана — сначала скачайте фото.")

    # --- запуск ------------------------------------------------------------
    def _options(self, scan_only: bool) -> ExportOptions | None:
        archive = self.archive_var.get().strip().strip('"')
        if not archive:
            messagebox.showwarning(APP_TITLE, "Сначала выберите архив VK (Archive.zip).")
            return None
        if not Path(archive).exists():
            messagebox.showerror(APP_TITLE, f"Архив не найден:\n{archive}")
            return None
        y1, y2 = int(self.year_from.get()), int(self.year_to.get())
        if y1 > y2:
            messagebox.showwarning(APP_TITLE, "Год «с» не может быть больше года «по».")
            return None
        output = self.output_var.get().strip().strip('"')
        if not scan_only and not output:
            messagebox.showwarning(APP_TITLE, "Укажите, куда сохранить фото.")
            return None
        return ExportOptions(
            archive=Path(archive),
            output=None if scan_only else Path(output).resolve(),
            start=date(y1, 1, 1), end=date(y2, 12, 31),
            scan_only=scan_only, retry_failed=self.retry_var.get())

    def start(self, scan_only: bool) -> None:
        if self.worker and self.worker.is_alive():
            return
        opts = self._options(scan_only)
        if opts is None:
            return
        self.stop_event.clear()
        self.set_running(True)
        self.result_var.set("")
        self.progress.configure(value=0, maximum=1)
        self.clear_log()
        period = f"{opts.start.year}–{opts.end.year}" if opts.start.year != opts.end.year else str(opts.start.year)
        self.write_log(f"{'Анализ' if scan_only else 'Скачивание'} фото за {period}\nАрхив: {opts.archive}\n")
        self.worker = threading.Thread(target=self._work, args=(opts,), daemon=True)
        self.worker.start()

    def _work(self, opts: ExportOptions) -> None:
        put = self.events.put
        try:
            result = run_export(opts, show_progress=False, stop_event=self.stop_event,
                                log=lambda text: put(("log", text)),
                                on_progress=lambda stage, done, total: put(("progress", stage, done, total)))
            put(("done", opts, result))
        except Cancelled:
            put(("cancelled",))
        except ExportError as e:
            put(("error", str(e), None))
        except Exception as e:  # покажем пользователю, а не упадём молча
            put(("error", f"{type(e).__name__}: {e}", traceback.format_exc()))

    def stop(self) -> None:
        self.stop_event.set()
        self.stop_btn.configure(state="disabled")
        self.status_var.set("Останавливаю… (дожидаюсь начатых загрузок, прогресс сохранится)")

    def set_running(self, running: bool) -> None:
        state = "disabled" if running else "normal"
        self.download_btn.configure(state=state)
        self.scan_btn.configure(state=state)
        self.stop_btn.configure(state="normal" if running else "disabled")

    # --- события из рабочего потока ----------------------------------------
    def poll_events(self) -> None:
        try:
            while True:
                self.handle(self.events.get_nowait())   # строго по порядку: «готово» идёт последним
        except queue.Empty:
            pass
        if self.closing and not (self.worker and self.worker.is_alive()):
            self.root.destroy()
            return
        self.root.after(100, self.poll_events)

    def handle(self, event: tuple) -> None:
        kind = event[0]
        if kind == "progress":
            _, stage, done, total = event
            self.progress.configure(maximum=max(total, 1), value=done)
            if not self.stop_event.is_set():
                self.status_var.set(f"{STAGE_LABELS.get(stage, stage)}: {done} из {total}")
        elif kind == "log":
            self.write_log(event[1])
        elif kind == "done":
            _, opts, result = event
            self.set_running(False)
            self.write_log(result.text)
            r = result.summary["results"]
            total = result.summary["download_candidates"]
            if opts.scan_only:
                self.status_var.set("Анализ завершён.")
                self.result_var.set(f"Найдено фото и изображений за период: {total}. "
                                    "Нажмите «Скачать фото».")
            else:
                stopped = "Остановлено. " if result.cancelled else "Готово! "
                self.status_var.set(stopped + (f"Фото сохранены в {result.out_dir}" if result.out_dir else ""))
                self.result_var.set(f"Скачано: {r['downloaded_records']} из {total}   •   "
                                    f"недоступно: {r['failed_records']}")
                if result.cancelled:
                    self.write_log("\nОстановлено. Нажмите «Скачать фото» ещё раз, чтобы продолжить с того же места.")
        elif kind == "cancelled":
            self.set_running(False)
            self.status_var.set("Остановлено.")
        elif kind == "error":
            _, message, tb = event
            self.set_running(False)
            self.status_var.set("Ошибка.")
            if tb:
                self.write_log("\n" + tb)
            messagebox.showerror(APP_TITLE, message)

    def write_log(self, text: str) -> None:
        self.log.configure(state="normal")
        self.log.insert("end", text.rstrip() + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def clear_log(self) -> None:
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")

    def on_close(self) -> None:
        if self.worker and self.worker.is_alive():
            if not messagebox.askyesno(APP_TITLE, "Идёт работа. Остановить и закрыть?\n"
                                                  "Прогресс сохранится, продолжить можно позже."):
                return
            self.stop()
            self.closing = True
            return
        self.root.destroy()


def _self_test(archive: str, report: str) -> None:
    """Проверка собранного exe без окна: анализ архива, результат — в файл."""
    try:
        result = run_export(ExportOptions(archive=Path(archive), scan_only=True), show_progress=False)
        text = result.text
    except Exception:
        text = "ERROR\n" + traceback.format_exc()
    Path(report).write_text(text, encoding="utf-8")


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--self-test":
        _self_test(sys.argv[2], sys.argv[3])
        return
    _enable_high_dpi()
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
