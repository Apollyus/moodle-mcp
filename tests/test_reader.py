import io
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from moodle_mcp import downloads, reader

W = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
A = 'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
S = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
R = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'


def make_zip(files: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as z:
        for name, content in files.items():
            z.writestr(name, content)
    return buffer.getvalue()


def make_pdf(text: str) -> bytes:
    """Minimal one-page PDF with a single text line."""
    objects = [
        "<< /Type /Catalog /Pages 2 0 R >>",
        "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 300 100] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>",
        None,
        "<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    stream = f"BT /F1 12 Tf 10 50 Td ({text}) Tj ET"
    objects[3] = f"<< /Length {len(stream)} >>\nstream\n{stream}\nendstream"
    out = b"%PDF-1.4\n"
    offsets = []
    for i, obj in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n{obj}\nendobj\n".encode()
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF".encode()
    return out


class ExtractTests(unittest.TestCase):
    def test_docx(self):
        data = make_zip({"word/document.xml": f'<w:document {W}><w:body><w:p><w:r><w:t>Hello</w:t></w:r>'
                         '<w:r><w:tab/><w:t>world</w:t></w:r></w:p><w:p><w:r><w:t>Druhý řádek</w:t></w:r></w:p>'
                         '</w:body></w:document>'})
        text, extractor, _ = reader.extract_text(data, "a.docx")
        self.assertEqual(text, "Hello\tworld\nDruhý řádek")
        self.assertEqual(extractor, "docx")

    def test_pptx_with_notes_and_order(self):
        def slide(t):
            return f'<sld {A}><a:p><a:r><a:t>{t}</a:t></a:r></a:p></sld>'
        data = make_zip({"ppt/slides/slide10.xml": slide("ten"), "ppt/slides/slide2.xml": slide("two"),
                         "ppt/slides/slide1.xml": slide("one"),
                         "ppt/notesSlides/notesSlide2.xml": f'<n {A}><a:p><a:r><a:t>speaker</a:t></a:r></a:p>'
                         f'<a:p><a:r><a:t>2</a:t></a:r></a:p></n>'})
        text, _, _ = reader.extract_text(data, "a.pptx")
        self.assertLess(text.index("one"), text.index("two"))
        self.assertLess(text.index("two"), text.index("ten"))
        self.assertIn("[Notes]\nspeaker", text)
        self.assertNotIn("\n2\n", text)

    def test_xlsx(self):
        data = make_zip({
            "xl/workbook.xml": f'<workbook {S} {R}><sheets><sheet name="Data" sheetId="1" r:id="rId1"/></sheets></workbook>',
            "xl/_rels/workbook.xml.rels": '<Relationships><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>',
            "xl/sharedStrings.xml": f'<sst {S}><si><t>Name</t></si><si><t>Alice</t></si></sst>',
            "xl/worksheets/sheet1.xml": f'<worksheet {S}><sheetData><row><c t="s"><v>0</v></c><c><v>Age</v></c></row>'
            '<row><c t="s"><v>1</v></c><c><v>30</v></c></row></sheetData></worksheet>'})
        text, _, _ = reader.extract_text(data, "a.xlsx")
        self.assertEqual(text, "--- Sheet: Data ---\nName\tAge\nAlice\t30")

    def test_pdf(self):
        text, _, warnings = reader.extract_text(make_pdf("Hello PDF"), "a.pdf")
        self.assertIn("--- Page 1 ---", text)
        self.assertIn("Hello PDF", text)
        self.assertEqual(warnings, [])

    def test_html(self):
        text, _, _ = reader.extract_text(b"<html><head><style>x{}</style></head><body><h1>T</h1><p>a &amp; b</p>"
                                          b"<script>bad()</script></body></html>", "a.html")
        self.assertEqual(text, "T\n\na & b")

    def test_text_encodings(self):
        self.assertEqual(reader.extract_text("Příliš žluťoučký".encode("cp1250"), "a.txt")[0], "Příliš žluťoučký")
        self.assertEqual(reader.extract_text("Příliš".encode("utf-8"), "a.md")[0], "Příliš")

    def test_unsupported_and_corrupt(self):
        with self.assertRaises(reader.UnsupportedFile):
            reader.extract_text(b"x", "a.png")
        with self.assertRaises(reader.UnsupportedFile):
            reader.extract_text(b"not a zip", "a.docx")  # e.g. old binary format renamed

    def test_zip_bomb_guard(self):
        with patch.object(reader, "MAX_XML_BYTES", 10):
            data = make_zip({"word/document.xml": "<w:document " + W + ">" + "x" * 100 + "</w:document>"})
            with self.assertRaises(reader.UnsupportedFile):
                reader.extract_text(data, "a.docx")


class ReadTests(unittest.TestCase):
    def setUp(self):
        self.cache = tempfile.TemporaryDirectory()
        self.addCleanup(self.cache.cleanup)
        patcher = patch.object(reader, "_cache_root", return_value=Path(self.cache.name))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.meta = {"moduleid": 7, "module": "Slides", "section": "Week 1", "filename": "n.txt",
                     "filepath": "/", "filesize": 0, "timemodified": 5, "mimetype": "text/plain"}
        self.downloaded = 0

        def fake_download(meta, url, destination):
            self.downloaded += 1
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(self.content, encoding="utf-8")

        self.content = "abcdefghij" * 300
        for target, replacement in [(downloads, "_download_file"), ]:
            p = patch.object(downloads, replacement, side_effect=fake_download)
            p.start()
            self.addCleanup(p.stop)
        files = patch.object(downloads, "_files", side_effect=lambda cid: iter([(self.meta, "https://x/f")]))
        files.start()
        self.addCleanup(files.stop)

    def test_paging_and_cache(self):
        first = reader.read_course_file(1, 7, max_chars=1000)
        self.assertEqual((first["total_chars"], first["total_pages"], first["has_more"]), (3000, 3, True))
        last = reader.read_course_file(1, 7, page=2, max_chars=1000)
        self.assertFalse(last["has_more"])
        self.assertEqual(last["text"], self.content[2000:])
        self.assertEqual(self.downloaded, 1)  # second read comes from the cache

    def test_validation(self):
        with self.assertRaises(ValueError):
            reader.read_course_file(1, 7, page=9, max_chars=1000)
        with self.assertRaises(ValueError):
            reader.read_course_file(1, 7, max_chars=10)
        with self.assertRaises(ValueError):
            reader.read_course_file(1, 8)

    def test_search_ignores_accents_and_reports_location(self):
        self.meta["filename"] = "n.txt"
        self.content = "--- Page 1 ---\nnic\n\n--- Page 2 ---\nEkonomická teorie a ekonomie"
        result = reader.search_course_files(1, "ekonomicka")
        self.assertEqual(result["files_searched"], 1)
        self.assertEqual(result["hits"][0]["location"], "Page 2")
        self.assertIn("Ekonomická teorie", result["hits"][0]["snippet"])
        self.assertEqual(len(reader.search_course_files(1, "ekonomi")["hits"]), 2)

    def test_search_skips_unsupported(self):
        self.meta["filename"] = "pic.png"
        result = reader.search_course_files(1, "abc")
        self.assertEqual(result["files_searched"], 0)
        self.assertEqual(result["hits"], [])


if __name__ == "__main__":
    unittest.main()
