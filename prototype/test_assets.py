import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from assets import Asset, amount_text, parse_amount, totals_by_category
from documents import Card, Catalog
from sample import sample_pdf
from vault import copy_verified

PASSWORD = "sample family password"


class AssetTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "family.fvault"

    def test_amounts_are_exact_and_summaries_keep_currency_and_category_separate(self):
        self.assertEqual(parse_amount("125 500,50"), 12_550_050)
        self.assertEqual(parse_amount(amount_text(-902)), -902)
        self.assertEqual(parse_amount("0"), 0)
        self.assertIsNone(parse_amount(""))
        with self.assertRaises(ValueError):
            parse_amount("1,005")

        assets = (
            Asset("Счёт 1", "account", "Анна", amount=100, as_of="2026-11-01"),
            Asset("Счёт 2", "account", "Анна", amount=-25, as_of="2026-11-01"),
            Asset("Счёт USD", "account", "Анна", amount=200, currency="USD", as_of="2026-11-01"),
            Asset("Закрытый счёт", "account", "Анна", amount=600, status="inactive", as_of="2026-11-01"),
            Asset("Брокерский счёт", "investment", "Анна", amount=300, as_of="2026-11-01"),
            Asset("Квартира", "property", "Семья"),
        )
        self.assertEqual(totals_by_category(assets), {
            "account": {"RUB": 75, "USD": 200}, "investment": {"RUB": 300}, "property": {},
        })

    def test_assets_and_links_survive_document_edits_and_restore(self):
        pdf = sample_pdf()
        Catalog.create(self.path, PASSWORD, cards=(
            Card("Договор", pdf=pdf),
            Card("Полис", folder="03"),
        ))
        initial = Catalog.open(self.path, PASSWORD)
        self.assertEqual(initial.assets, ())
        apartment = Asset(
            "Квартира", "property", "Семья", kind="Квартира", amount=999_999_999,
            as_of="2026-11-01", access_pointer="Семейные доступы → Полис квартиры",
            document_numbers=(1, 2),
        )
        with self.assertRaisesRegex(ValueError, "из этого архива"):
            initial.add_asset(replace(apartment, document_numbers=(999,)))
        with self.assertRaisesRegex(ValueError, "дату"):
            initial.add_asset(replace(apartment, as_of=""))
        catalog = initial.add_asset(apartment)
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            initial.set_tags(0, "Старый сеанс")
        catalog = catalog.update_asset(0, replace(apartment, amount=1_100_000_000))
        catalog = catalog.set_tags(0, "Недвижимость")
        self.assertEqual(catalog.assets[0].asset_number, 1)
        self.assertEqual(catalog.assets[0].document_numbers, (1, 2))
        self.assertNotIn("Семейные доступы".encode(), self.path.read_bytes())

        backup = self.root / "copy.fvault"
        catalog.backup(backup)
        self.path.unlink()
        copy_verified(backup, self.path, PASSWORD)
        restored = Catalog.open(self.path, PASSWORD)
        self.assertEqual(restored.cards, catalog.cards)
        self.assertEqual(restored.assets, catalog.assets)
        self.assertEqual(restored.cards[0].pdf, pdf)


if __name__ == "__main__":
    unittest.main()
