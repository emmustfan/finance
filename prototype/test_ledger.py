import tempfile
import unittest
from pathlib import Path

from assets import Asset
from documents import Card, Catalog
from ledger import Entry, Ledger, Opening, preview_import, read_statement
from vault import copy_verified

PASSWORD = "fictional ledger password"


class LedgerTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.opening = Ledger(openings=(
            Opening(1, "2026-10-31", 100_00, "RUB", "FIRST"),
            Opening(2, "2026-10-31", 200_00, "RUB", "SECOND"),
        ))

    def preview(self, ledger, name, rows, account=1):
        path = self.root / name
        path.write_text(rows, encoding="utf-8")
        statement = read_statement(path)
        return preview_import(ledger, account, statement, statement.suggested_columns())

    def test_identical_purchases_overlap_and_exact_replay(self):
        header = "Date;Amount;Description;Balance\n"
        same_purchase = "01.11.2026;-5,00;Coffee;\n"
        first = self.preview(self.opening, "first.csv", header + same_purchase * 2)
        imported = first.apply(self.opening, set())
        self.assertEqual(len(imported.entries), 2)
        self.assertEqual(imported.balance_on(1, "2026-11-01"), 90_00)
        self.assertEqual(first.apply(imported, set()), imported)

        overlapping = self.preview(
            imported, "overlap.csv",
            header + same_purchase * 2 + "01.11.2026;-5,00;Coffee;85,00\n",
        )
        self.assertEqual([row.state for row in overlapping.rows], ["review", "review", "new"])
        updated = overlapping.apply(imported, set())
        self.assertEqual(len(updated.entries), 3)
        self.assertEqual(updated.balance_on(1, "2026-11-01"), overlapping.balance.amount)

        partial = self.preview(updated, "partial.csv", header + "01.11.2026;-5,00;Coffee;80,00\n")
        confirmed = partial.apply(updated, {partial.rows[0].line})
        self.assertEqual(len(confirmed.entries), 4)
        self.assertEqual(confirmed.balance_on(1, "2026-11-01"), 80_00)

    def test_bank_id_is_per_account_and_conflicts_need_attention(self):
        header = "Дата;Сумма;Описание;ID операции;Валюта\n"
        row = "01.11.2026;-12,10;Покупка;BANK-ID;RUB\n"
        statement = self.preview(self.opening, "with-id.csv", header + row + row)
        self.assertEqual([entry.state for entry in statement.rows], ["new", "duplicate"])
        imported = statement.apply(self.opening, set())
        second_account = self.preview(imported, "other-account.csv", header + row, account=2)
        imported = second_account.apply(imported, set())
        self.assertEqual(len(imported.entries), 2)
        conflict = self.preview(
            imported, "conflict.csv",
            header + "01.11.2026;-7,50;Покупка;BANK-ID;RUB\n"
            "01.11.2026;-7,50;Покупка;UNKNOWN;USD\n",
        )
        self.assertEqual([entry.state for entry in conflict.rows], ["error", "error"])

    def test_operations_preserve_archive_and_reconcile_as_of_selected_day(self):
        path = self.root / "archive.fvault"
        catalog = Catalog.create(
            path, PASSWORD, cards=(Card("Договор", folder="01"),),
            assets=(Asset("Основной счёт", "account", "Семья"),
                    Asset("Вклад", "account", "Семья")),
            ledger=self.opening,
        )
        catalog = catalog.save_ledger(catalog.ledger.add_manual(
            Entry(1, "2026-11-01", -950, "Покупка")
        ))
        statement = self.preview(
            catalog.ledger, "statement.csv", "Date;Amount;Description;Account;Balance\n"
            "02.11.2026;40,50;Поступление;FIRST;130,99\n",
        )
        saved = catalog.save_ledger(statement.apply(catalog.ledger, set()))
        self.assertEqual(saved.ledger.balance_on(1, "2026-11-01"), 90_50)
        self.assertEqual(saved.recorded_value(saved.assets[0]).amount, 131_00)
        self.assertEqual(saved.ledger.balance_on(1, "2026-11-02") - statement.balance.amount, 1)
        with self.assertRaisesRegex(ValueError, "начального остатка"):
            saved.ledger.balance_on(1, "2026-10-30")
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            catalog.save_ledger(catalog.ledger)
        saved = saved.set_tags(0, "Проверено")
        backup = self.root / "backup.fvault"
        saved.backup(backup)
        path.unlink()
        copy_verified(backup, path, PASSWORD)
        restored = Catalog.open(path, PASSWORD)
        self.assertEqual(restored.ledger, saved.ledger)
        self.assertEqual(restored.cards[0].tags, "Проверено")

    def test_different_account_requisites_and_opening_day_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "отличается"):
            self.preview(self.opening, "wrong.csv", "Date;Amount;Description;Account\n"
                         "01.11.2026;-5;Coffee;SECOND\n")
        result = self.preview(self.opening, "old.csv", "Date;Amount;Description\n31.10.2026;-5;Coffee\n")
        self.assertEqual(result.rows[0].state, "error")


if __name__ == "__main__":
    unittest.main()
