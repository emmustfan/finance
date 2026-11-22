import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pypdfium2 as pdfium
from cryptography.exceptions import InvalidTag
from PIL import ImageTk

from documents import CARD_FIELDS, Card, Catalog, read_pdf, tag_names
from sample import sample_pdf
from vault import copy_verified, create

VAULT_TYPES = [("Архив", "*.fvault")]


class ArchiveWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.pdf: bytes | None = None
        self.document = None
        self.image = None
        self.page = 0
        self.catalog: Catalog | None = None
        self.results: list[int] = []

        root.title("Семейный архив")
        toolbar = ttk.Frame(root, padding=8)
        toolbar.pack(fill="x")
        for label, action in (
            ("Новый архив", lambda: self.create_archive(False)),
            ("Создать тестовый архив", lambda: self.create_archive(True)),
            ("Открыть архив", self.open_archive),
            ("Создать копию", lambda: self.copy_archive(False)),
            ("Восстановить из копии", lambda: self.copy_archive(True)),
        ):
            ttk.Button(toolbar, text=label, command=action).pack(side="left", padx=3)

        body = ttk.Frame(root, padding=8)
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, width=260)
        sidebar.pack(side="left", fill="y", padx=(0, 12))
        ttk.Label(sidebar, text="Поиск документов").pack(anchor="w")
        self.query = tk.StringVar()
        ttk.Entry(sidebar, textvariable=self.query, width=29).pack(fill="x", pady=(3, 8))
        ttk.Label(sidebar, text="Фильтр по тегу").pack(anchor="w")
        tag_row = ttk.Frame(sidebar)
        tag_row.pack(fill="x", pady=(3, 8))
        self.tag_filter = ttk.Combobox(tag_row, width=16, state="disabled")
        self.tag_filter.pack(side="left", fill="x", expand=True)
        self.tag_filter.bind("<<ComboboxSelected>>", lambda _: self.refresh_cards())
        self.clear_tag_button = ttk.Button(
            tag_row, text="Сбросить", command=self.clear_tag, state="disabled"
        )
        self.clear_tag_button.pack(side="left", padx=(4, 0))
        self.add_button = ttk.Button(
            sidebar, text="Добавить документ", command=self.add_card, state="disabled"
        )
        self.add_button.pack(fill="x", pady=(0, 8))
        self.listbox = tk.Listbox(sidebar, width=29, height=12, exportselection=False)
        self.listbox.pack(fill="both", expand=True)
        self.listbox.bind("<<ListboxSelect>>", self.select_card)
        self.query.trace_add("write", lambda *_: self.refresh_cards())

        detail = ttk.Frame(body)
        detail.pack(side="left", fill="both", expand=True)
        self.details = ttk.Label(detail, wraplength=510, justify="left")
        self.details.pack(anchor="w")
        self.edit_tags_button = ttk.Button(
            detail, text="Изменить теги…", command=self.edit_tags, state="disabled"
        )
        self.edit_tags_button.pack(anchor="w", pady=(4, 0))
        controls = ttk.Frame(detail, padding=(0, 8))
        controls.pack(fill="x")
        ttk.Button(controls, text="←", command=lambda: self.move_page(-1)).pack(side="left")
        ttk.Button(controls, text="→", command=lambda: self.move_page(1)).pack(side="left")
        self.status = ttk.Label(controls, text="Откройте зашифрованный архив")
        self.status.pack(side="left", padx=10)
        self.print_button = ttk.Button(
            controls, text="Печать на принтере по умолчанию", command=self.print_pdf,
            state="disabled",
        )
        self.print_button.pack(side="right")
        self.preview = ttk.Label(detail, anchor="center")
        self.preview.pack(fill="both", expand=True, padx=8, pady=8)

    def password(self, prompt: str) -> str | None:
        return simpledialog.askstring("Пароль архива", prompt, show="*", parent=self.root)

    def save_path(self, title: str) -> Path | None:
        path = filedialog.asksaveasfilename(
            title=title, defaultextension=".fvault", filetypes=VAULT_TYPES, parent=self.root
        )
        return Path(path) if path else None

    def source_path(self, title: str) -> Path | None:
        path = filedialog.askopenfilename(title=title, filetypes=VAULT_TYPES, parent=self.root)
        return Path(path) if path else None

    def error(self, exc: Exception) -> None:
        detail = str(exc)
        if isinstance(exc, InvalidTag):
            detail = "Неверный пароль или нарушена целостность архива."
        if isinstance(exc, FileExistsError):
            detail = "Этот файл уже существует. Выберите другое имя."
        messagebox.showerror("Операция не выполнена", detail, parent=self.root)

    def create_archive(self, sample: bool) -> None:
        path = self.save_path("Создать тестовый архив" if sample else "Новый архив")
        if path is None:
            return
        password = self.password("Новый пароль, от 12 символов")
        if password is None:
            return
        confirmation = self.password("Повторите новый пароль")
        if confirmation is None:
            return
        if password != confirmation:
            self.error(ValueError("Пароли не совпадают."))
            return
        try:
            if sample:
                create(path, sample_pdf(), password)
            else:
                Catalog.create(path, password)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.show_archive(path, password)

    def open_archive(self) -> None:
        path = self.source_path("Открыть архив")
        if path is None:
            return
        password = self.password("Введите пароль")
        if password is not None:
            self.show_archive(path, password)

    def show_archive(self, path: Path, password: str) -> None:
        try:
            catalog = Catalog.open(path, password)
        except (OSError, ValueError, InvalidTag) as exc:
            self.error(exc)
            return
        self.catalog = catalog
        self.root.title(f"Семейный архив — {path.name}")
        self.add_button.configure(state="disabled" if catalog.legacy else "normal")
        self.tag_filter.set("")
        self.refresh_tags()
        if self.query.get():
            self.query.set("")
        else:
            self.refresh_cards()

    def refresh_tags(self) -> None:
        active = self.tag_filter.get().casefold()
        tags = self.catalog.available_tags
        self.tag_filter.configure(values=tags, state="readonly" if tags else "disabled")
        self.tag_filter.set(next((tag for tag in tags if tag.casefold() == active), ""))

    def clear_tag(self) -> None:
        self.tag_filter.set("")
        self.refresh_cards()

    def refresh_cards(self, preferred_index: int | None = None) -> None:
        cards = self.catalog.cards if self.catalog is not None else ()
        query, tag = self.query.get(), self.tag_filter.get()
        self.clear_tag_button.configure(state="normal" if tag else "disabled")
        self.results = [
            index for index, card in enumerate(cards) if card.matches(query, tag=tag)
        ]
        self.listbox.delete(0, "end")
        for index in self.results:
            card = cards[index]
            caption = f"{card.title} · {card.number}" if card.number else card.title
            self.listbox.insert("end", caption)
        if self.results:
            row = self.results.index(preferred_index) if preferred_index in self.results else 0
            self.listbox.selection_set(row)
            self.listbox.see(row)
        self.select_card()

    def select_card(self, _event=None) -> None:
        selected = self.listbox.curselection()
        if self.document is not None:
            self.document.close()
        self.document = self.pdf = self.image = None
        self.preview.configure(image="")
        self.print_button.configure(state="disabled")
        self.edit_tags_button.configure(state="disabled")
        self.details.configure(text="")
        self.status.configure(text="Документы не найдены" if self.catalog else "Откройте архив")
        if not selected or self.catalog is None:
            return
        card = self.catalog.cards[self.results[selected[0]]]
        if not self.catalog.legacy:
            self.edit_tags_button.configure(state="normal")
        lines = [f"{label}: {getattr(card, key)}" for key, label in CARD_FIELDS[:8] if getattr(card, key)]
        if card.address:
            lines.append(f"Оригинал: {card.address}")
        self.details.configure(text="\n".join(lines))
        self.status.configure(text="У документа нет PDF")
        if card.pdf is None:
            return
        document = None
        try:
            document = pdfium.PdfDocument(card.pdf)
            image = self.render(document, 0)
        except (OSError, ValueError, IndexError, pdfium.PdfiumError, tk.TclError) as exc:
            if document is not None:
                document.close()
            self.status.configure(text="Не удалось открыть PDF")
            self.error(exc)
            return
        self.document, self.pdf = document, card.pdf
        self.set_page(image, 0)
        self.print_button.configure(state="normal")

    def add_card(self) -> None:
        if self.catalog is None or self.catalog.legacy:
            return
        dialog = CardDialog(self.root, "Новый документ")
        if dialog.result is None:
            return
        password = self.password("Введите пароль архива, чтобы сохранить документ")
        if password is None:
            return
        try:
            self.catalog = self.catalog.add(dialog.result, password)
        except (OSError, ValueError, InvalidTag) as exc:
            self.error(exc)
            return
        self.refresh_tags()
        index = len(self.catalog.cards) - 1
        if not self.catalog.cards[index].matches(self.query.get(), tag=self.tag_filter.get()):
            self.tag_filter.set("")
            self.query.set("")
        self.refresh_cards(index)

    def edit_tags(self) -> None:
        selected = self.listbox.curselection()
        if not selected or self.catalog is None or self.catalog.legacy:
            return
        index = self.results[selected[0]]
        current = self.catalog.cards[index].tags
        value = simpledialog.askstring(
            "Теги документа",
            "Введите теги через запятую, например Анна, 2024.\n"
            "Оставьте поле пустым, чтобы удалить все теги.",
            initialvalue=current, parent=self.root,
        )
        if value is None or ", ".join(tag_names(value)) == current:
            return
        password = self.password("Введите пароль архива, чтобы сохранить теги")
        if password is None:
            return
        try:
            self.catalog = self.catalog.set_tags(index, value, password)
        except (OSError, ValueError, InvalidTag) as exc:
            self.error(exc)
            return
        self.refresh_tags()
        self.refresh_cards(index)

    @staticmethod
    def render(document, index: int):
        page = document[index]
        try:
            scale = 600 / max(page.get_size())
            bitmap = page.render(scale=scale)
            try:
                return ImageTk.PhotoImage(bitmap.to_pil())
            finally:
                bitmap.close()
        finally:
            page.close()

    def set_page(self, image, index: int) -> None:
        self.image, self.page = image, index
        self.preview.configure(image=image)
        self.status.configure(text=f"Страница {index + 1} из {len(self.document)}")

    def move_page(self, delta: int) -> None:
        index = self.page + delta
        if self.document is not None and 0 <= index < len(self.document):
            try:
                self.set_page(self.render(self.document, index), index)
            except (ValueError, pdfium.PdfiumError, tk.TclError) as exc:
                self.error(exc)

    def copy_archive(self, restore: bool) -> None:
        source = self.source_path("Выберите резервную копию" if restore else "Выберите архив")
        if source is None:
            return
        destination = self.save_path("Куда восстановить архив" if restore else "Куда сохранить копию")
        if destination is None:
            return
        password = self.password("Введите пароль исходного архива")
        if password is None:
            return
        try:
            copy_verified(source, destination, password)
        except (OSError, ValueError, InvalidTag) as exc:
            self.error(exc)
            return
        if restore:
            self.show_archive(destination, password)
        else:
            messagebox.showinfo("Копия создана", str(destination), parent=self.root)

    def print_pdf(self) -> None:
        if self.pdf is None or not messagebox.askokcancel(
            "Печать", "Отправить PDF на принтер по умолчанию?", parent=self.root
        ):
            return
        try:
            result = subprocess.run(
                ["/usr/bin/lp", "-t", "Family archive PDF", "-"],
                input=self.pdf, capture_output=True, timeout=30, check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace").strip() or "Ошибка CUPS.")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.error(exc)
            return
        messagebox.showinfo("Печать", "Задание передано в очередь печати.", parent=self.root)


class CardDialog(simpledialog.Dialog):
    def body(self, master):
        self.pdf = None
        self.inputs = {}
        for row, (key, label) in enumerate(CARD_FIELDS):
            if key == "tags":
                label = "Теги через запятую"
            ttk.Label(master, text=label).grid(row=row, column=0, sticky="w", padx=6, pady=2)
            entry = ttk.Entry(master, width=40)
            entry.grid(row=row, column=1, sticky="ew", padx=6, pady=2)
            self.inputs[key] = entry
        row = len(CARD_FIELDS)
        ttk.Button(master, text="Выбрать PDF…", command=self.choose_pdf).grid(row=row, column=0)
        self.filename = ttk.Label(master, text="PDF не выбран", wraplength=270)
        self.filename.grid(row=row, column=1, sticky="w", padx=6, pady=6)
        return self.inputs["title"]

    def choose_pdf(self) -> None:
        filename = filedialog.askopenfilename(
            title="Выберите PDF", filetypes=[("PDF", "*.pdf")], parent=self
        )
        if not filename:
            return
        try:
            path = Path(filename)
            pdf = read_pdf(path)
            with pdfium.PdfDocument(pdf) as doc:
                if len(doc) == 0:
                    raise ValueError("В PDF нет страниц.")
        except (OSError, ValueError, pdfium.PdfiumError) as exc:
            messagebox.showerror("Не удалось приложить PDF", str(exc), parent=self)
            return
        self.pdf = pdf
        self.filename.configure(text=path.name)

    def validate(self):
        card = Card(**{key: entry.get().strip() for key, entry in self.inputs.items()}, pdf=self.pdf)
        try:
            card.validate()
        except ValueError as exc:
            messagebox.showerror("Проверьте карточку", str(exc), parent=self)
            return False
        self.result = card
        return True


def main() -> None:
    root = tk.Tk()
    root.minsize(835, 690)
    ArchiveWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
