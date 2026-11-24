"""Local parsers for the text-layer layouts used by three Russian banks."""

import hashlib
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path

import pypdfium2 as pdfium

from assets import parse_amount
from ledger import MAX_ROWS, Statement

MAX_PDF_BYTES = 20 * 1024 * 1024
MAX_PAGES = 250
DAY = r"(?:[0-9]{2}\.[0-9]{2}\.[0-9]{4}|[0-9]{2}\.[0-9]{2}\.[0-9]{2})(?![0-9])"
TIME = r"[0-9]{2}:[0-9]{2}(?::[0-9]{2})?"
CASH = r"[+−-]?[ \t]*[0-9]+(?: [0-9]{3})*[.,][0-9]{2}"
RUBLES = re.compile(f"(?P<amount>{CASH})[ \t]*₽")
T_ROW = re.compile(rf"(?m)^[ \t]*(?P<event>{DAY})(?:[ \t]+{TIME})?[ \t]+(?P<day>{DAY})[ \t]*")
O_ROW = re.compile(rf"(?m)^[ \t]*(?P<day>{DAY})[ \t]+{TIME}[ \t]+(?P<id>[0-9]{{6,18}})(?![0-9])")
M_ROW = re.compile(rf"(?m)^[ \t]*(?P<number>[0-9]+)[ \t]+(?P<day>{DAY})[ \t]*(?P<text>[^\n]*?)\s*$")
M_REF = re.compile(
    r"(?im)^БИК[ \t]+[0-9]{9}[ \t]+(?P<label>[^\n]*?сч[её]т(?:-копилка)?)[ \t]*№[ \t]*"
    r"(?P<ref>[0-9]{20})[ \t]*\((?:RUR|RUB)\)"
)
M_TOTALS = re.compile(rf"(?im)^Итого оборотов:[ \t]*({CASH})[ \t]+({CASH})[ \t]*$")
PERIOD = re.compile(rf"(?im)^(?:Дата|Период выписки)[ \t]*:[ \t]*(?P<start>{DAY})[ \t]*[-–—][ \t]*(?P<end>{DAY})")
COLUMNS = ("Дата", "Сумма", "Описание", "ID операции", "Валюта", "Счёт", "Счёт контрагента")


def _date(value: str) -> str:
    parts = value.split(".")
    if len(parts[-1]) == 2:
        parts[-1] = "20" + parts[-1]
    return datetime.strptime(".".join(parts), "%d.%m.%Y").date().isoformat()


def _money(value: str) -> int:
    amount = parse_amount(re.sub(r"^([+−-])\s+", r"\1", value.strip()))
    if amount is None:
        raise ValueError("В выписке не читается сумма.")
    return amount


def _minor_text(value: int) -> str:
    units, digits = divmod(abs(value), 100)
    return f"{'-' if value < 0 else ''}{units}.{digits:02d}"


def _clean(text: str) -> str:
    return " ".join(text.split())


def _reference(text: str) -> str:
    match = re.search(r"(?<![0-9])[0-9]{20}(?![0-9])", text)
    return match[0] if match else ""


@dataclass
class Section:
    bank: str
    account: str
    start: str
    end: str
    opening: int | None
    closing: int | None = None
    declared_income: int | None = None
    declared_expenses: int | None = None
    entries: list[tuple[int, tuple[str, ...]]] = field(default_factory=list)
    account_label: str = ""

    def add(self, day: str, amount: int, text: str, *, identifier="", counterparty="") -> None:
        if not amount:
            return
        if len(self.entries) >= MAX_ROWS:
            raise ValueError("В одном счёте выписки поддерживается до 20 000 операций.")
        self.entries.append((len(self.entries) + 1, (
            day, _minor_text(amount), _clean(text) or "Операция по выписке", identifier,
            "RUB", self.account, counterparty,
        )))

    def statement(self, name: str, file_digest: str) -> Statement:
        if self.closing is None:
            raise ValueError(f"{self.bank}: не найден исходящий остаток по счёту.")
        opening = self.opening
        if opening is None:
            if self.entries or self.declared_income != 0 or self.declared_expenses != 0:
                raise ValueError(f"{self.bank}: не найден входящий остаток по счёту.")
            opening = self.closing
        amounts = [_money(values[1]) for _, values in self.entries]
        credited = sum(amount for amount in amounts if amount > 0)
        debited = -sum(amount for amount in amounts if amount < 0)
        if (
            opening + credited - debited != self.closing
            or (self.declared_income is not None and credited != self.declared_income)
            or (self.declared_expenses is not None and debited != self.declared_expenses)
        ):
            raise ValueError(
                f"{self.bank}: извлечённые операции не сходятся с остатками или оборотами PDF. "
                "Импорт этого файла остановлен."
            )
        if any(not self.start <= values[0] <= self.end for _, values in self.entries):
            raise ValueError(f"{self.bank}: в таблице есть операции вне периода выписки.")
        opening_day = (date.fromisoformat(self.start) - timedelta(days=1)).isoformat()
        digest = hashlib.sha256(f"{file_digest}:{self.account}".encode()).hexdigest()
        return Statement(name, digest, COLUMNS, tuple(self.entries), "pdf", self.bank,
                         (opening_day, opening, "RUB", self.account), (self.end, self.closing), self.account_label)


