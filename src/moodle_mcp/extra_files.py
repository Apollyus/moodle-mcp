"""Files and texts that Moodle's ``core_course_get_contents`` does not list as course files.

- assignment attachments (``mod_assign_get_assignments``)
- forum post attachments (``mod_forum_get_discussion_posts``)
- H5P packages (``mod_h5pactivity_get_h5pactivities_by_courses``)
- files embedded in section summaries / module descriptions (labels, page intros, ...)
- plain text of those descriptions and the links they contain

Every function degrades gracefully: when the token cannot call an API (or Moodle returns an
error) that source is skipped, so one missing web service never breaks listing or downloading.
"""

from __future__ import annotations

import html.parser
import mimetypes
import re
from typing import Iterator
from urllib.parse import unquote, urlsplit, urlunsplit

from .logger import logger
from .moodle import (
    APIFunction,
    MoodleAPIError,
    format_moodle_array_params,
    get_moodle_api_data,
    get_moodle_url,
)

MAX_DISCUSSIONS_PER_COURSE = 100
DISCUSSIONS_PER_PAGE = 50


def _site_prefix() -> tuple[str, str, str]:
    base = urlsplit(get_moodle_url())
    return base.scheme, base.netloc, base.path.split("/webservice/", 1)[0]


def to_webservice_url(url: str) -> str | None:
    """Return ``url`` rewritten to this site's ``/webservice/pluginfile.php`` or None if it is not one.

    Moodle writes embedded files as ``/pluginfile.php/...`` (browser session), but those only
    work with a token through ``/webservice/pluginfile.php/...``.
    """
    scheme, netloc, prefix = _site_prefix()
    target = urlsplit(url.strip())
    if (target.scheme, target.netloc) != (scheme, netloc):
        return None
    plain, webservice = prefix + "/pluginfile.php", prefix + "/webservice/pluginfile.php"
    if target.path.startswith(plain + "/"):
        path = webservice + target.path[len(plain):]
    elif target.path.startswith(webservice + "/"):
        path = target.path
    else:
        return None
    return urlunsplit((target.scheme, target.netloc, path, target.query, ""))


class _Links(html.parser.HTMLParser):
    """Collects ``src``/``href`` targets and anchor texts of an HTML fragment."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.sources: list[str] = []
        self.anchors: list[tuple[str, str]] = []
        self._href: str | None = None
        self._text: list[str] = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        for key in ("src", "data"):
            if attrs.get(key):
                self.sources.append(attrs[key])
        if tag == "a" and attrs.get("href"):
            self._href, self._text = attrs["href"], []
            self.sources.append(attrs["href"])

    def handle_data(self, data):
        if self._href is not None:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.anchors.append((self._href, " ".join("".join(self._text).split())))
            self._href = None


def parse_links(fragment: str | None) -> _Links:
    parser = _Links()
    if fragment:
        parser.feed(fragment)
    return parser


def embedded_file_urls(fragment: str | None) -> list[str]:
    """Token-capable URLs of this site's files referenced from an HTML fragment (deduplicated)."""
    seen: dict[str, None] = {}
    for source in parse_links(fragment).sources:
        converted = to_webservice_url(source)
        if converted:
            seen.setdefault(converted)
    return list(seen)


def filename_from_url(url: str) -> str:
    name = unquote(urlsplit(url).path.rsplit("/", 1)[-1])
    return name or "file"


def make_meta(moduleid: int, module: str, section: str, item: dict, filepath: str | None = None) -> dict:
    filename = item.get("filename") or filename_from_url(item["fileurl"])
    return {
        "moduleid": moduleid, "module": module, "section": section, "filename": filename,
        "filepath": filepath if filepath is not None else (item.get("filepath") or "/"),
        "filesize": item.get("filesize") or 0, "timemodified": item.get("timemodified") or 0,
        "mimetype": item.get("mimetype") or mimetypes.guess_type(filename)[0] or "",
    }


def _safe_call(function: APIFunction, params: dict | None = None):
    try:
        return get_moodle_api_data(function, params=params)
    except MoodleAPIError as exc:
        logger.info(f"{function.value} unavailable: {exc.error_code}")
        return None


def _usable(item: dict) -> str | None:
    """Token-capable URL for an API file item or None."""
    return to_webservice_url(item["fileurl"]) if item.get("fileurl") else None


def assignment_files(courseid: int, index: dict[int, tuple[str, str]]) -> Iterator[tuple[dict, str]]:
    data = _safe_call(APIFunction.mod_assign_get_assignments,
                      format_moodle_array_params("courseids", [courseid]))
    for course in (data or {}).get("courses", []):
        for assignment in course.get("assignments", []):
            cmid = assignment.get("cmid")
            if not cmid:
                continue
            module, section = index.get(cmid, (assignment.get("name", ""), ""))
            for key in ("introattachments", "activityattachments", "introfiles"):
                for item in assignment.get(key) or []:
                    url = _usable(item)
                    if url:
                        yield make_meta(cmid, module, section, item), url


