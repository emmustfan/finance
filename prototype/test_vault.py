from contextlib import closing
import stat
import tempfile
import unittest
from pathlib import Path

import pypdfium2 as pdfium
from cryptography.exceptions import InvalidTag

from sample import sample_pdf
from vault import copy_verified, create, open_pdf

FAMILY_TEST_PASSWORD = "sample family password"
PERSONAL_TEST_PASSWORD = "sample personal password"


class VaultTest(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.family = self.root / "family.fvault"
        self.pdf = sample_pdf()
        create(self.family, self.pdf, FAMILY_TEST_PASSWORD)

    def test_separate_keys_and_private_default_permissions(self):
        personal = self.root / "personal.fvault"
        create(personal, self.pdf, PERSONAL_TEST_PASSWORD)
        self.assertEqual(stat.S_IMODE(personal.stat().st_mode), 0o600)
        self.assertNotEqual(personal.read_bytes(), self.family.read_bytes())
        with self.assertRaises(InvalidTag):
            open_pdf(personal, FAMILY_TEST_PASSWORD)
        self.assertEqual(open_pdf(personal, PERSONAL_TEST_PASSWORD), self.pdf)

    def test_backup_restores_pdf_without_original(self):
        backup = self.root / "copy.fvault"
        restored = self.root / "restored.fvault"
        copy_verified(self.family, backup, FAMILY_TEST_PASSWORD)
        self.assertEqual(backup.read_bytes(), self.family.read_bytes())
        self.assertNotIn(b"Family archive - test PDF", backup.read_bytes())
        self.family.unlink()
        copy_verified(backup, restored, FAMILY_TEST_PASSWORD)
        opened = open_pdf(restored, FAMILY_TEST_PASSWORD)
        self.assertEqual(opened, self.pdf)
        with pdfium.PdfDocument(opened) as doc:
            self.assertEqual(len(doc), 1)
            with closing(doc[0]) as page:
                with closing(page.get_textpage()) as text_page:
                    self.assertEqual(text_page.get_text_range().strip(), "Family archive - test PDF")
                with closing(page.render(scale=0.5)) as bitmap:
                    self.assertGreater(bitmap.to_pil().getextrema()[0][1], 0)
                    self.assertLess(bitmap.to_pil().getextrema()[0][0], 255)

    def test_corruption_and_overwrite_are_rejected(self):
        corrupted = self.root / "corrupted.fvault"
        original = self.family.read_bytes()
        corrupted.write_bytes(original[:-1] + bytes([original[-1] ^ 1]))
        destination = self.root / "destination.fvault"
        with self.assertRaises(InvalidTag):
            copy_verified(corrupted, destination, FAMILY_TEST_PASSWORD)
        self.assertFalse(destination.exists())
        destination.write_bytes(b"existing")
        with self.assertRaises(FileExistsError):
            copy_verified(self.family, destination, FAMILY_TEST_PASSWORD)
        self.assertEqual(destination.read_bytes(), b"existing")


if __name__ == "__main__":
    unittest.main()