def _segments(text: str, pattern):
    rows = list(pattern.finditer(text))
    for index, row in enumerate(rows):
        stop = rows[index + 1].start() if index + 1 < len(rows) else len(text)
        yield row, text[row.end():stop]


def _matching_money(text, label):
    match = re.search(rf"(?im)^[ \t]*{label}[^\n]*?({CASH})[ \t]*₽?[ \t]*$", text)
    return _money(match[1]) if match else None


def _ozon(pages) -> list[Section]:
    first = pages[0]
    dates = PERIOD.search(first)
    account = re.search(r"(?im)^Номер[^\n]*сч[её]та:[ \t]*№?[ \t]*([0-9]{20})", first)
    opening = _matching_money(first, "Входящий остаток:")
    if not dates or not account or opening is None:
        raise ValueError("Озон Банк: не найдены период, счёт или входящий остаток.")
    summary = "\n".join(pages)
    closing = _matching_money(summary, "Исходящий остаток:")
    income = _matching_money(summary, "Итого зачислений")
    expenses = _matching_money(summary, "Итого списаний")
    if income is None or expenses is None:
        raise ValueError("Озон Банк: не найдены итоговые зачисления и списания.")
    section = Section("Озон Банк", account[1], _date(dates["start"]), _date(dates["end"]), opening, closing,
                      income, abs(expenses))
    for page in pages:
        for row, text in _segments(page, O_ROW):
            text = re.split(r"(?im)^[ \t]*(?:Итого зачислений|Итого списаний|Исходящий остаток):?", text)[0]
            amounts = list(RUBLES.finditer(text))
            if not amounts:
                raise ValueError("Озон Банк: не читается сумма строки.")
            identifier = row["id"]
            tail = re.match(r"^[ \t]*\n[ \t]*([0-9]{1,4})[ \t]*\n", text)
            description_start = 0
            if tail:
                identifier += tail[1]
                description_start = tail.end()
            amount = _money(amounts[-1]["amount"])
            if amount > 0 and not amounts[-1]["amount"].strip().startswith("+"):
                raise ValueError("Озон Банк: не читается направление операции.")
            description = text[description_start:amounts[0].start()]
            section.add(_date(row["day"]), amount, description, identifier=identifier,
                        counterparty=_reference(description))
    return [section]


def _tbank(pages) -> list[Section]:
    first = pages[0]
    balances = list(re.finditer(rf"(?im)^Баланс на[ \t]+({DAY})[ \t]+({CASH})[ \t]*₽", first))
    account = re.search(r"(?im)^Операции по[^\n]*?№[ \t]*([0-9]{6}\*+[0-9]{4}|[0-9]{20})", first)
    if len(balances) != 2 or not account:
        raise ValueError("Т-Банк: не найдены счёт и два остатка по периоду.")
    section = Section("Т-Банк", account[1], _date(balances[0][1]), _date(balances[1][1]),
                      _money(balances[0][2]), _money(balances[1][2]))
    for page in pages:
        for row, text in _segments(page, T_ROW):
            amounts = list(RUBLES.finditer(text))
            if len(amounts) != 2:
                raise ValueError("Т-Банк: не удалось выделить обе колонки сумм строки.")
            amount = _money(amounts[-1]["amount"])
            if amount > 0 and not amounts[-1]["amount"].strip().startswith("+"):
                amount = -amount
            description = text[:amounts[0].start()] + " " + text[amounts[-1].end():]
            section.add(_date(row["day"]), amount, description, counterparty=_reference(description))
    return [section]


