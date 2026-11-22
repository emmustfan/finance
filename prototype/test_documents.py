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

    def test_stage_zero_archive_opens_with_its_pdf(self):
        pdf = sample_pdf()
        create(self.path, pdf, PASSWORD)
        catalog = Catalog.open(self.path, PASSWORD)
        self.assertTrue(catalog.legacy)
        self.assertEqual(catalog.cards[0].pdf, pdf)


if __name__ == "__main__":
    unittest.main()
