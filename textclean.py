"""Visible-text cleanup for the cockpit page.

The page shows no emoji and no exclamation points. Cleaning happens at render
time so a data push cannot put them on screen.
"""
import re

_EMOJI = re.compile(
    "["
    "\U0001F000-\U0001FAFF"
    "\u2600-\u27BF"
    "\uFE0F"
    "\u200D"
    "\u2190-\u21FF"
    "]+"
)
_DASH = re.compile(r"\s*[\u2013\u2014]\s*")
_SPACE = re.compile(r"\s{2,}")


def clean_text(value):
    """Return a display string with emoji and exclamation points removed."""
    if value is None:
        return ""
    text = str(value).replace("\xa0", " ")
    text = _DASH.sub(", ", text)
    text = text.replace(";", ",").replace("!", "")
    text = _EMOJI.sub("", text)
    text = _SPACE.sub(" ", text).strip(" ,")
    return text
