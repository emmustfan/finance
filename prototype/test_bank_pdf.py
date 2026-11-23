import unittest
from unittest.mock import Mock, patch

from assets import Asset
from bank_pdf import _modulbank, _ozon, _tbank
from ledger import Ledger, Opening, prepare_statement_account, preview_import

RUB_ACCOUNT = "40817810000000000001"


class BankPdfTest(unittest.TestCase):
    def test_ozon_import_seeds_opening_and_rejects_replay_or_wrong_account(self):
        page = f"""Период выписки: 01.11.2026 - 30.11.2026
Номер банковского счёта: {RUB_ACCOUNT}
Входящий остаток: 1 000,00 ₽
01.11.2026 09:40:14 123456789
  01
Утреннее списание -20,00 ₽ -20,00 ₽
02.11.2026 15:36:05 987654321
Возврат +10,00 ₽ +10,00 ₽
Итого зачислений: +10,00 ₽
Итого списаний: -20,00 ₽
Исходящий остаток: 990,00 ₽
"""
        section, = _ozon([page])
        statement = section.statement("example.pdf", "dummy")
        account = Asset("Тестовый счёт", "account", "Семья", asset_number=1)
        ledger = prepare_statement_account(Ledger(), account, statement)
        self.assertEqual((ledger.opening(1).day, ledger.opening(1).amount), ("2026-10-31", 100_000))
        mapping = statement.suggested_columns()
        preview = preview_import(ledger, 1, statement, mapping)
        self.assertEqual(preview.rows[0].entry.external_id, "12345678901")
        saved = preview.apply(ledger, set())
        saved.validate((account,))
        self.assertEqual(tuple(entry.source for entry in saved.entries), ("pdf", "pdf"))
        self.assertEqual(saved.balance_on(1, "2026-11-30"), preview.balance.amount)
        self.assertEqual([row.state for row in preview_import(saved, 1, statement, mapping).rows],
                         ["duplicate", "duplicate"])
        with self.assertRaisesRegex(ValueError, "другому счёту"):
            prepare_statement_account(saved, Asset("Другой счёт", "account", "Семья", asset_number=2), statement)
        sums_changed = page.replace("Итого списаний: -20,00", "Итого списаний: -19,00")
        with self.assertRaisesRegex(ValueError, "не сходятся"):
            _ozon([sums_changed])[0].statement("bad.pdf", "dummy")

    def test_tbank_uses_posting_day_account_amount_and_masked_reference(self):
        section, = _tbank(["""Операции по счёту № 408178******0001
Баланс на 01.11.2026 1 200,00 ₽
Баланс на 30.11.2026 1 192,27 ₽
01.11.2026 23:59 02.11.2026 30,00 ₽ 7,73 ₽ Покупка
"""])
        statement = section.statement("example.pdf", "dummy")
        asset = Asset("Тестовый счёт", "account", "Семья", asset_number=1)
        existing = Ledger().set_opening(Opening(1, "2026-10-31", 120_000, "RUB", RUB_ACCOUNT))
        ledger = prepare_statement_account(existing, asset, statement)
        entry = preview_import(ledger, 1, statement, statement.suggested_columns()).rows[0].entry
        self.assertEqual((entry.day, entry.amount), ("2026-11-02", -773))
        self.assertEqual(ledger.opening(1).reference, RUB_ACCOUNT)

    def test_modulbank_keeps_sections_separate_and_detects_misaligned_columns(self):
        page = Mock()
        page.get_rotation.return_value = 90
        page.get_size.return_value = (842, 595)
        geometry = page.get_textpage.return_value
        sections = [f"""Дата: 01.11.2026 - 30.11.2026
БИК 000000001 Расчётный счёт № {RUB_ACCOUNT} (RUR)
Средства на начало периода (сальдо входящее): 1 000,00
Обороты Приход Расход
1 03.11.2026 Входящий платёж 2 500,00
2 04.11.2026 Исходящий платёж 1 050,00
Итого оборотов: 2 500,00 1 050,00
Средства на конец периода (сальдо исходящее): 2 450,00
""", """Окончание выписки""", f"""Дата: 01.11.2026 - 30.11.2026
БИК 000000001 Счёт-копилка № {int(RUB_ACCOUNT) + 1} (RUR)
Обороты Приход Расход
Итого оборотов: 0,00 0,00
Средства на конец периода (сальдо исходящее): 0,00
"""]
        geom = [(0, 773, 0, 774), (0, 822, 0, 823)]
        with patch("bank_pdf.pdfium.raw.FPDFText_GetCharIndexFromTextIndex", return_value=42):
            geometry.get_charbox.side_effect = geom
            incoming, empty = _modulbank([(page, text) for text in sections])
            first, second = (section.statement("example.pdf", "dummy") for section in (incoming, empty))
            self.assertEqual(len(first.rows), 2)
            self.assertEqual(first.closing_hint[1], 245_000)
            self.assertFalse(second.rows)
            self.assertNotEqual(first.digest, second.digest)
            geometry.get_charbox.side_effect = reversed(geom)
            with self.assertRaisesRegex(ValueError, "не сходятся"):
                _modulbank([(page, sections[0])])[0].statement("bad.pdf", "dummy")
