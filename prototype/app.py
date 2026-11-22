import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import pypdfium2 as pdfium
from cryptography.exceptions import InvalidTag
from PIL import ImageTk

from sample import sample_pdf
from vault import copy_verified, create, open_pdf

VAULT_TYPES = [("Архив", "*.fvault")]


class ArchiveWindow:
    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.pdf: bytes | None = None
        self.document = None
        self.image = None
        self.page = 0

        root.title("Семейный архив")
        toolbar = ttk.Frame(root, padding=8)
        toolbar.pack(fill="x")
        for label, action in (
            ("Создать тестовый архив", self.create_archive),
            ("Открыть архив", self.open_archive),
            ("Создать копию", lambda: self.copy_archive(False)),
            ("Восстановить из копии", lambda: self.copy_archive(True)),
        ):
            ttk.Button(toolbar, text=label, command=action).pack(side="left", padx=3)

        controls = ttk.Frame(root, padding=8)
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
        self.preview = ttk.Label(root, anchor="center")
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

    def create_archive(self) -> None:
        path = self.save_path("Создать тестовый архив")
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
            create(path, sample_pdf(), password)
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
        document = None
        try:
            pdf = open_pdf(path, password)
            document = pdfium.PdfDocument(pdf)
            image = self.render(document, 0)
        except (OSError, ValueError, InvalidTag, pdfium.PdfiumError, tk.TclError) as exc:
            if document is not None:
                document.close()
            self.error(exc)
            return
        if self.document is not None:
            self.document.close()
        self.document, self.pdf = document, pdf
        self.set_page(image, 0)
        self.print_button.configure(state="normal")

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
                ["/usr/bin/lp", "-t", "Family archive test", "-"],
                input=self.pdf, capture_output=True, timeout=30, check=False,
            )
            if result.returncode:
                raise RuntimeError(result.stderr.decode(errors="replace").strip() or "Ошибка CUPS.")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            self.error(exc)
            return
        messagebox.showinfo("Печать", "Задание передано в очередь печати.", parent=self.root)


def main() -> None:
    root = tk.Tk()
    root.minsize(710, 690)
    ArchiveWindow(root)
    root.mainloop()


if __name__ == "__main__":
    main()
