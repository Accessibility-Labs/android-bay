import json
from pathlib import Path
import sys
import tempfile
import unittest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from catalog import write_catalog


class CatalogTests(unittest.TestCase):
    def test_escapes_phone_names_and_excludes_unsafe_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            rows=[{"status":"copied","source":"/sdcard/<script>alert(1)</script>.txt","localPath":"safe file.txt","size":3,"category":"shared-storage"},
                  {"status":"copied","source":"/bad","localPath":"../outside.txt","size":4},
                  {"status":"skipped","source":"/not-copied","localPath":"skip.txt"}]
            (root / "manifest.jsonl").write_text("\n".join(json.dumps(r) for r in rows),encoding="utf-8")
            self.assertEqual(write_catalog(root),1)
            content=(root / "catalog.html").read_text(encoding="utf-8")
            self.assertNotIn("<script>alert(1)",content)
            self.assertIn("&lt;script&gt;alert(1)",content)
            self.assertIn('href="safe%20file.txt"',content)
            self.assertNotIn("outside.txt",content)
            self.assertNotIn("/not-copied",content)

    def test_empty_manifest_produces_valid_empty_index(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(write_catalog(directory),0)
            self.assertTrue((Path(directory)/"catalog.html").is_file())


if __name__=="__main__": unittest.main()
