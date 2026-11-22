from dataclasses import asdict
import json
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.exceptions import InvalidTag
from PIL import Image

from documents import Card, Catalog, read_pdf, read_photo
from sample import sample_pdf
from vault import DOCUMENT_MAGIC, HEADER_SIZE, NONCE_SIZE, copy_verified, create, encrypt

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
        )
        catalog.add(Card("Квитанция", folder="05"))
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

    def test_rejects_invalid_card_wrong_archive_password_and_stale_writer(self):
        original = Catalog.create(self.path, PASSWORD)
        card = Card("Квитанция", folder="05")
        with self.assertRaises(ValueError):
            original.add(Card("Без оригинала и файла"))
        with self.assertRaises(InvalidTag):
            Catalog.open(self.path, "different test password")
        self.assertEqual(Catalog.open(self.path, PASSWORD).cards, ())

        latest = original.add(card)
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            original.add(Card("Другая карточка", folder="05"))
        self.assertEqual(Catalog.open(self.path, PASSWORD), latest)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o600)

    def test_stage_zero_archive_can_be_edited_with_its_pdf_intact(self):
        pdf = sample_pdf()
        create(self.path, pdf, PASSWORD)
        catalog = Catalog.open(self.path, PASSWORD)
        self.assertTrue(catalog.editable)
        tagged = catalog.set_tags(0, "Анна, 2024")
        tagged.add(Card("Квитанция", folder="03"))
        reopened = Catalog.open(self.path, PASSWORD)
        self.assertEqual(reopened.cards[0], Card("Тестовый документ", tags="Анна, 2024", pdf=pdf, archive_number=1))
        self.assertEqual(reopened.cards[1].title, "Квитанция")

    def test_new_sample_archive_can_add_cards_and_edit_tags(self):
        sample = Card("Тестовый документ", pdf=sample_pdf())
        catalog = Catalog.create(self.path, PASSWORD, cards=(sample,))
        self.assertTrue(catalog.editable)
        updated = catalog.add(Card("Квитанция", folder="03"))
        updated.set_tags(0, "Анна")
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
            catalog.set_tags(0, "Анна")
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o640)

        personal = self.root / "personal.fvault"
        copy_verified(self.path, personal, PASSWORD)
        self.assertTrue(Catalog.open(personal, PASSWORD).editable)

    def test_tags_filter_exactly_and_combine_with_search(self):
        catalog = Catalog.create(self.path, PASSWORD)
        catalog = catalog.add(
            Card("Договор", tags="Анна, 2024, анна, Семейный архив", folder="01"),
        )
        catalog = catalog.add(
            Card("Квитанция", tags="Аннабель, 2024", folder="02"),
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
        )
        catalog = Catalog.open(self.path, PASSWORD)
        updated = catalog.set_tags(0, "  Имя, 2024, ИМЯ,, ")
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            catalog.set_tags(0, "Устаревшее изменение")

        backup = self.root / "backup.fvault"
        copy_verified(self.path, backup, PASSWORD)
        restored = Catalog.open(backup, PASSWORD)
        self.assertEqual(restored.cards, updated.cards)
        self.assertEqual(
            restored.cards[0],
            Card("Договор", number="07", tags="Имя, 2024", folder="01", pdf=pdf, archive_number=1),
        )
        restored.set_tags(0, "")
        self.assertEqual(Catalog.open(backup, PASSWORD).available_tags, ())

    def test_previous_catalog_numbers_and_custom_types_survive_edit_and_backup(self):
        old_card = asdict(Card("Старый договор", number="А-52", folder="03"))
        old_card.pop("archive_number")
        encrypted = encrypt(json.dumps([old_card]).encode(), PASSWORD, DOCUMENT_MAGIC)
        self.path.write_bytes(encrypted)
        self.path.chmod(0o600)

        initial = Catalog.open(self.path, PASSWORD)
        original, = initial.cards
        self.assertEqual(original.archive_label, "0001")
        self.assertTrue(original.matches("0001"))
        self.assertTrue(original.matches("а-52"))
        self.assertIn("Страховой полис", initial.available_kinds)

        catalog = initial.update(0, Card("Договор", kind="Наследство", number="А-52", folder="03"))
        first_save = self.path.read_bytes()
        catalog = catalog.add(Card("Паспорт", kind="Паспорт", folder="04"))
        self.assertIn("Наследство", catalog.available_kinds)
        self.assertEqual([card.archive_number for card in catalog.cards], [1, 2])
        self.assertEqual(catalog.cards[0].number, "А-52")

        backup = self.root / "unlocked-backup.fvault"
        catalog.backup(backup)
        with self.assertRaises(FileExistsError):
            catalog.backup(backup)
        restored = Catalog.open(backup, PASSWORD)
        self.assertEqual(restored.cards, catalog.cards)
        self.assertEqual(restored.available_kinds, catalog.available_kinds)

        nonce_offset = HEADER_SIZE - NONCE_SIZE
        saved = self.path.read_bytes()
        self.assertEqual(saved[:nonce_offset], encrypted[:nonce_offset])
        self.assertEqual(
            len({data[nonce_offset:HEADER_SIZE] for data in (encrypted, first_save, saved)}), 3
        )
        with self.assertRaisesRegex(ValueError, "Архив изменился"):
            initial.backup(self.root / "stale.fvault")
        self.assertFalse((self.root / "stale.fvault").exists())

    def test_photos_and_pdf_are_restored_without_source_files(self):
        png = self.root / "front.png"
        jpg = self.root / "back.jpg"
        Image.new("RGBA", (80, 40), "blue").save(png)
        Image.new("RGB", (40, 80), "red").save(jpg)
        photos = (read_photo(png), read_photo(jpg))
        sample = sample_pdf()
        catalog = Catalog.create(self.path, PASSWORD, cards=(
            Card("Фото оригинала", photos=photos),
            Card("Копия и фотографии", pdf=sample, photos=photos),
        ))
        self.assertNotIn(b"front.png", self.path.read_bytes())
        copy = self.root / "backup.fvault"
        catalog.backup(copy)
        png.unlink()
        jpg.unlink()
        self.path.unlink()
        copy_verified(copy, self.path, PASSWORD)
        restored = Catalog.open(self.path, PASSWORD)
        self.assertEqual(restored.cards[0].photos, photos)
        self.assertEqual(restored.cards[1].pdf, sample)
        self.assertEqual(restored.cards[1].photos, photos)
        self.assertEqual(restored.cards[0].photos[1].render((30, 30)).size, (15, 30))

    def test_photo_requires_a_supported_image(self):
        path = self.root / "invalid.png"
        path.write_bytes(sample_pdf())
        with self.assertRaisesRegex(ValueError, "Выберите JPEG или PNG"):
            read_photo(path)

    def test_paper_inventory_groups_location_and_sorts_section_and_position(self):
        catalog = Catalog.create(self.path, PASSWORD, cards=(
            Card("Договор", cabinet="Шкаф", folder="03", section="10"),
            Card("Страховка", cabinet="Шкаф", folder="03", section="2", position="11"),
            Card("Паспорт", cabinet="шкаф", folder="03", section="2", position="2"),
            Card("Другая папка", cabinet="Ящик", folder="03"),
            Card("Без папки", cabinet="Полка"),
        ))
        inventory = catalog.folder_inventory()
        self.assertEqual(list(inventory), [("Шкаф", "03"), ("Ящик", "03")])
        self.assertEqual(
            [card.title for card in inventory[("Шкаф", "03")]],
            ["Паспорт", "Страховка", "Договор"],
        )
        self.assertEqual([card.title for card in inventory[("Ящик", "03")]], ["Другая папка"])


if __name__ == "__main__":
    unittest.main()
