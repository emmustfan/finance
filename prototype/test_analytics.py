import unittest
from datetime import date

from analytics import MAX_BUCKETS, account_history, calendar_buckets, debt_history, flow_series, fund_history
from assets import Asset
from debts import Debt, Debts, Repayment
from finance import activity, flow_breakdown
from funds import Fund, Funds, Movement
from ledger import Entry, Ledger, Opening
from review import Assignment, Review, Transfer


def on(day):
    return date.fromisoformat(day)


class AnalyticsTest(unittest.TestCase):
    def test_flow_buckets_match_report_totals_and_wallet_filters(self):
        ledger = Ledger(
            openings=(Opening(1, "2026-10-01", 0, "RUB"), Opening(2, "2026-10-01", 0, "RUB"),
                      Opening(3, "2026-10-01", 0, "USD")),
            entries=(
                Entry(1, "2026-10-30", 100_000, "Зарплата"),
                Entry(1, "2026-10-31", -3500, "Перевод"),
                Entry(2, "2026-11-01", 3000, "Перевод"),
                Entry(2, "2026-11-01", -1200, "Такси"),
                Entry(2, "2026-11-06", 9000, "Подработка"),
                Entry(3, "2026-11-06", -1100, "Такси"),
                Entry(1, "2026-11-08", -5000, "Кафе"),
                Entry(1, "2026-12-01", -5000, "Кафе"),
            ),
        )
        review = Review(transfers=(Transfer(2, 3, 500),), assignments=(
            Assignment(1, "Зарплата"), Assignment(4, "Транспорт"), Assignment(5, "Подработка"),
        )).tag_operations({2: ("Поездка",), 4: ("Поездка",), 6: ("Поездка",)})
        first, last = on("2026-10-30"), on("2026-11-08")
        mode, buckets = calendar_buckets(first, last, "month")
        self.assertEqual(mode, "month")
        self.assertEqual([(bucket.start, bucket.end) for bucket in buckets], [
            (first, on("2026-10-31")), (on("2026-11-01"), last),
        ])
        rows = activity(ledger, review, first.isoformat(), last.isoformat(), "RUB")
        totals, parts = flow_series(rows, buckets, "expense")
        self.assertEqual(totals, (500, 6200))
        self.assertEqual({item.group: item.total for item in parts},
                         {key[2]: value for key, value in flow_breakdown(rows).items() if key[1] == "expense"})
        income, sources = flow_series(rows, buckets, "income")
        self.assertEqual(income, (100_000, 9000))
        self.assertEqual([source.group for source in sources], ["Зарплата", "Подработка"])
        tagged = activity(ledger, review, first.isoformat(), last.isoformat(), "RUB", tag="поездка")
        values, groups = flow_series(tagged, buckets, "expense", by_wallet=True, account=2)
        self.assertEqual(values, (0, 1200))
        self.assertEqual([item.group for item in groups], [2])

    def test_calendar_keeps_partial_weeks_and_all_dates_when_coarsening(self):
        first, last = on("2026-11-03"), on("2026-11-10")
        mode, buckets = calendar_buckets(first, last, "week")
        self.assertEqual(mode, "week")
        self.assertEqual([(bucket.start, bucket.end) for bucket in buckets], [
            (first, on("2026-11-08")), (on("2026-11-09"), last),
        ])
        first, last = on("2021-01-01"), on("2026-12-31")
        mode, buckets = calendar_buckets(first, last, "day")
        self.assertNotEqual(mode, "day")
        self.assertLessEqual(len(buckets), MAX_BUCKETS)
        self.assertEqual((buckets[0].start, buckets[-1].end), (first, last))
        self.assertTrue(all((right.start - left.end).days == 1 for left, right in zip(buckets, buckets[1:])))
        mode, buckets = calendar_buckets(date.min, date.max, "year")
        self.assertEqual(mode, "year")
        self.assertLessEqual(len(buckets), MAX_BUCKETS)
        self.assertEqual((buckets[0].start, buckets[-1].end), (date.min, date.max))

    def test_debt_uses_principal_and_shows_only_posted_dates(self):
        loans = Debts((
            Debt("Кредит", "borrowed", "RUB", "2026-09-01", 100_000, number=1),
            Debt("Сосед", "lent", "RUB", "2026-10-15", 5000, number=2),
        ), (Repayment(1, 1, 20_000), Repayment(1, 2, 0), Repayment(1, 3, 80_000)))
        ledger = Ledger(entries=(
            Entry(1, "2026-10-03", -22_000, "Возврат"),
            Entry(1, "2026-10-20", -1000, "Проценты"),
            Entry(1, "2026-11-02", -84_000, "Последний платёж"),
        ))
        first, last = on("2026-10-01"), on("2026-10-31")
        history = debt_history(loans, ledger, {1}, first, last)
        self.assertEqual(history.opening, 100_000)
        self.assertEqual(history.points, ((first, 100_000), (on("2026-10-03"), 80_000), (last, 80_000)))
        self.assertEqual(debt_history(loans, ledger, {2}, first, last).points[0], (on("2026-10-15"), 5000))
        self.assertEqual(debt_history(loans, ledger, {1}, on("2026-11-01"), on("2026-11-04")).points[-1][1], 0)

    def test_savings_and_legacy_manual_movements_keep_exact_balances(self):
        ledger = Ledger(
            openings=(Opening(1, "2026-10-31", 6000, "RUB"),),
            entries=(Entry(1, "2026-11-06", 100_000, "Доход"), Entry(1, "2026-11-12", -5000, "Трата")),
        )
        funds = Funds(
            (Fund("Поездка", "Цель", "RUB", number=1), Fund("Подушка", "Подушка", "RUB", number=2)),
            (Movement(1, 3000, "legacy"), Movement(2, 1200, "legacy")),
        ).apply("salary", ((1, 5000),), 1).apply("payment", ((1, -5000),), 2)
        first, last = on("2026-11-01"), on("2026-11-30")
        history = fund_history(funds, ledger, {1}, first, last)
        self.assertEqual((history.undated, history.current, history.points[-1][1]), (1, 3000, 0))
        self.assertFalse(fund_history(funds, ledger, {2}, first, last).points)
        dated = funds.date_movement("legacy", "2026-10-19").apply("new", ((2, 4000),), day="2026-11-20")
        self.assertEqual(Funds.from_payload(dated.to_payload()), dated)
        self.assertEqual(fund_history(dated, ledger, {1, 2}, first, last).points[-1][1], 8200)
        self.assertEqual(fund_history(dated, ledger, {1}, first, last).opening, 3000)
        self.assertEqual(account_history(ledger, {1}, first, last).points[-1][1], ledger.balance_on(1, last.isoformat()))
        snapshot = Asset("Вклад", "account", "Я", amount=20_000, as_of="2026-11-20")
        self.assertEqual(account_history(ledger, {2}, first, last, snapshot=snapshot).points,
                         ((on("2026-11-20"), 20_000),))
