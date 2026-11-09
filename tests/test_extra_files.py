import unittest
from unittest.mock import patch

from moodle_mcp import extra_files as ef
from moodle_mcp.moodle import APIFunction, MoodleAPIError

SITE = "https://moodle.example"
WS = f"{SITE}/webservice/pluginfile.php"


class ExtraFilesTests(unittest.TestCase):
    def setUp(self):
        p = patch.object(ef, "get_moodle_url", return_value=f"{SITE}/webservice/rest/server.php")
        p.start()
        self.addCleanup(p.stop)

    def test_to_webservice_url(self):
        self.assertEqual(ef.to_webservice_url(f"{SITE}/pluginfile.php/1/mod_label/intro/a%20b.jpg"),
                         f"{WS}/1/mod_label/intro/a%20b.jpg")
        self.assertEqual(ef.to_webservice_url(f"{WS}/1/x.pdf?forcedownload=1"), f"{WS}/1/x.pdf?forcedownload=1")
        self.assertIsNone(ef.to_webservice_url("https://evil.example/pluginfile.php/1/x.pdf"))
        self.assertIsNone(ef.to_webservice_url(f"{SITE}/course/view.php?id=1"))

    def test_embedded_file_urls_dedupe_and_filter(self):
        html = (f'<img src="{SITE}/pluginfile.php/9/mod_label/intro/a.jpg">'
                f'<a href="{SITE}/pluginfile.php/9/mod_label/intro/a.jpg">again</a>'
                '<a href="https://other.example/x.pdf">ext</a>')
        self.assertEqual(ef.embedded_file_urls(html), [f"{WS}/9/mod_label/intro/a.jpg"])

    def test_embedded_files_sections_and_modules(self):
        sections = [{"id": 5, "name": "Week 1", "summary": f'<img src="{SITE}/pluginfile.php/1/s.png">',
                     "modules": [{"id": 7, "name": "Label", "description": f'<a href="{SITE}/pluginfile.php/2/m.pdf">x</a>'}]}]
        result = list(ef.embedded_files(sections))
        self.assertEqual([(m["moduleid"], m["filename"]) for m, _ in result], [(-5, "s.png"), (7, "m.pdf")])
        self.assertEqual(result[1][0]["mimetype"], "application/pdf")

    def test_assignment_files(self):
        data = {"courses": [{"assignments": [
            {"cmid": 11, "name": "A1", "introattachments": [
                {"filename": "t.pdf", "filesize": 3, "fileurl": f"{WS}/1/mod_assign/introattachment/0/t.pdf"}]},
            {"cmid": 12, "name": "A2", "introattachments": []}]}]}
        with patch.object(ef, "get_moodle_api_data", return_value=data):
            result = list(ef.assignment_files(1, {11: ("Task one", "Week 2")}))
        self.assertEqual(len(result), 1)
        meta, url = result[0]
        self.assertEqual((meta["moduleid"], meta["module"], meta["section"], meta["filename"]),
                         (11, "Task one", "Week 2", "t.pdf"))
        self.assertTrue(url.startswith(WS))

    def test_forum_files(self):
        def api(function, params=None):
            if function == APIFunction.mod_forum_get_forums_by_courses:
                return [{"id": 1, "cmid": 20, "name": "F", "numdiscussions": 1}, {"id": 2, "cmid": 21, "numdiscussions": 0}]
            if function == APIFunction.mod_forum_get_forum_discussions:
                return {"discussions": [{"discussion": 100}]}
            return {"posts": [{"id": 5, "attachments": [{"filename": "a.docx", "fileurl": f"{WS}/1/a.docx"}],
                               "messageinlinefiles": []}]}
        with patch.object(ef, "get_moodle_api_data", side_effect=api):
            result = list(ef.forum_files(1, {20: ("Forum", "Week 3")}))
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0][0]["filepath"], "/discussion-100/post-5/")
        self.assertEqual(result[0][0]["module"], "Forum")

    def test_missing_web_service_is_skipped(self):
        err = MoodleAPIError("accessexception", "no", "x")
        with patch.object(ef, "get_moodle_api_data", side_effect=err):
            self.assertEqual(list(ef.assignment_files(1, {})), [])
            self.assertEqual(list(ef.forum_files(1, {})), [])
            self.assertEqual(list(ef.h5p_files(1, {})), [])

    def test_extra_files_deduplicates_against_seen(self):
        data = {"courses": [{"assignments": [{"cmid": 11, "name": "A", "introattachments": [
            {"filename": "t.pdf", "fileurl": f"{WS}/1/t.pdf?x=1"}]}]}]}
        with patch.object(ef, "get_moodle_api_data", return_value=data):
            fresh = list(ef.extra_files(1, [], set()))
            skipped = list(ef.extra_files(1, [], {f"{WS}/1/t.pdf"}))
        self.assertEqual(len(fresh), 1)
        self.assertEqual(skipped, [])

    def test_course_texts_and_links(self):
        sections = [{"id": 1, "name": "W1", "summary": '<p>Úvod <a href="https://a.example/x">odkaz</a></p>',
                     "modules": [
                         {"id": 3, "name": "Label", "modname": "label", "description": "<div>Hello<br>world</div>"},
                         {"id": 4, "name": "Web", "modname": "url", "description": "",
                          "contents": [{"type": "url", "fileurl": "https://b.example/doc.pdf"}]},
                         {"id": 5, "name": "Img", "modname": "label",
                          "description": f'<a href="{SITE}/pluginfile.php/1/f.pdf">file</a>'}]}]
        texts = ef.course_texts(sections)
        self.assertEqual([t["modname"] for t in texts], ["section", "label", "label"])
        self.assertEqual(texts[1]["text"], "Hello\nworld")
        links = ef.course_links(sections)
        self.assertEqual({(l["url"], l["source"]) for l in links},
                         {("https://a.example/x", "section_summary"), ("https://b.example/doc.pdf", "url_module")})


if __name__ == "__main__":
    unittest.main()
