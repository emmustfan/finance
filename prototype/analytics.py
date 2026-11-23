"""Calendar cash-flow reports and dated balance histories in minor currency units."""

from bisect import bisect_left
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, timedelta

from assets import Asset
from debts import Debts
from finance import Activity
from funds import Funds
from ledger import Ledger

MAX_BUCKETS = 360
GRAINS = ("day", "week", "month", "year")
Group = str | int


@dataclass(frozen=True)
class Bucket:
    start: date
    end: date


def calendar_buckets(first: date, last: date, requested: str) -> tuple[str, tuple[Bucket, ...]]:
    if last < first:
        raise ValueError("Начало периода должно предшествовать окончанию.")
    days = (last - first).days + 1
    year_count = last.year - first.year + 1
    year_step = (year_count + MAX_BUCKETS - 1) // MAX_BUCKETS
    if requested == "auto":
        requested = "day" if days <= 35 else "week" if days <= 122 else "month"
    for grain in GRAINS[GRAINS.index(requested):]:
        buckets = []
        cursor = first
        while cursor <= last and len(buckets) < MAX_BUCKETS:
            if grain == "day":
                end = cursor
            elif grain == "week":
                end = cursor + timedelta(days=min(6 - cursor.weekday(), (last - cursor).days))
            elif grain == "month":
                if (cursor.year, cursor.month) == (last.year, last.month):
                    end = last
                else:
                    next_month = date(cursor.year + (cursor.month == 12), cursor.month % 12 + 1, 1)
                    end = next_month - timedelta(days=1)
            else:
                end = min(last, date(min(last.year, cursor.year + year_step - 1), 12, 31))
            buckets.append(Bucket(cursor, end))
            if end == last:
                return grain, tuple(buckets)
            cursor = end + timedelta(days=1)


@dataclass(frozen=True)
class FlowSeries:
    group: Group
    values: tuple[int, ...]

    @property
    def total(self) -> int:
        return sum(self.values)


def flow_series(
    rows: tuple[Activity, ...], buckets: tuple[Bucket, ...], kind: str, *, by_wallet=False, account=None,
) -> tuple[tuple[int, ...], tuple[FlowSeries, ...]]:
    ends = tuple(bucket.end.isoformat() for bucket in buckets)
    begin = buckets[0].start.isoformat()
    total = [0] * len(buckets)
    groups = {}
    for row in rows:
        amount = row.expense if kind == "expense" else row.income
        if not amount or (account is not None and row.account != account) or not begin <= row.day <= ends[-1]:
            continue
        key = row.account if by_wallet else row.expense_category if kind == "expense" else row.category
        series = groups.get(key)
        if series is None:
            series = groups[key] = [0] * len(buckets)
        index = bisect_left(ends, row.day)
        total[index] += amount
        series[index] += amount
    parts = [FlowSeries(key, tuple(values)) for key, values in groups.items()]
    parts.sort(key=lambda line: (-line.total, str(line.group).casefold()))
    return tuple(total), tuple(parts)


@dataclass(frozen=True)
class BalanceHistory:
    points: tuple[tuple[date, int], ...]
    opening: int | None = None
    undated: int = 0
    current: int | None = None


def balance_history(
    events: dict[date, int], first: date, last: date, *, undated=0, current=None,
) -> BalanceHistory:
    if undated and not any(first <= day <= last for day in events):
        return BalanceHistory((), undated=undated, current=current)
    opening = 0 if undated else None
    for day, amount in events.items():
        if not undated and day < first:
            opening = (opening or 0) + amount
    points = {first: opening} if opening is not None else {}
    running = opening or 0
    for day, amount in sorted(events.items()):
        if first <= day <= last:
            running += amount
            points[day] = running
    if points:
        points[last] = running
    return BalanceHistory(tuple(points.items()), opening, undated, current)


def debt_history(debts: Debts, ledger: Ledger, numbers: set[int], first: date, last: date) -> BalanceHistory:
    events = defaultdict(int)
    for item in debts.items:
        if item.number in numbers:
            events[date.fromisoformat(item.day)] += item.amount
    for payment in debts.payments:
        if payment.debt in numbers and payment.principal:
            events[date.fromisoformat(ledger.entries[payment.operation - 1].day)] -= payment.principal
    return balance_history(events, first, last)


def fund_history(funds: Funds, ledger: Ledger, numbers: set[int], first: date, last: date) -> BalanceHistory:
    events = defaultdict(int)
    undated, total = 0, 0
    for item in funds.movements:
        if item.fund not in numbers:
            continue
        total += item.amount
        day = item.effective_day(ledger)
        if day:
            events[date.fromisoformat(day)] += item.amount
        else:
            undated += 1
    return balance_history(events, first, last, undated=undated, current=total)


def account_history(
    ledger: Ledger, numbers: set[int], first: date, last: date, *, snapshot: Asset | None = None,
) -> BalanceHistory:
    events = defaultdict(int)
    tracked = {item.account for item in ledger.openings if item.account in numbers}
    if not tracked and snapshot is not None and snapshot.amount is not None:
        day = date.fromisoformat(snapshot.as_of)
        return BalanceHistory(((day, snapshot.amount),) if first <= day <= last else ())
    for opening in ledger.openings:
        if opening.account in tracked:
            events[date.fromisoformat(opening.day)] += opening.amount
    for entry in ledger.entries:
        if entry.account in tracked:
            events[date.fromisoformat(entry.day)] += entry.amount
    return balance_history(events, first, last)
