import io
import os
import queue
import threading
import traceback
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

import fitz  # PyMuPDF
from PIL import Image

A4_W, A4_H = 2480, 3508  # A4 портрет при 300 DPI


def find_split_px(img):
    """Ищет x-координату корешка (самой светлой вертикальной полосы)."""
    gray = img.convert("L")
    w, h = gray.size
    px = gray.load()
    x_start, x_end = int(w * 0.3), int(w * 0.7)
    step_y = max(1, h // 400)

    best_x, min_dark = w // 2, 10**9
    for x in range(x_start, x_end):
        dark = 0
        for y in range(0, h, step_y):
            if px[x, y] < 200:
                dark += 1
        if dark < min_dark:
            min_dark, best_x = dark, x

    if min_dark > (h / step_y) * 0.05:
        best_x = w // 2
    return best_x


def process_pdf(src_path, dst_path, log=print, should_stop=lambda: False):
    doc = fitz.open(src_path)
    out = fitz.open()
    total = len(doc)

    for i, page in enumerate(doc, 1):
        if should_stop():
            log("Остановлено пользователем.")
            break

        log(f"Обработка страницы {i}/{total}...")

        zoom = 300 / 72
        mat = fitz.Matrix(zoom, zoom)
        pix = page.get_pixmap(matrix=mat, alpha=False)
        img = Image.open(io.BytesIO(pix.tobytes("png")))
        w, h = img.size

        if w > h:
            split_x = find_split_px(img)
            halves = [
                img.crop((0, 0, split_x, h)),
                img.crop((split_x, 0, w, h)),
            ]
        else:
            halves = [img]

        for half in halves:
            hw, hh = half.size
            scale = min(A4_W / hw, A4_H / hh)
            new_w, new_h = int(hw * scale), int(hh * scale)
            half = half.resize((new_w, new_h), Image.LANCZOS)

            canvas = Image.new("RGB", (A4_W, A4_H), "white")
            canvas.paste(half, ((A4_W - new_w) // 2, (A4_H - new_h) // 2))

            buf = io.BytesIO()
            canvas.save(buf, format="JPEG", quality=90)

            new_page = out.new_page(width=595, height=842)
            new_page.insert_image(new_page.rect, stream=buf.getvalue())

    out.save(dst_path)
    doc.close()
    out.close()


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("PDF Splitter A5 → A4")
        self.geometry("640x420")
        self.resizable(False, False)

        self.pdf_path = tk.StringVar()
        self.out_path = tk.StringVar()

        # Очередь сообщений из рабочего потока в главный
        self.msg_queue = queue.Queue()
        self.stop_flag = threading.Event()

        self._build_ui()
        self.after(50, self._drain_queue)

    def _build_ui(self):
        pad = {"padx": 10, "pady": 6}

        ttk.Label(self, text="Исходный PDF:").grid(row=0, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.pdf_path, width=60).grid(
            row=0, column=1, sticky="we", **pad
        )
        ttk.Button(self, text="Обзор...", command=self.pick_pdf).grid(
            row=0, column=2, **pad
        )

        ttk.Label(self, text="Куда сохранить:").grid(row=1, column=0, sticky="w", **pad)
        ttk.Entry(self, textvariable=self.out_path, width=60).grid(
            row=1, column=1, sticky="we", **pad
        )
        ttk.Button(self, text="Обзор...", command=self.pick_out).grid(
            row=1, column=2, **pad
        )

        self.btn_run = ttk.Button(self, text="Обработать", command=self.run)
        self.btn_run.grid(row=2, column=0, columnspan=3, pady=(16, 6))

        self.progress = ttk.Progressbar(self, mode="indeterminate", length=580)
        self.progress.grid(row=3, column=0, columnspan=3, padx=10, pady=6)

        self.log_box = tk.Text(self, height=10, width=82, state="disabled")
        self.log_box.grid(row=4, column=0, columnspan=3, padx=10, pady=6)

        self.columnconfigure(1, weight=1)

    # ---------- выбор файлов ----------

    def pick_pdf(self):
        path = filedialog.askopenfilename(
            title="Выберите PDF",
            filetypes=[("PDF files", "*.pdf"), ("All files", "*.*")],
        )
        if not path:
            return
        self.pdf_path.set(path)
        base, _ = os.path.splitext(path)
        self.out_path.set(base + "_A4.pdf")

    def pick_out(self):
        path = filedialog.asksaveasfilename(
            title="Сохранить как",
            defaultextension=".pdf",
            filetypes=[("PDF files", "*.pdf")],
        )
        if path:
            self.out_path.set(path)

    # ---------- лог через очередь ----------

    def log(self, msg):
        """Вызывается только из главного потока."""
        self.log_box.configure(state="normal")
        self.log_box.insert("end", msg + "\n")
        self.log_box.see("end")
        self.log_box.configure(state="disabled")

    def _drain_queue(self):
        """Раз в 50 мс забираем сообщения из очереди и печатаем в лог."""
        try:
            while True:
                msg = self.msg_queue.get_nowait()
                kind, payload = msg
                if kind == "log":
                    self.log(payload)
                elif kind == "done":
                    self.progress.stop()
                    self.btn_run.configure(state="normal")
                    messagebox.showinfo("Успех", f"Файл сохранён:\n{payload}")
                elif kind == "error":
                    self.progress.stop()
                    self.btn_run.configure(state="normal")
                    self.log(payload)
                    messagebox.showerror("Ошибка", payload.splitlines()[-1])
                elif kind == "stopped":
                    self.progress.stop()
                    self.btn_run.configure(state="normal")
        except queue.Empty:
            pass
        self.after(50, self._drain_queue)

    # ---------- запуск ----------

    def run(self):
        src = self.pdf_path.get().strip()
        dst = self.out_path.get().strip()

        if not src or not os.path.isfile(src):
            messagebox.showerror("Ошибка", "Выберите существующий PDF-файл.")
            return
        if not dst:
            messagebox.showerror("Ошибка", "Укажите путь для сохранения.")
            return
        if os.path.abspath(src) == os.path.abspath(dst):
            messagebox.showerror("Ошибка", "Путь сохранения совпадает с исходником.")
            return

        self.btn_run.configure(state="disabled")
        self.progress.start(10)
        self.log_box.configure(state="normal")
        self.log_box.delete("1.0", "end")
        self.log_box.configure(state="disabled")
        self.stop_flag.clear()

        threading.Thread(
            target=self._worker, args=(src, dst), daemon=True
        ).start()

    def _worker(self, src, dst):
        """Выполняется в отдельном потоке. НЕ трогает GUI напрямую!"""
        try:
            self.msg_queue.put(("log", f"Открыт: {src}"))
            process_pdf(
                src, dst,
                log=lambda m: self.msg_queue.put(("log", m)),
                should_stop=self.stop_flag.is_set,
            )
            self.msg_queue.put(("log", f"Готово: {dst}"))
            self.msg_queue.put(("done", dst))
        except Exception as e:
            tb = traceback.format_exc()
            self.msg_queue.put(("error", f"Ошибка: {e}\n{tb}"))


if __name__ == "__main__":
    App().mainloop()