def _modulbank(text_pages) -> list[Section]:
    active = None
    sections = []
    references = set()
    for page, text in text_pages:
        reference = M_REF.search(text)
        if reference:
            dates = PERIOD.search(text)
            opening = _matching_money(text, r"Средства на начало периода \(сальдо входящее\):")
            if not dates:
                raise ValueError("Модульбанк: не найден период выписки по счёту.")
            account = reference["ref"]
            if account in references:
                raise ValueError("Модульбанк: в PDF несколько разделов одного счёта.")
            references.add(account)
            active = Section("Модульбанк", account, _date(dates["start"]), _date(dates["end"]), opening,
                             account_label=_clean(reference["label"]))
            sections.append(active)
        elif PERIOD.search(text):
            raise ValueError("Модульбанк: не удалось определить рублёвый счёт на странице.")
        if active is None:
            raise ValueError("Модульбанк: не найдено начало первого счёта.")
        closing = _matching_money(text, r"Средства на конец периода \(сальдо исходящее\):")
        if closing is not None:
            active.closing = closing
            totals = M_TOTALS.search(text)
            if not totals:
                raise ValueError("Модульбанк: не найдены итоговые обороты счёта.")
            active.declared_income = _money(totals[1])
            active.declared_expenses = abs(_money(totals[2]))
        rows = list(M_ROW.finditer(text))
        if not rows:
            continue
        if "Обороты Приход Расход" not in text or page.get_rotation() != 90:
            raise ValueError("Модульбанк: таблица оборотов имеет другой формат.")
        tp = page.get_textpage()
        try:
            for row in rows:
                body = row["text"]
                amount = re.search(rf"(?P<amount>{CASH})[ \t]*$", body)
                if not amount:
                    raise ValueError("Модульбанк: в строке не найдена сумма.")
                pos = row.start("text") + amount.end("amount") - 1
                idx = pdfium.raw.FPDFText_GetCharIndexFromTextIndex(tp.raw, pos)
                if idx < 0:
                    raise ValueError("Модульбанк: не удалось найти сумму в таблице.")
                # This landscape template uses the native vertical axis for its displayed columns.
                _, column_x, _, _ = tp.get_charbox(idx)
                width, _ = page.get_size()
                fraction = column_x / width
                if not 0.86 <= fraction <= 0.99:
                    raise ValueError("Модульбанк: не удалось определить приход или расход.")
                signed = _money(amount["amount"]) * (1 if fraction < 0.925 else -1)
                tail = body[:amount.start("amount")]
                account = _reference(tail)
                purpose = tail.split(account, 1)[-1] if account else tail
                active.add(_date(row["day"]), signed, purpose, counterparty=account)
        finally:
            tp.close()
    return sections


def read_pdf_statements(path: Path) -> tuple[Statement, ...]:
    with path.open("rb") as stream:
        data = stream.read(MAX_PDF_BYTES + 1)
    if len(data) > MAX_PDF_BYTES:
        raise ValueError("Один PDF выписки может занимать до 20 МиБ.")
    try:
        with pdfium.PdfDocument(data) as document:
            if not 0 < len(document) <= MAX_PAGES:
                raise ValueError("В PDF ожидается от 1 до 250 страниц.")
            pages = []
            for index in range(len(document)):
                page = document[index]
                tp = page.get_textpage()
                try:
                    text = tp.get_text_range().translate({13: 32, 160: 32, 8239: 32})
                    pages.append((page, text))
                finally:
                    tp.close()
            first = pages[0][1]
            if "Период выписки:" in first and "Входящий остаток:" in first and "Назначение платежа" in first:
                sections = _ozon([text for _, text in pages])
            elif "Выписка по договору" in first and "обработки" in first and "в валюте счёта" in first:
                sections = _tbank([text for _, text in pages])
            elif M_REF.search(first) and "Обороты Приход Расход" in first:
                sections = _modulbank(pages)
            else:
                raise ValueError("Формат PDF не распознан. Поддерживаются проверенные текстовые выписки трёх банков.")
            digest = hashlib.sha256(data).hexdigest()
            statements = []
            for section in sections:
                title = path.name if len(sections) == 1 else f"{path.name} · счёт •{section.account[-4:]}"
                statements.append(section.statement(title, digest))
            return tuple(statements)
    except pdfium.PdfiumError as exc:
        raise ValueError("PDF не удалось открыть. Выберите обычный PDF без пароля.") from exc