def forum_files(courseid: int, index: dict[int, tuple[str, str]]) -> Iterator[tuple[dict, str]]:
    forums = _safe_call(APIFunction.mod_forum_get_forums_by_courses,
                        format_moodle_array_params("courseids", [courseid]))
    budget = MAX_DISCUSSIONS_PER_COURSE
    for forum in forums if isinstance(forums, list) else []:
        if not forum.get("numdiscussions") or not forum.get("cmid"):
            continue
        cmid = forum["cmid"]
        module, section = index.get(cmid, (forum.get("name", ""), ""))
        page = 0
        while budget > 0:
            listing = _safe_call(APIFunction.mod_forum_get_forum_discussions,
                                 {"forumid": forum["id"], "page": page, "perpage": DISCUSSIONS_PER_PAGE})
            discussions = (listing or {}).get("discussions", [])
            for discussion in discussions:
                if budget <= 0:
                    break
                budget -= 1
                posts = _safe_call(APIFunction.mod_forum_get_discussion_posts,
                                   {"discussionid": discussion["discussion"]})
                for post in (posts or {}).get("posts", []):
                    where = f"/discussion-{discussion['discussion']}/post-{post['id']}/"
                    for key in ("attachments", "messageinlinefiles"):
                        for item in post.get(key) or []:
                            url = _usable(item)
                            if url:
                                yield make_meta(cmid, module, section, item, filepath=where), url
            if len(discussions) < DISCUSSIONS_PER_PAGE:
                break
            page += 1
        if budget <= 0:
            logger.info(f"Forum scan of course {courseid} stopped after {MAX_DISCUSSIONS_PER_COURSE} discussions")
            return


def h5p_files(courseid: int, index: dict[int, tuple[str, str]]) -> Iterator[tuple[dict, str]]:
    data = _safe_call(APIFunction.mod_h5pactivity_get_h5pactivities_by_courses,
                      format_moodle_array_params("courseids", [courseid]))
    for activity in (data or {}).get("h5pactivities", []):
        cmid = activity.get("coursemodule")
        deployed = activity.get("deployedfile") or {}
        url = _usable(deployed) if deployed else None
        if cmid and url:
            module, section = index.get(cmid, (activity.get("name", ""), ""))
            yield make_meta(cmid, module, section, deployed), url


def embedded_files(sections: list[dict]) -> Iterator[tuple[dict, str]]:
    """Files referenced from section summaries (moduleid = -sectionid) and module descriptions."""
    for section in sections:
        sname = section.get("name", "")
        for url in embedded_file_urls(section.get("summary")):
            item = {"fileurl": url}
            yield make_meta(-int(section.get("id", 0)), f"Section: {sname}", sname, item, "/embedded/"), url
        for module in section.get("modules", []):
            for url in embedded_file_urls(module.get("description")):
                item = {"fileurl": url}
                yield make_meta(module["id"], module["name"], sname, item, "/embedded/"), url


def extra_files(courseid: int, sections: list[dict], seen_urls: set[str]) -> Iterator[tuple[dict, str]]:
    """All additional (metadata, url) pairs, skipping URLs already listed."""
    index = {m["id"]: (m["name"], s.get("name", "")) for s in sections for m in s.get("modules", [])}

    def fresh(pairs):
        for meta, url in pairs:
            key = url.split("?")[0]
            if key not in seen_urls:
                seen_urls.add(key)
                yield meta, url

    yield from fresh(assignment_files(courseid, index))
    yield from fresh(h5p_files(courseid, index))
    yield from fresh(embedded_files(sections))
    yield from fresh(forum_files(courseid, index))


# ---------------------------------------------------------------------------
# Texts and links
# ---------------------------------------------------------------------------


def html_to_text(fragment: str | None) -> str:
    from .reader import _extract_html  # local import: reader imports downloads

    return _extract_html((fragment or "").encode("utf-8"))[0]


def course_texts(sections: list[dict]) -> list[dict]:
    """Plain text of section summaries and module descriptions (labels, intros)."""
    result = []
    for section in sections:
        sname = section.get("name", "")
        text = html_to_text(section.get("summary"))
        if text:
            result.append({"moduleid": 0, "modname": "section", "section": sname,
                           "module": sname, "text": text})
        for module in section.get("modules", []):
            text = html_to_text(module.get("description"))
            if text:
                result.append({"moduleid": module["id"], "modname": module.get("modname", ""),
                               "section": sname, "module": module["name"], "text": text})
    return result


def course_links(sections: list[dict]) -> list[dict]:
    """External links: URL modules and anchors inside section summaries / descriptions."""
    links: list[dict] = []
    seen: set[tuple[int, str]] = set()

    def add(moduleid, section, module, title, url, source):
        url = url.strip()
        if not re.match(r"https?://", url, re.I) or to_webservice_url(url) or (moduleid, url) in seen:
            return
        seen.add((moduleid, url))
        links.append({"moduleid": moduleid, "section": section, "module": module,
                      "title": title, "url": url, "source": source})

    for section in sections:
        sname = section.get("name", "")
        for href, text in parse_links(section.get("summary")).anchors:
            add(0, sname, sname, text, href, "section_summary")
        for module in section.get("modules", []):
            if module.get("modname") == "url":
                for item in module.get("contents") or []:
                    if item.get("type") == "url" and item.get("fileurl"):
                        add(module["id"], sname, module["name"], module["name"], item["fileurl"], "url_module")
            for href, text in parse_links(module.get("description")).anchors:
                add(module["id"], sname, module["name"], text, href, "description")
    return links
