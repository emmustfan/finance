import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assets import Asset
from documents import Card, Catalog
from funds import Fund, Funds, Line, Template
from ledger import Entry, Ledger, Opening, preview_import, read_statement
from review import Assignment, Recurring, Review, Rule, TAX_PAYMENT
from sample import sample_pdf
from vault import copy_verified

PASSWORD = "fictional household passphrase"
EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def import_example(catalog, directory, name, account):
    statement = read_statement(EXAMPLES / directory / name)
    preview = preview_import(catalog.ledger, account, statement, statement.suggested_columns())
    assert all(row.state == "new" for row in preview.rows)
    return catalog.save_ledger(preview.apply(catalog.ledger, set()))


class PlanningTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_stage_four_and_tax_reserve_with_two_bank_exports(self):
        path = self.root / "planning.fvault"
        catalog = Catalog.create(
            path, PASSWORD, scope="family",
            assets=(Asset("Т-Банк", "account", "Семья"), Asset("Ozon", "account", "Семья"),
                    Asset("Модульбанк", "account", "Владелец", kind="Счёт ИП")),
            ledger=Ledger(openings=(
                Opening(1, "2026-10-31", 3_000_000, "RUB", "DEMO-T-MAIN"),
                Opening(2, "2026-10-31", 500_000, "RUB", "DEMO-OZON"),
                Opening(3, "2026-10-31", 0, "RUB", "DEMO-IP"),
            )),
        )
        catalog = import_example(catalog, "review", "01-main.csv", 1)
        self.assertEqual(catalog.review.transfer_candidates(catalog.ledger, 2), ())
        catalog = catalog.save_review(catalog.review.wait_for_transfer(catalog.ledger, 2))
        self.assertEqual(catalog.review.waiting, (2,))
        catalog = import_example(catalog, "review", "02-ozon.csv", 2)
        candidate, = catalog.review.transfer_candidates(catalog.ledger, 2)
        self.assertEqual(candidate[:2], (4, 3_000))
        self.assertIn("Реквизит совпал", candidate[2])
        amounts = catalog.liquid_totals()
        review = catalog.review.link_transfer(catalog.ledger, 2, 4)
        self.assertEqual(review.link_transfer(catalog.ledger, 4, 2), review)
        self.assertEqual(review.waiting, ())
        review = review.save_recurring(
            Recurring("Аренда квартиры", 1, "аренда", "Жильё → Аренда", 700_000, 5)
        )
        self.assertEqual(review.suggestion(catalog.ledger.entries[2]).difference, 50_000)
        self.assertEqual(review.calendar(catalog.ledger, "2026-12")[0][2].day, "2026-12-05")
        review = review.learn(Rule("recipient", "арендодатель", -1, "Аренда"))
        no_reg = Review(rules=review.rules)
        self.assertIn("Получатель", no_reg.suggestion(catalog.ledger.entries[0]).reason)
        catalog = catalog.save_review(review.assign((Assignment(1, "Жильё → Аренда"),)))
        self.assertEqual(catalog.liquid_totals(), amounts)
        outcome = review.report(catalog.ledger, "2026-11-01", "2026-11-30")
        self.assertEqual(outcome[("RUB", "Расходы", "Комиссии банка")], 3_000)
        self.assertNotIn(("RUB", "Доходы", "Без подтверждённой статьи"), outcome)
        catalog = import_example(catalog, "review", "03-ip.csv", 3)
        catalog = catalog.save_review(catalog.review.assign((
            Assignment(5, "Выручка ИП", True), Assignment(6, TAX_PAYMENT),
        )))
        revenue, reserve = catalog.review.tax_reserve(catalog.ledger, "2026-11-01", "2026-11-30")
        self.assertEqual((revenue, reserve), (5_000_000, 300_000))
        self.assertEqual(catalog.review.report(catalog.ledger, "2026-11-01", "2026-11-30")
                         [("RUB", "Расходы", TAX_PAYMENT)], 250_000)

    def test_stage_five_funds_exact_cash_and_offline_archive_restore(self):
        path = self.root / "family.fvault"
        pdf = sample_pdf()
        funds = Funds(funds=(Fund("Резерв", "Подушка", "RUB", 10_000_000, 1),))
        funds = funds.apply("opening", ((1, 500_000),))
        funds = funds.set_template(Template("RUB", (Line(1, "fixed", 6_500_000),)))
        catalog = Catalog.create(
            path, PASSWORD, scope="family", cards=(Card("Договор", pdf=pdf),),
            assets=(Asset("Счёт семьи", "account", "Семья", document_numbers=(1,)),),
            ledger=Ledger(openings=(Opening(1, "2026-10-31", 2_000_000, "RUB", "DEMO-FUNDS"),)),
            funds=funds,
        )
        catalog = import_example(catalog, "funds", "01-income.csv", 1)
        cash = catalog.liquid_totals()["RUB"]
        plan = catalog.funds.plan("RUB", catalog.ledger.entries[0].amount, cash - 500_000)
        self.assertEqual(plan, ((1, 6_500_000),))
        allocated = catalog.funds.apply("income:1", plan, 1)
        self.assertEqual(allocated.apply("income:1", plan, 1), allocated)
        catalog = catalog.save_funds(allocated)
        self.assertEqual((cash, allocated.reserved()["RUB"], cash - allocated.reserved()["RUB"]),
                         (12_000_000, 7_000_000, 5_000_000))
        catalog = import_example(catalog, "funds", "02-expense.csv", 1)
        paid = catalog.funds.apply("expense:2", ((1, -700_000),), 2)
        self.assertEqual(paid.apply("expense:2", ((1, -700_000),), 2), paid)
        catalog = catalog.save_funds(paid)
        after = catalog.liquid_totals()["RUB"]
        self.assertEqual((after, paid.reserved()["RUB"], after - paid.reserved()["RUB"]),
                         (11_300_000, 6_300_000, 5_000_000))
        copy = self.root / "backup.fvault"
        catalog.backup(copy)
        self.assertEqual(stat.S_IMODE(copy.stat().st_mode), 0o600)
        path.unlink()
        copy_verified(copy, path, PASSWORD)
        restored = Catalog.open(path, PASSWORD)
        self.assertEqual((restored.scope, restored.cards[0].pdf, restored.funds, restored.ledger),
                         ("family", pdf, catalog.funds, catalog.ledger))

    def test_template_percent_goal_and_remainder(self):
        funds = Funds(funds=tuple(Fund(name, "Цель", "RUB", 3000, number)
                                 for number, name in enumerate(("Фикс", "%", "Цель", "Остаток"), 1)))
        funds = funds.set_template(Template("RUB", (
            Line(1, "fixed", 500), Line(2, "percent", 2500), Line(3, "goal"), Line(4, "remainder"),
        )))
        self.assertEqual(funds.plan("RUB", 10000, 9000), ((1, 500), (2, 2500), (3, 3000), (4, 3000)))

    def test_explicit_archive_scope_and_family_group_file_modes(self):
        shared = self.root / "shared"
        shared.mkdir()
        shared.chmod(0o770)
        family_path = shared / "family.fvault"
        family = Catalog.create(family_path, PASSWORD, scope="family")
        self.assertEqual(stat.S_IMODE(family_path.stat().st_mode), 0o660)
        family.add(Card("Документ", folder="1"))
        self.assertEqual(stat.S_IMODE(family_path.stat().st_mode), 0o660)
        lock = shared / ".family.fvault.lock"
        self.assertEqual(stat.S_IMODE(lock.stat().st_mode), 0o660)
        personal = Catalog.create(shared / "personal.fvault", PASSWORD, scope="business")
        self.assertEqual(stat.S_IMODE(personal.path.stat().st_mode), 0o600)
        with patch("documents.os.getuid", return_value=os.getuid() + 1):
            self.assertTrue(Catalog.open(family_path, PASSWORD).editable)
            with self.assertRaisesRegex(ValueError, "владельца"):
                Catalog.open(personal.path, PASSWORD)
