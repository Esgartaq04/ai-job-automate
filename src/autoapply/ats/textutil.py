"""HTML -> plain text. Board APIs return escaped HTML in wildly different shapes."""

from __future__ import annotations

import html
import re

_TAG = re.compile(r"<[^>]+>")
_SCRIPT = re.compile(r"<(script|style)[^>]*>.*?</\1>", re.S | re.I)
_BLOCK = re.compile(r"<br\s*/?>|</p>|</li>|</div>|</h[1-6]>", re.I)


def strip_html(raw: str) -> str:
    """Unescape, flatten to text, and normalize whitespace.

    Boards double-escape inconsistently (Greenhouse escapes the whole body,
    Lever and Ashby don't), so unescaping happens on both sides of tag removal.
    """
    text = html.unescape(raw or "")
    text = _SCRIPT.sub(" ", text)
    text = _BLOCK.sub("\n", text)
    text = _TAG.sub("", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()
