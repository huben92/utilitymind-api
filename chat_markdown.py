"""Render untrusted AI Markdown with a small, safe HTML vocabulary."""

import bleach
from markdown_it import MarkdownIt


_MARKDOWN = MarkdownIt("commonmark", {"html": False, "breaks": True})
_MARKDOWN.enable("table")
_MARKDOWN.enable("strikethrough")

_TAGS = {
    "a", "blockquote", "br", "code", "del", "em", "h1", "h2", "h3", "h4",
    "hr", "li", "ol", "p", "pre", "s", "strong", "table", "tbody", "td",
    "th", "thead", "tr", "ul",
}


def render_ai_markdown(answer: str) -> str:
    """Return formatted HTML safe to insert in an assistant chat bubble."""
    rendered = _MARKDOWN.render(answer)
    return bleach.clean(
        rendered,
        tags=_TAGS,
        attributes={"a": ["href", "title"]},
        protocols=["https", "http", "mailto"],
        strip=True,
    )
