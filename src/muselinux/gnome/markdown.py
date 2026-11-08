"""A small Markdown renderer that targets Pango markup.

Muse replies are Markdown. The GNOME client shows them in plain
``Gtk.Label`` widgets (no WebKit), so this converts the subset chat
replies use into Pango markup: paragraphs, headings, bullet and
numbered lists, block quotes, rules, fenced code, inline code, bold,
italics, strikethrough and links. Anything else passes through as
escaped text. Every character of the source is escaped before markup
is added, so reply text can never inject Pango tags, and links are
kept only for http, https and mailto targets.

``render(text)`` returns a list of blocks:

    ("markup", "<b>Pango</b> markup for a Gtk.Label")
    ("code", "raw code text", "language or empty")

Pure Python with no GTK import, so it is unit tested anywhere.
"""

from __future__ import annotations

import re
from html import escape as _html_escape

_FENCE = re.compile(r"^\s{0,3}(```|~~~)\s*([\w+#.-]*)\s*$")
_HEADING = re.compile(r"^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$")
_BULLET = re.compile(r"^(\s*)([-*+])\s+(.*)$")
_ORDERED = re.compile(r"^(\s*)(\d{1,9})([.)])\s+(.*)$")
_QUOTE = re.compile(r"^\s{0,3}>\s?(.*)$")
_RULE = re.compile(r"^\s{0,3}([-*_])(\s*\1){2,}\s*$")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")

_SAFE_SCHEMES = ("http://", "https://", "mailto:")
_HEADING_SIZE = {1: "x-large", 2: "large", 3: "large"}


def escape(text: str) -> str:
    return _html_escape(text, quote=True)


def _safe_url(url: str) -> str | None:
    url = url.strip()
    if url.lower().startswith(_SAFE_SCHEMES) and not any(
            c in url for c in ' \t\n"<>'):
        return url
    return None


_INLINE = re.compile(
    r"(?P<code>`+)(?P<code_body>.+?)(?P=code)"
    r"|\[(?P<link_text>[^\]\n]+)\]\((?P<link_url>[^)\s]+)\)"
    r"|(?P<auto>https?://[^\s<>()\[\]]+[^\s<>()\[\].,;:!?'\"])"
)
_STRONG = re.compile(r"(\*\*|__)(?=\S)(.+?)(?<=\S)\1")
_EM_STAR = re.compile(r"(?<![\w*])\*(?=\S)(.+?)(?<=\S)\*(?![\w*])")
_EM_UNDER = re.compile(r"(?<![\w_])_(?=\S)(.+?)(?<=\S)_(?![\w_])")
_STRIKE = re.compile(r"~~(?=\S)(.+?)(?<=\S)~~")


def _emphasis(escaped: str) -> str:
    escaped = _STRONG.sub(r"<b>\2</b>", escaped)
    escaped = _EM_STAR.sub(r"<i>\1</i>", escaped)
    escaped = _EM_UNDER.sub(r"<i>\1</i>", escaped)
    return _STRIKE.sub(r"<s>\1</s>", escaped)


def inline(text: str) -> str:
    """Inline Markdown to Pango markup, escaping everything else."""
    out: list[str] = []
    pos = 0
    for match in _INLINE.finditer(text):
        out.append(_emphasis(escape(text[pos:match.start()])))
        if match.group("code"):
            body = match.group("code_body")
            if len(match.group("code")) > 1:
                body = body.strip()
            out.append(f"<tt>{escape(body)}</tt>")
        elif match.group("link_text") is not None:
            label = _emphasis(escape(match.group("link_text")))
            url = _safe_url(match.group("link_url"))
            if url:
                out.append(f'<a href="{escape(url)}">{label}</a>')
            else:
                out.append(label + escape(f" ({match.group('link_url')})"))
        else:
            url = _safe_url(match.group("auto"))
            shown = escape(match.group("auto"))
            out.append(f'<a href="{escape(url)}">{shown}</a>' if url
                       else shown)
        pos = match.end()
    out.append(_emphasis(escape(text[pos:])))
    return "".join(out)


