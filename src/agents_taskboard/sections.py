"""One section of a markdown document, found by the heading slug agents-web gives the heading
(`src/markdown/headingId.ts`): lowercase, runs of non `[a-z0-9]` become `-`, trimmed, empty is
`section`, the second heading with the same slug gets `-2`, the third `-3`.

A section is its heading line and everything up to the next heading of the same or a higher
level. ATX (`#`) and setext (`===`, `---` under a paragraph line) headings count; lines inside
fenced code blocks do not. The heading text is reduced to what the web renders for links and
images (their label), so the slug matches the rendered anchor.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

SECTION_CAP = 40000

_ATX = re.compile(r"^ {0,3}(#{1,6})(?:[ \t]+(.*?))?[ \t]*$")
_CLOSING = re.compile(r"(?:^|[ \t]+)#+$")
_SETEXT = re.compile(r"^ {0,3}(=+|-+)[ \t]*$")
_FENCE = re.compile(r"^ {0,3}(`{3,}|~{3,})")
_LINK = re.compile(r"!?\[([^\]]*)\]\([^)]*\)")
_NOT_BLOCK = re.compile(r"^ {0,3}([-*+>]|\d{1,9}[.)])([ \t]|$)")


def slug(text: str, seen: dict[str, int]) -> str:
    """The headingId slug of `text`; `seen` counts slugs already given in the document."""
    base = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-") or "section"
    n = seen.get(base, 0) + 1
    seen[base] = n
    return base if n == 1 else f"{base}-{n}"


def _plain(text: str) -> str:
    return _LINK.sub(r"\1", text)


@dataclass(frozen=True)
class _Heading:
    slug: str
    level: int
    line: int


def _headings(lines: list[str]) -> list[_Heading]:
    seen: dict[str, int] = {}
    out: list[_Heading] = []
    fence: str | None = None
    previous_paragraph = False
    for index, raw in enumerate(lines):
        line = raw.rstrip("\r\n")
        opened = _FENCE.match(line)
        if fence is not None:
            closing = opened.group(1) if opened else ""
            if (
                closing[:1] == fence[0]
                and len(closing) >= len(fence)
                and not line.strip()[len(closing) :].strip()
            ):
                fence = None
            previous_paragraph = False
            continue
        if opened:
            fence = opened.group(1)
            previous_paragraph = False
            continue
        atx = _ATX.match(line)
        if atx:
            text = _CLOSING.sub("", atx.group(2) or "").strip()
            out.append(_Heading(slug(_plain(text), seen), len(atx.group(1)), index))
            previous_paragraph = False
            continue
        underline = _SETEXT.match(line)
        if underline and previous_paragraph:
            text = lines[index - 1].strip()
            level = 1 if underline.group(1)[0] == "=" else 2
            out.append(_Heading(slug(_plain(text), seen), level, index - 1))
            previous_paragraph = False
            continue
        previous_paragraph = bool(line.strip()) and not _NOT_BLOCK.match(line)
    return out


def find_section(body: str, heading: str) -> tuple[str, bool] | None:
    """(section text, truncated) of the heading whose slug is `heading`, or None. Text of
    `SECTION_CAP` characters or more is cut at a line boundary below the cap."""
    lines = body.splitlines(keepends=True)
    headings = _headings(lines)
    for position, found in enumerate(headings):
        if found.slug != heading:
            continue
        end = len(lines)
        for later in headings[position + 1 :]:
            if later.level <= found.level:
                end = later.line
                break
        text = "".join(lines[found.line : end])
        if len(text) < SECTION_CAP:
            return text, False
        kept: list[str] = []
        size = 0
        for line in lines[found.line : end]:
            if size + len(line) >= SECTION_CAP:
                break
            kept.append(line)
            size += len(line)
        return ("".join(kept) or text[: SECTION_CAP - 1]), True
    return None
