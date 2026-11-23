"""Find own-account transfers without changing the bank postings."""

import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import date

from assets import Asset, amount_text
from ledger import Ledger, Opening, reference_matches
from review import Review, TRANSFER_WORDS

SELF_TRANSFER = re.compile(
    r"\b(?:себе на (?:карту|сч[её]т)|перевод (?:самому |самой )?себе|пополнение собственных (?:оборотных )?средств)\b",
    re.IGNORECASE,
)
BANK_NAMES = {
    "модульбанк": ("модульбанк",),
    "тбанк": ("тбанк", "тинькофф", "tbank", "tinkoff"),
    "озонбанк": ("озонбанк", "ozonbank"),
}


def transfer_route(amount: int, account: str, other: str) -> str:
    source, target = (account, other) if amount < 0 else (other, account)
    return f"{source} → {target}"


def signed_amount(amount: int) -> str:
    return ("+" if amount > 0 else "") + amount_text(amount)


def own_transfer_hint(text: str) -> bool:
    description = text.casefold().replace("ё", "е")
    return bool(SELF_TRANSFER.search(text) or "перевод между счетами" in description
                or "перевод между своими счетами" in description)


def matching_account(reference: str, currency: str, openings: tuple[Opening, ...]) -> int | None:
    if not reference:
        return None
    found = [item.account for item in openings
             if item.currency == currency and reference_matches(reference, item.reference)]
    return found[0] if len(found) == 1 else None


def _compact_bank(text: str) -> str:
    return re.sub(r"[^a-zа-я0-9]", "", text.casefold())


@dataclass(frozen=True)
class TransferMatch:
    debit: int
    credit: int
    reason: str
    automatic: bool = False


def suggested_transfers(
    ledger: Ledger, review: Review, assets: tuple[Asset, ...], *, excluded=(), touched=None,
) -> tuple[TransferMatch, ...]:
    """Return the uniquely best pairs. Match signed, existing rows only."""
    blocked = review.fixed_operations | set(review.ignored_transfers) | set(excluded)
    assigned = review.assignments_by_operation()
    intended = review.transfer_targets()
    openings = {item.account: item for item in ledger.openings}
    bank = {}
    for asset in assets:
        name = _compact_bank(asset.organization)
        bank[asset.asset_number] = next((key for key, names in BANK_NAMES.items()
                                         if any(token in name for token in names)), "")

    entries = {}
    references = {}
    destination = {}
    amounts = defaultdict(list)
    by_account = defaultdict(list)
    by_sender = defaultdict(list)
    for number, entry in enumerate(ledger.entries, 1):
        if number in blocked:
            continue
        origin = openings[entry.account]
        day = date.fromisoformat(entry.day).toordinal()
        other = matching_account(entry.counterparty, origin.currency, ledger.openings)
        if other == entry.account:
            other = None
        references[number] = other
        destination[number] = intended.get(number, other)
        entries[number] = entry, day, origin.currency
        if entry.amount > 0:
            amounts[origin.currency, entry.amount, day].append(number)
            by_account[entry.account, entry.amount, day].append(number)
            if destination[number] is not None:
                by_sender[destination[number], entry.amount, day].append(number)

    best = {}
    for debit, (outgoing, day, currency) in entries.items():
        if outgoing.amount > 0:
            continue
        expected = destination[debit]
        source_bank = bank.get(outgoing.account)
        self_payment = bool(source_bank and SELF_TRANSFER.search(outgoing.description))
        candidates = set()
        for offset in range(-3, 4):
            post_day = day + offset
            key = -outgoing.amount, post_day
            if expected is not None:
                candidates.update(by_account.get((expected, *key), ()))
            candidates.update(by_sender.get((outgoing.account, *key), ()))
            if self_payment and abs(offset) <= 1:
                candidates.update(amounts.get((currency, *key), ()))
        for credit in candidates:
            incoming, other_day, other_currency = entries[credit]
            if currency != other_currency or outgoing.account == incoming.account:
                continue
            if expected not in (None, incoming.account) or destination[credit] not in (None, outgoing.account):
                continue
            if references[debit] not in (None, incoming.account) or references[credit] not in (None, outgoing.account):
                continue
            distance = abs(day - other_day)
            reciprocal = references[debit] == incoming.account and references[credit] == outgoing.account
            if reciprocal:
                grade, reason = 0, "Совпали реквизиты обеих сторон"
            elif references[debit] == incoming.account or references[credit] == outgoing.account:
                grade, reason = 1, "Совпал реквизит второго счёта"
            elif intended.get(debit) == incoming.account or intended.get(credit) == outgoing.account:
                grade, reason = 2, "Второй счёт выбран заранее"
            elif (
                self_payment and distance <= 1 and bank.get(incoming.account) not in (None, "", source_bank)
                and any(token in _compact_bank(incoming.description) for token in BANK_NAMES[source_bank])
                and any(word in incoming.description.casefold() for word in (*TRANSFER_WORDS, "пополнение"))
            ):
                grade, reason = 3, "Перевод себе · банк, дата и сумма совпали"
            else:
                continue
            if distance:
                reason += f" · разница дат {distance} дн."
            ranked = grade, distance
            full_references = all("*" not in reference for reference in (
                outgoing.counterparty, incoming.counterparty,
                openings[outgoing.account].reference, openings[incoming.account].reference,
            ))
            automatic = reciprocal and full_references and debit not in assigned and credit not in assigned
            match = TransferMatch(debit, credit, reason, automatic)
            for number in (debit, credit):
                previous = best.get(number)
                if previous is None or ranked < previous[0]:
                    best[number] = ranked, match
                elif ranked == previous[0]:
                    best[number] = ranked, None

    return tuple(
        match for number, (_, match) in sorted(best.items())
        if match is not None and number == match.debit and best[match.credit][1] == match
        and (touched is None or match.debit in touched or match.credit in touched)
    )
