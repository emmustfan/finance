import subprocess
import tkinter as tk
from tkinter import font
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pypdfium2 as pdfium
from cryptography.exceptions import InvalidTag
from PIL import ImageTk

from documents import CARD_FIELDS, Card, Catalog, read_pdf, tag_names
from sample import sample_pdf
from vault import copy_verified

VAULT_TYPES = [("Архив", "*.fvault")]


def configure_appearance(root: tk.Tk) -> None:
    style = ttk.Style(root)
    style.theme_use("clam")
    system = root.tk.call("tk", "windowingsystem")
    preferred = {"aqua": "Helvetica Neue", "x11": "DejaVu Sans"}.get(system)
    family = font.nametofont("TkDefaultFont", root=root).actual("family")
    if preferred in font.families(root):
        family = preferred
    for name, size, weight in (
        ("TkDefaultFont", 12, "normal"), ("TkTextFont", 12, "normal"),
        ("ArchiveTitleFont", 22, "bold"), ("DocumentTitleFont", 20, "bold"),
        ("SectionFont", 12, "bold"),
    ):
        try:
            typeface = font.nametofont(name, root=root)
        except tk.TclError:
            typeface = font.Font(root=root, name=name, exists=False)
            typeface.delete_font = False
        typeface.configure(family=family, size=size, weight=weight)
    surface, background, ink, muted = "#FFFFFF", "#EEF2F8", "#152638", "#496075"
    root.configure(background=background)
    style.configure(".", background=surface, foreground=ink, font="TkDefaultFont")
    style.configure("Workspace.TFrame", background=background)
    style.configure("ArchiveTitle.TLabel", font="ArchiveTitleFont")
    style.configure("DocumentTitle.TLabel", font="DocumentTitleFont")
    style.configure("Section.TLabel", font="SectionFont")
    style.configure("Muted.TLabel", foreground=muted)
    style.configure("Preview.TLabel", background=background)
    style.configure("TButton", padding=(11, 7), background="#E7EDF5", bordercolor="#CBD6E4")
    style.map("TButton", background=[("active", "#D8E3F1")], foreground=[("disabled", muted)])
    style.configure("Primary.TButton", background="#2054BB", foreground="#FFFFFF", bordercolor="#2054BB")
    style.map("Primary.TButton",
              background=[("disabled", "#E7EDF5"), ("active", "#173F92")],
              foreground=[("disabled", muted), ("!disabled", "#FFFFFF")])
    style.configure("TEntry", padding=6, fieldbackground=surface, insertcolor=ink)
    style.configure("TCombobox", padding=5, fieldbackground=surface, insertcolor=ink)
    style.map("TCombobox", fieldbackground=[("readonly", surface)], foreground=[("readonly", ink)])
    style.configure("Treeview", rowheight=34, background=surface, fieldbackground=surface)
    style.configure("Treeview.Heading", padding=6, background="#E7EDF5", font="SectionFont")
    style.map("Treeview", background=[("selected", "#DCE9FF")], foreground=[("selected", "#153976")])


class ArchiveWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.pdf: bytes | None = None
        self.document = None
        self.image = None
        self.page = 0
        self.preview_resize = None
        self.catalog: Catalog | None = None
        self.can_edit = False
        self.displayed_card: Card | None = None

        root.title("Семейный архив")
        header = ttk.Frame(root, padding=(22, 14))
        header.pack(fill="x")
        ttk.Label(header, text="Семейный архив", style="ArchiveTitle.TLabel").pack(side="left")
        self.archive_name = ttk.Label(
            header, text="Архив не открыт", style="Muted.TLabel", wraplength=360
        )
        self.archive_name.pack(side="left", padx=16)
        self.close_archive_button = ttk.Button(
            header, text="Закрыть архив", command=self.close_archive, state="disabled"
        )
        self.close_archive_button.pack(side="right")

        toolbar = ttk.Frame(root, padding=(18, 0, 18, 12))
        toolbar.pack(fill="x")
        for label, action in (
            ("Новый архив", lambda: self.create_archive(False)),
            ("Открыть архив", self.open_archive),
            ("Создать копию", lambda: self.copy_archive(False)),
            ("Восстановить из копии", lambda: self.copy_archive(True)),
            ("Создать тестовый архив", lambda: self.create_archive(True)),
        ):
            ttk.Button(toolbar, text=label, command=action).pack(side="left", padx=3)

        body = ttk.Frame(root, padding=14, style="Workspace.TFrame")
        body.pack(fill="both", expand=True)
        sidebar = ttk.Frame(body, padding=14)
        sidebar.pack(side="left", fill="y", padx=(0, 14))
        ttk.Label(sidebar, text="Документы", style="Section.TLabel").pack(anchor="w", pady=(0, 12))
        ttk.Label(sidebar, text="Поиск документов").pack(anchor="w")
        self.query = tk.StringVar()
        self.search = ttk.Entry(sidebar, textvariable=self.query, width=29, state="disabled")
        self.search.pack(fill="x", pady=(3, 8))
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
            sidebar, text="+  Добавить документ", command=self.save_card, state="disabled",
            style="Primary.TButton",
        )
        self.add_button.pack(fill="x", pady=(0, 8))
        self.archive_hint = ttk.Label(
            sidebar, text="Создайте или откройте архив, чтобы добавить документ.",
            wraplength=255, justify="left", style="Muted.TLabel",
        )
        self.archive_hint.pack(anchor="w", pady=(0, 8))
        table = ttk.Frame(sidebar)
        table.pack(fill="both", expand=True)
        self.card_table = ttk.Treeview(
            table, columns=("number", "title"), show="headings", selectmode="browse", height=8,
        )
        self.card_table.heading("number", text="№", anchor="center")
        self.card_table.heading("title", text="Название", anchor="w")
        self.card_table.column("number", width=56, stretch=False, anchor="center")
        self.card_table.column("title", width=198, minwidth=120)
        scrollbar = ttk.Scrollbar(table, orient="vertical", command=self.card_table.yview)
        self.card_table.configure(yscrollcommand=scrollbar.set)
        self.card_table.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")
        self.table = table
        self.card_table.bind("<<TreeviewSelect>>", self.select_card)
        self.query.trace_add("write", lambda *_: self.refresh_cards())

        detail = ttk.Frame(body, padding=16)
        detail.pack(side="left", fill="both", expand=True)
        self.card_title = ttk.Label(
            detail, text="Документ", style="DocumentTitle.TLabel", wraplength=500, justify="left"
        )
        self.card_title.pack(anchor="w")
        self.card_meta = ttk.Label(detail, style="Muted.TLabel", wraplength=500, justify="left")
        self.card_meta.pack(anchor="w", pady=(2, 8))
        self.details = ttk.Label(detail, wraplength=500, justify="left")
        self.details.pack(anchor="w", pady=(0, 8))
        actions = ttk.Frame(detail)
        actions.pack(fill="x")
        self.edit_card_button = ttk.Button(
            actions, text="Изменить карточку…", command=lambda: self.save_card(edit=True),
            state="disabled",
        )
        self.edit_card_button.pack(side="left", padx=(0, 8))
        self.edit_tags_button = ttk.Button(
            actions, text="Изменить теги…", command=self.edit_tags, state="disabled"
        )
        self.edit_tags_button.pack(side="left")
        ttk.Separator(detail).pack(fill="x", pady=(12, 0))
        controls = ttk.Frame(detail, padding=(0, 8))
        controls.pack(fill="x")
        self.previous_page = ttk.Button(
            controls, text="←", width=3, command=lambda: self.move_page(-1), state="disabled",
        )
        self.previous_page.pack(side="left", padx=(0, 3))
        self.next_page = ttk.Button(
            controls, text="→", width=3, command=lambda: self.move_page(1), state="disabled",
        )
        self.next_page.pack(side="left")
        self.status = ttk.Label(controls, text="Откройте зашифрованный архив", style="Muted.TLabel")
        self.status.pack(side="left", padx=10)
        self.print_button = ttk.Button(
            controls, text="Печать PDF", command=self.print_pdf,
            state="disabled",
        )
        self.print_button.pack(side="right")
        self.preview_area = ttk.Frame(detail, style="Workspace.TFrame")
        self.preview_area.pack(fill="both", expand=True)
        self.preview_area.bind("<Configure>", self.resize_preview)
        self.preview = ttk.Label(self.preview_area, anchor="center", style="Preview.TLabel")
        self.preview.place(relwidth=1, relheight=1)

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
            cards = (Card("Тестовый документ", pdf=sample_pdf()),) if sample else ()
            catalog = Catalog.create(path, password, cards=cards)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.show_catalog(catalog)

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
        self.show_catalog(catalog)

    def show_catalog(self, catalog: Catalog) -> None:
        self.catalog = catalog
        self.can_edit = catalog.editable
        self.archive_name.configure(text=catalog.path.name)
        self.close_archive_button.configure(state="normal")
        self.search.configure(state="normal")
        self.add_button.configure(state="normal" if self.can_edit else "disabled")
        if self.can_edit:
            self.archive_hint.pack_forget()
        else:
            self.archive_hint.configure(
                text="Архив открыт для просмотра. Чтобы редактировать, сохраните личную "
                "копию через «Восстановить из копии»."
            )
            self.archive_hint.pack(anchor="w", pady=(0, 8), before=self.table)
        self.tag_filter.set("")
        self.refresh_tags()
        self.query.set("")

    def close_archive(self) -> None:
        self.catalog = None
        self.can_edit = False
        self.archive_name.configure(text="Архив не открыт")
        self.close_archive_button.configure(state="disabled")
        self.search.configure(state="disabled")
        self.add_button.configure(state="disabled")
        self.archive_hint.configure(text="Создайте или откройте архив, чтобы добавить документ.")
        self.archive_hint.pack(anchor="w", pady=(0, 8), before=self.table)
        self.refresh_tags()
        self.query.set("")

    def refresh_tags(self) -> None:
        active = self.tag_filter.get().casefold()
        tags = self.catalog.available_tags if self.catalog is not None else ()
        self.tag_filter.configure(values=tags, state="readonly" if tags else "disabled")
        self.tag_filter.set(next((tag for tag in tags if tag.casefold() == active), ""))

    def clear_tag(self) -> None:
        self.tag_filter.set("")
        self.refresh_cards()

    def refresh_cards(self, preferred_index: int | None = None) -> None:
        cards = self.catalog.cards if self.catalog is not None else ()
        query, tag = self.query.get(), self.tag_filter.get()
        self.clear_tag_button.configure(state="normal" if tag else "disabled")
        self.card_table.delete(*self.card_table.get_children())
        for index, card in enumerate(cards):
            if card.matches(query, tag=tag):
                self.card_table.insert("", "end", iid=str(index), values=(card.archive_label, card.title))
        rows = self.card_table.get_children()
        if rows:
            preferred = str(preferred_index)
            row = preferred if preferred in rows else rows[0]
            self.card_table.selection_set(row)
            self.card_table.focus(row)
            self.card_table.see(row)
        self.select_card()

    def selected_index(self) -> int | None:
        selected = self.card_table.selection()
        return int(selected[0]) if selected else None

    def select_card(self, _event=None) -> None:
        index = self.selected_index()
        card = self.catalog.cards[index] if self.catalog is not None and index is not None else None
        if card is not None and card is self.displayed_card:
            return
        self.displayed_card = card
        if self.document is not None:
            self.document.close()
        self.document = self.pdf = self.image = None
        self.preview.configure(image="")
        self.print_button.configure(state="disabled")
        self.previous_page.configure(state="disabled")
        self.next_page.configure(state="disabled")
        self.edit_tags_button.configure(state="disabled")
        self.edit_card_button.configure(state="disabled")
        self.card_title.configure(text="Документ")
        self.card_meta.configure(text="")
        self.details.configure(text="")
        self.status.configure(text="Документы не найдены" if self.catalog else "Откройте архив")
        if card is None:
            return
        if self.can_edit:
            self.edit_tags_button.configure(state="normal")
            self.edit_card_button.configure(state="normal")
        self.card_title.configure(text=card.title)
        self.card_meta.configure(text="  ·  ".join(filter(None, (f"№ {card.archive_label}", card.kind))))
        lines = [
            f"{label}: {getattr(card, key)}"
            for key, label in CARD_FIELDS[2:8] if getattr(card, key)
        ]
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

    def save_card(self, *, edit: bool = False) -> None:
        if self.catalog is None or not self.can_edit:
            return
        index = self.selected_index() if edit else None
        if edit and index is None:
            return
        card = self.catalog.cards[index] if index is not None else None
        dialog = CardDialog(self.root, self.catalog.available_kinds, card)
        if dialog.result is None:
            return
        try:
            if index is None:
                self.catalog = self.catalog.add(dialog.result)
                index = len(self.catalog.cards) - 1
            else:
                self.catalog = self.catalog.update(index, dialog.result)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.refresh_tags()
        if not self.catalog.cards[index].matches(self.query.get(), tag=self.tag_filter.get()):
            self.tag_filter.set("")
            self.query.set("")
        self.refresh_cards(index)

    def edit_tags(self) -> None:
        index = self.selected_index()
        if index is None or self.catalog is None or not self.can_edit:
            return
        current = self.catalog.cards[index].tags
        value = simpledialog.askstring(
            "Теги документа",
            "Введите теги через запятую, например Анна, 2024.\n"
            "Оставьте поле пустым, чтобы удалить все теги.",
            initialvalue=current, parent=self.root,
        )
        if value is None or ", ".join(tag_names(value)) == current:
            return
        try:
            self.catalog = self.catalog.set_tags(index, value)
        except (OSError, ValueError) as exc:
            self.error(exc)
            return
        self.refresh_tags()
        self.refresh_cards(index)

    def resize_preview(self, _event) -> None:
        if self.preview_resize is not None:
            self.root.after_cancel(self.preview_resize)
        self.preview_resize = self.root.after(100, self.redraw_preview)

    def redraw_preview(self) -> None:
        self.preview_resize = None
        if self.document is not None:
            self.move_page(0)

    def render(self, document, index: int):
        page = document[index]
        try:
            width, height = page.get_size()
            if width <= 0 or height <= 0:
                raise ValueError("У страницы PDF некорректный размер.")
            available_width = max(100, self.preview_area.winfo_width() - 16)
            available_height = max(100, self.preview_area.winfo_height() - 16)
            scale = min(available_width / width, available_height / height, 1200 / max(width, height))
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
        count = len(self.document)
        self.status.configure(text=f"Страница {index + 1} из {count}")
        self.previous_page.configure(state="normal" if index > 0 else "disabled")
        self.next_page.configure(state="normal" if index + 1 < count else "disabled")

    def move_page(self, delta: int) -> None:
        index = self.page + delta
        if self.document is not None and 0 <= index < len(self.document):
            try:
                self.set_page(self.render(self.document, index), index)
            except (ValueError, pdfium.PdfiumError, tk.TclError) as exc:
                self.error(exc)

    def copy_archive(self, restore: bool) -> None:
        if self.catalog is not None and not restore:
            destination = self.save_path("Куда сохранить копию открытого архива")
            if destination is None:
                return
            try:
                self.catalog.backup(destination)
            except (OSError, ValueError) as exc:
                self.error(exc)
                return
            messagebox.showinfo("Копия создана", str(destination), parent=self.root)
            return
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
    def __init__(self, parent, kinds: tuple[str, ...], card: Card | None):
        self.kinds = kinds
        self.card = card
        super().__init__(parent, "Изменить карточку" if card is not None else "Новый документ")

    def body(self, master):
        self.configure(background="#FFFFFF")
        master.configure(background="#FFFFFF")
        self.pdf = self.card.pdf if self.card is not None else None
        self.inputs = {}
        ttk.Label(
            master, text="Укажите название. Приложите PDF или заполните поле «Шкаф или "
            "место хранения» либо «Папка».", wraplength=560, justify="left",
            style="Muted.TLabel",
        ).pack(anchor="w", padx=14, pady=(12, 2))
        panels = ttk.Frame(master, padding=12)
        panels.pack(fill="both", expand=True)
        document_panel = ttk.Frame(panels)
        document_panel.pack(side="left", fill="y", padx=(0, 18))
        original_panel = ttk.Frame(panels)
        original_panel.pack(side="left", fill="y")
        ttk.Label(document_panel, text="Документ", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Label(original_panel, text="Бумажный оригинал", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        for key, label in CARD_FIELDS[:8]:
            self.add_field(document_panel, key, label, 30)
        for key, label in CARD_FIELDS[8:]:
            self.add_field(original_panel, key, label, 24)
        ttk.Separator(original_panel).pack(fill="x", pady=10)
        ttk.Label(original_panel, text="Вложение", style="Section.TLabel").pack(anchor="w", pady=(0, 6))
        ttk.Button(original_panel, text="Выбрать PDF…", command=self.choose_pdf).pack(anchor="w")
        self.filename = ttk.Label(
            original_panel, text="PDF прикреплён" if self.pdf is not None else "PDF не выбран",
            wraplength=210, style="Muted.TLabel",
        )
        self.filename.pack(anchor="w", pady=6)
        return self.inputs["title"]

    def add_field(self, panel, key: str, label: str, width: int) -> None:
        labels = {
            "title": "Название (обязательно)", "kind": "Тип (выберите или впишите свой)",
            "number": "Номер на оригинале (необязательно)", "tags": "Теги через запятую",
        }
        ttk.Label(panel, text=labels.get(key, label)).pack(anchor="w")
        if key == "kind":
            entry = ttk.Combobox(panel, width=width, values=self.kinds)
        else:
            entry = ttk.Entry(panel, width=width)
        if self.card is not None:
            entry.insert(0, getattr(self.card, key))
        entry.pack(fill="x", pady=(2, 5))
        self.inputs[key] = entry

    def buttonbox(self):
        buttons = ttk.Frame(self, padding=(12, 0, 12, 12))
        ttk.Button(buttons, text="Сохранить", command=self.ok, style="Primary.TButton").pack(side="right", padx=4)
        ttk.Button(buttons, text="Отмена", command=self.cancel).pack(side="right", padx=4)
        self.bind("<Return>", self.ok)
        self.bind("<Escape>", self.cancel)
        buttons.pack(fill="x")

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
    configure_appearance(root)
    root.minsize(900, 620)
    root.geometry("1060x740")
    ArchiveWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
