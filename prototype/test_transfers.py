from dataclasses import replace
import unittest

from assets import Asset
from finance import activity, flow_totals
from ledger import Entry, Ledger, Opening, reference_matches
from review import Assignment, Review
from transfers import matching_account, suggested_transfers, transfer_route

FIRST = "40817810000000009001"
SECOND = "40817810000000009002"
THIRD = "40817810000000009003"


class TransferMatchingTest(unittest.TestCase):
    def setUp(self):
        self.assets = (
            Asset("Основной", "account", "Семья", organization="Модульбанк", asset_number=1),
            Asset("Копилка", "account", "Семья", organization="Модульбанк", asset_number=2),
            Asset("Дебетовая карта", "account", "Семья", organization="Т-Банк", asset_number=3),
        )
        self.ledger = Ledger(openings=tuple(Opening(number, "2026-10-31", 100_000, "RUB", ref)
                                            for number, ref in enumerate((FIRST, SECOND, THIRD), 1)))

    def test_reciprocal_requisites_prefer_same_day_and_keep_direction(self):
        ledger = replace(self.ledger, entries=(
            Entry(1, "2026-11-04", -30_000, "Перевод", counterparty=SECOND, source="pdf"),
            Entry(2, "2026-11-05", 30_000, "Зачисление", counterparty=FIRST, source="pdf"),
            Entry(2, "2026-11-04", 30_000, "Зачисление", counterparty=FIRST, source="pdf"),
            Entry(2, "2026-11-05", -10_000, "Уплата налога", counterparty="40102810000000000012", source="pdf"),
        ))
        match, = suggested_transfers(ledger, Review(), self.assets)
        self.assertEqual((match.debit, match.credit, match.automatic), (1, 3, True))
        self.assertFalse(suggested_transfers(ledger, Review(), self.assets, touched={2}))
        review = Review().link_transfer(ledger, match.credit, match.debit)
        rows = {row.number: row for row in activity(ledger, review, "2026-11-01", "2026-11-30")}
        for number in (1, 3):
            row = rows[number]
            self.assertEqual(row.kind, "transfer")
            self.assertEqual(transfer_route(row.amount, self.assets[row.account - 1].title,
                                           self.assets[row.transfer_account - 1].title), "Основной → Копилка")
        self.assertEqual(flow_totals(tuple(rows.values())), (30_000, 10_000))
        self.assertFalse(suggested_transfers(ledger, review.unlink(1), self.assets))

    def test_duplicate_pairs_and_masked_requisites_require_a_decision(self):
        debit = Entry(1, "2026-11-01", -25_000, "Списание", counterparty=SECOND)
        credit = Entry(2, "2026-11-01", 25_000, "Зачисление", counterparty=FIRST)
        ledger = replace(self.ledger, entries=(debit, credit, credit))
        self.assertEqual(suggested_transfers(ledger, Review(), self.assets), ())
        masked = replace(ledger, entries=(replace(debit, counterparty="408178******9002"), credit))
        match, = suggested_transfers(masked, Review(), self.assets)
        self.assertFalse(match.automatic)
        assigned = Review().assign((Assignment(2, "Зарплата"),))
        match, = suggested_transfers(replace(ledger, entries=(debit, credit)), assigned, self.assets)
        self.assertFalse(match.automatic)
        self.assertEqual(suggested_transfers(masked, Review(), self.assets, excluded={1}), ())
        self.assertEqual(matching_account("40817810 000000009002", "RUB", ledger.openings), 2)
        self.assertTrue(reference_matches("408178******9002", SECOND))

    def test_cross_bank_text_is_only_a_suggestion_and_needs_an_own_transfer_clue(self):
        debit = Entry(1, "2026-11-04", -50_000, "Себе на карту в другой банк",
                      counterparty="30232810000000000012")
        from_bank = Entry(3, "2026-11-04", 50_000, "Пополнение. МОДУЛЬБАНК.")
        salary = Entry(3, "2026-11-04", 50_000, "Зарплата")
        ledger = replace(self.ledger, entries=(debit, from_bank, salary))
        match, = suggested_transfers(ledger, Review(), self.assets, touched={2, 3})
        self.assertEqual((match.debit, match.credit, match.automatic), (1, 2, False))
        external = replace(ledger, entries=(replace(debit, description="Оплата контрагенту"), from_bank, salary))
        self.assertEqual(suggested_transfers(external, Review(), self.assets), ())
        self.assertEqual(suggested_transfers(ledger, Review().wait_for_transfer(ledger, 1, 2), self.assets), ())
        ordinary = Review().keep_ordinary((1,))
        self.assertEqual(Review.from_payload(ordinary.to_payload()), ordinary)
        self.assertFalse(suggested_transfers(ledger, ordinary, self.assets))
