import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag

from documents import Card, Catalog, read_pdf
from sample import sample_pdf
from vault import copy_verified, create

PASSWORD = "sample family password"


class DocumentTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "documents.fvault"

    def test_document_and_paper_address_recovered_after_source_removal(self):
        pdf = sample_pdf()
        input_path = self.root / "input.pdf"
        input_path.write_bytes(pdf)
        catalog = Catalog.create(self.path, PASSWORD)
        catalog = catalog.add(
            Card("Договор", kind="Договор", number="А-237", party="Анна",
                 tags="Дом", cabinet="Шкаф", folder="03", section="2",
                 position="07", pdf=read_pdf(input_path)),
            PASSWORD,
        )
        catalog.add(Card("Квитанция", folder="05"), PASSWORD)
        input_path.unlink()

        backup = self.root / "backup.fvault"
        copy_verified(self.path, backup, PASSWORD)
        self.assertNotIn("Договор".encode(), backup.read_bytes())
        self.assertNotIn(b"%PDF", backup.read_bytes())
        self.path.unlink()
        recovered = self.root / "recovered.fvault"
        copy_verified(backup, recovered, PASSWORD)
        restored = Catalog.open(recovered, PASSWORD)
        self.assertEqual(len(restored.cards), 2)
        found, = [card for card in restored.cards if card.matches("а-237 анна")]
        self.assertEqual(found.address, "Шкаф → 03 → 2 → 07")
        self.assertEqual(found.pdf, pdf)
        self.assertIsNone(restored.cards[1].pdf)

    def test_rejects_invalid_card_password_and_stale_writer(self):
        original = Catalog.create(self.path, PASSWORD)
        card = Card("Квитанция", folder="05")
        with self.assertRaises(ValueError):
            original.add(Card("Без оригинала и файла"), PASSWORD)
        with self.assertRaises(InvalidTag):
            original.add(card, "different test password")
        self.assertEqual(Catalog.open(self.path, PASSWORD).cards, ())

        latest = original.add(card, PASSWORD)
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            original.add(Card("Другая карточка", folder="05"), PASSWORD)
        self.assertEqual(Catalog.open(self.path, PASSWORD), latest)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_stage_zero_archive_can_be_edited_with_its_pdf_intact(self):
        pdf = sample_pdf()
        create(self.path, pdf, PASSWORD)
        catalog = Catalog.open(self.path, PASSWORD)
        self.assertTrue(catalog.editable)
        tagged = catalog.set_tags(0, "Анна, 2024", PASSWORD)
        tagged.add(Card("Квитанция", folder="03"), PASSWORD)
        reopened = Catalog.open(self.path, PASSWORD)
        self.assertEqual(reopened.cards[0], Card("Тестовый документ", tags="Анна, 2024", pdf=pdf))
        self.assertEqual(reopened.cards[1].title, "Квитанция")

    def test_new_sample_archive_can_add_cards_and_edit_tags(self):
        sample = Card("Тестовый документ", pdf=sample_pdf())
        catalog = Catalog.create(self.path, PASSWORD, cards=(sample,))
        self.assertTrue(catalog.editable)
        updated = catalog.add(Card("Квитанция", folder="03"), PASSWORD)
        updated.set_tags(0, "Анна", PASSWORD)
        reopened = Catalog.open(self.path, PASSWORD)
        self.assertEqual(reopened.cards[0].tags, "Анна")
        self.assertEqual(reopened.cards[0].pdf, sample.pdf)
        self.assertEqual(reopened.cards[1].title, "Квитанция")

    def test_shared_archive_opens_for_reading_and_copies_to_editable_file(self):
        pdf = sample_pdf()
        create(self.path, pdf, PASSWORD)
        self.path.chmod(0o640)
        original = self.path.read_bytes()

        catalog = Catalog.open(self.path, PASSWORD)
        self.assertFalse(catalog.editable)
        self.assertEqual(catalog.cards[0].pdf, pdf)
        with self.assertRaisesRegex(ValueError, "с приватными правами"):
            catalog.set_tags(0, "Анна", PASSWORD)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

        personal = self.root / "personal.fvault"
        copy_verified(self.path, personal, PASSWORD)
        self.assertTrue(Catalog.open(personal, PASSWORD).editable)

    def test_tags_filter_exactly_and_combine_with_search(self):
        catalog = Catalog.create(self.path, PASSWORD)
        catalog = catalog.add(
            Card("Договор", tags="Анна, 2024, анна, Семейный архив", folder="01"),
            PASSWORD,
        )
        catalog = catalog.add(
            Card("Квитанция", tags="Аннабель, 2024", folder="02"),
            PASSWORD,
        )
        self.assertEqual(catalog.available_tags, ("2024", "Анна", "Аннабель", "Семейный архив"))
        self.assertEqual(catalog.cards[0].tags, "Анна, 2024, Семейный архив")
        self.assertEqual(
            [card.title for card in catalog.cards if card.matches("", tag="АННА")],
            ["Договор"],
        )
        self.assertEqual(
            [card.title for card in catalog.cards if card.matches("квит", tag="2024")],
            ["Квитанция"],
        )
        self.assertTrue(catalog.cards[0].matches("", tag="семейный архив"))

    def test_tags_can_be_changed_and_cleared_without_losing_other_fields(self):
        pdf = sample_pdf()
        catalog = Catalog.create(self.path, PASSWORD).add(
            Card("Договор", number="07", tags="Старый тег", folder="01", pdf=pdf),
            PASSWORD,
        )
        catalog = Catalog.open(self.path, PASSWORD)
        updated = catalog.set_tags(0, "  Имя, 2024, ИМЯ,, ", PASSWORD)
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            catalog.set_tags(0, "Устаревшее изменение", PASSWORD)

        backup = self.root / "backup.fvault"
        copy_verified(self.path, backup, PASSWORD)
        restored = Catalog.open(backup, PASSWORD)
        self.assertEqual(restored.cards, updated.cards)
        self.assertEqual(
            restored.cards[0],
            Card("Договор", number="07", tags="Имя, 2024", folder="01", pdf=pdf),
        )
        restored.set_tags(0, "", PASSWORD)
        self.assertEqual(Catalog.open(backup, PASSWORD).available_tags, ())


if __name__ == "__main__":
    unittest.main()