def render(text: str) -> list[tuple]:
    lines = (text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple] = []
    markup: list[str] = []      # pieces of the current markup block
    paragraph: list[str] = []

    def flush_paragraph():
        if paragraph:
            markup.append(inline("\n".join(paragraph)))
            paragraph.clear()

    def flush_markup():
        flush_paragraph()
        if markup:
            blocks.append(("markup", "\n\n".join(markup)))
            markup.clear()

    i = 0
    list_lines: list[str] = []

    def flush_list():
        if list_lines:
            markup.append("\n".join(list_lines))
            list_lines.clear()

    while i < len(lines):
        line = lines[i]
        fence = _FENCE.match(line)
        if fence:
            flush_list()
            flush_markup()
            marker, lang = fence.group(1), fence.group(2)
            body: list[str] = []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith(marker):
                body.append(lines[i])
                i += 1
            blocks.append(("code", "\n".join(body), lang))
            i += 1
            continue
        if _TABLE_ROW.match(line):
            flush_list()
            flush_markup()
            rows = []
            while i < len(lines) and _TABLE_ROW.match(lines[i]):
                rows.append(lines[i].strip())
                i += 1
            blocks.append(("code", "\n".join(rows), "table"))
            continue
        if not line.strip():
            flush_list()
            flush_paragraph()
            i += 1
            continue
        heading = _HEADING.match(line)
        if heading:
            flush_list()
            flush_paragraph()
            level = len(heading.group(1))
            size = _HEADING_SIZE.get(level)
            body = inline(heading.group(2))
            markup.append(f'<span size="{size}" weight="bold">{body}</span>'
                          if size else f"<b>{body}</b>")
            i += 1
            continue
        if _RULE.match(line) and not paragraph:
            flush_list()
            flush_paragraph()
            markup.append("─" * 24)
            i += 1
            continue
        bullet = _BULLET.match(line)
        ordered = _ORDERED.match(line)
        if bullet or ordered:
            flush_paragraph()
            if bullet:
                indent, body = bullet.group(1), bullet.group(3)
                mark = "•"
            else:
                indent, body = ordered.group(1), ordered.group(4)
                mark = ordered.group(2) + ordered.group(3)
            depth = min(len(indent.replace("\t", "    ")) // 2, 6)
            list_lines.append("    " * depth + f"{escape(mark)} {inline(body)}")
            i += 1
            continue
        if list_lines and line.startswith((" ", "\t")):
            # continuation of the previous list item
            list_lines[-1] += " " + inline(line.strip())
            i += 1
            continue
        quote = _QUOTE.match(line)
        if quote:
            flush_list()
            flush_paragraph()
            quoted = []
            while i < len(lines):
                q = _QUOTE.match(lines[i])
                if not q:
                    break
                quoted.append(q.group(1))
                i += 1
            markup.append(f'<span alpha="70%"><i>{inline(chr(10).join(quoted))}</i></span>')
            continue
        flush_list()
        paragraph.append(line)
        i += 1
    flush_list()
    flush_markup()
    return blocks


_LINK_TAGS = re.compile(r"</?a\b[^>]*>")


def without_links(markup: str) -> str:
    """The markup minus <a> tags, which Gtk.Label understands but plain
    Pango does not; used to validate markup before showing it."""
    return _LINK_TAGS.sub("", markup)


def to_plain(text: str) -> str:
    """Notification-safe plain text: markup characters stripped."""
    plain = re.sub(r"```.*?```", " [code] ", text or "", flags=re.S)
    plain = re.sub(r"[`*_~#>]+", "", plain)
    plain = re.sub(r"\[([^\]]+)\]\([^)]+\)", r"\1", plain)
    return re.sub(r"\s+", " ", plain).strip()
