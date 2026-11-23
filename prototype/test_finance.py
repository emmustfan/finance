import csv
import io
import json
import tempfile
import unittest
from dataclasses import replace
from datetime import date
from pathlib import Path

from assets import Asset
from documents import Catalog
from finance import activity, export_text, flow_breakdown, flow_totals
from funds import Fund, Funds, goal_gap
from ledger import Entry
from review import Recurring, Rule
from vault import copy_verified


class FinanceTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.path = Path(directory.name) / "finance.fvault"
        self.password = "fictional finance password"

    def test_new_wallet_allows_first_manual_operation_and_backdating_zero(self):
        catalog = Catalog.create(self.path, self.password, assets=(Asset("Карта", "account", "Я"),))
        self.assertIsNone(catalog.ledger.opening(1))
        catalog = catalog.record_manual(Entry(1, "2026-11-23", -25_00, "Обед"), "Кафе")
        self.assertEqual(catalog.ledger.latest_balance(1), (-25_00, "2026-11-23"))
        catalog = catalog.record_manual(Entry(1, "2026-11-20", 100_00, "Подработка"), "Подработка")
        self.assertEqual(catalog.ledger.opening(1).day, "2026-11-19")
        self.assertEqual(catalog.ledger.latest_balance(1)[0], 75_00)
        updated = catalog.record_manual(Entry(1, "2026-11-23", -20_00, "Обед"), "Кафе", number=1)
        self.assertEqual(updated.ledger.latest_balance(1)[0], 80_00)
        self.assertEqual(len(updated.ledger.entries), 2)

    def test_opening_from_previous_snapshot_or_explicit_amount(self):
        catalog = Catalog.create(self.path, self.password, assets=(
            Asset("Наличные", "account", "Я", amount=20_000_00, as_of="2026-11-23"),
        ))
        with self.assertRaisesRegex(ValueError, "перед операцией"):
            catalog.record_manual(Entry(1, "2026-11-23", -100_00, "Расход"))
        updated = catalog.record_manual(Entry(1, "2026-11-23", -100_00, "Расход"), prior=20_000_00)
        self.assertEqual(updated.ledger.latest_balance(1)[0], 19_900_00)

    def test_directory_rename_updates_rules_and_assignment_and_archive_hides_suggestion(self):
        catalog = Catalog.create(self.path, self.password, assets=(Asset("Карта", "account", "Я"),))
        catalog = catalog.record_manual(Entry(1, "2026-11-23", -25_00, "Кофе"), "Кафе")
        review = catalog.review.learn(Rule("description", "Кофе", -1, "Кафе")).save_recurring(
            Recurring("Платёж", 1, "кофе", "Кафе", 25_00, 1)
        )
        catalog = catalog.save_review(review)
        directory, review = catalog.directory.edit(catalog.ledger, catalog.review, -1, "Кофейни", previous="Кафе")
        catalog = catalog.save_directory(directory, review)
        self.assertEqual(review.assignments[0].category, "Кофейни")
        self.assertEqual(review.rules[0].category, "Кофейни")
        self.assertEqual(review.recurring[0].category, "Кофейни")
        self.assertEqual(directory.suggestion(review, catalog.ledger.entries[0]).category, "Кофейни")
        directory, review = directory.edit(catalog.ledger, review, -1, "Кофейни", previous="Кофейни", archived=True)
        catalog = catalog.save_directory(directory, review)
        self.assertIsNone(directory.suggestion(review, catalog.ledger.entries[0]))
        with self.assertRaisesRegex(ValueError, "в архиве"):
            catalog.record_manual(Entry(1, "2026-11-24", -5_00, "Кофе"), "Кафе")
        backup = self.path.with_name("backup.fvault")
        catalog.backup(backup)
        restored = self.path.with_name("restored.fvault")
        copy_verified(backup, restored, self.password)
        self.assertEqual(Catalog.open(restored, self.password).directory, directory)

    def test_transfer_fee_in_report_and_export_privacy(self):
        catalog = Catalog.create(self.path, self.password, assets=(
            Asset("Персональная карта", "account", "Тест"), Asset("Наличные", "account", "Тест"),
        ))
        catalog = catalog.record_manual(Entry(1, "2026-11-20", 1000_00, "Секретное описание"), "Зарплата")
        catalog = catalog.record_transfer(Entry(1, "2026-11-21", -105_00, "Между счетами"),
                                          Entry(2, "2026-11-21", 100_00, "Между счетами"))
        catalog = catalog.record_manual(Entry(2, "2026-11-22", -25_00, "=HYPERLINK()"), "Кафе")
        catalog = catalog.record_manual(Entry(1, "2026-12-01", -30_00, "Вне периода"), "Кафе")
        rows = activity(catalog.ledger, catalog.review, "2026-11-01", "2026-11-30")
        self.assertEqual(flow_totals(rows), (1000_00, 30_00))
        self.assertEqual(flow_breakdown(rows)[("RUB", "expense", "Комиссии банка")], 5_00)
        encoded = export_text(rows, "2026-11-01", "2026-11-30", kind="json")
        output = json.loads(encoded)
        self.assertEqual(len(output["operations"]), 4)
        self.assertEqual(output["monthly_totals"][0]["net_minor"], 970_00)
        for hidden in ("Секретное описание", "Персональная карта", "Вне периода"):
            self.assertNotIn(hidden, encoded)
        exported = export_text(rows, "2026-11-01", "2026-11-30", kind="csv", details=True)
        details = list(csv.DictReader(io.StringIO(exported)))
        self.assertEqual(details[-1]["description"], "'=HYPERLINK()")

    def test_goal_planning_and_investments_survive_reopen(self):
        fund = Fund("Образование", "Цель", "RUB", 120_000_00, 1, "2027-01-18")
        self.assertEqual(goal_gap(fund, 30_000_00, date(2026, 11, 23)), (90_000_00, 30_000_00))
        holding = Asset("Акция", "investment", "Я", amount=25_000_00, as_of="2026-11-22",
                        ticker="TEST", quantity="10", purchase_cost=20_000_00)
        catalog = Catalog.create(self.path, self.password, assets=(holding,))
        catalog = catalog.save_funds(catalog.funds.save_fund(replace(fund, number=0)))
        restored = Catalog.open(self.path, self.password)
        self.assertEqual(restored.assets, catalog.assets)
        self.assertEqual(restored.funds.funds, (fund,))

    def test_manual_expense_uses_reserve_in_same_write(self):
        funds = Funds(funds=(Fund("Путешествие", "Цель", "RUB", 100_000_00, 1),))
        funds = funds.apply("initial", ((1, 70_000_00),))
        catalog = Catalog.create(self.path, self.password,
                                 assets=(Asset("Кошелёк", "account", "Я"),), funds=funds)
        catalog = catalog.record_manual(
            Entry(1, "2026-11-23", -7_000_00, "Билеты"), "Путешествия",
            prior=120_000_00, fund=1,
        )
        self.assertEqual(catalog.liquid_totals()["RUB"], 113_000_00)
        self.assertEqual(catalog.funds.reserved()["RUB"], 63_000_00)
        self.assertEqual(catalog.funds.movements[-1].operation, 1)
        with self.assertRaisesRegex(ValueError, "недостаточно"):
            catalog.record_manual(Entry(1, "2026-11-25", -65_000_00, "Новый билет"), fund=1)
        self.assertEqual(len(Catalog.open(self.path, self.password).ledger.entries), 1)


if __name__ == "__main__":
    unittest.main()
