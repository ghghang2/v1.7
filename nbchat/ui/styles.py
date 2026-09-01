"""Centralized styling for nbchat UI components.

Modify the constants below to customize the chat interface appearance.
Changes propagate automatically to all message types.

Note: Functional styles (e.g., white-space: pre-wrap) are kept inline.
"""

import html
import re
from typing import Any, Dict, List

import ipywidgets as widgets

from nbchat.ui.utils import md_to_html
import logging

_log = logging.getLogger("nbchat.compaction")
# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

# Dark theme colors
BACKGROUND_LIGHT = "#1a1a1a"
BACKGROUND_ASSISTANT = "#2d2d2d"
CODE_COLOR = "#98c379"
TEXT_COLOR = "#e0e0e0"
LINK_COLOR = "#61dafb"

PADDING = "0px"
BORDER_RADIUS = "0px"
MARGIN = "0"

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _style(bg: str, text_color: str = TEXT_COLOR) -> str:
    return (
        f"background-color:{bg}; padding:{PADDING}; "
        f"border-radius:{BORDER_RADIUS}; margin:{MARGIN}; "
        f"color:{text_color};"
    )

def _div(content: str, bg: str) -> str:
    return f'<div style="{_style(bg)}">{content}</div>'

def _style_code(s: str) -> str:
    """Inject color style into un-styled <code>, <span>, and codehilite <div> tags."""
    s = re.sub(r"<code(?![^>]*\bstyle\b)([^>]*)>", rf'<code\1 style="color:{CODE_COLOR};">', s)
    s = re.sub(r'<div\s+class="codehilite"(?![^>]*\bstyle\b)([^>]*)>', rf'<div class="codehilite"\1 style="color:{CODE_COLOR};">', s)
    s = re.sub(r"<span(?![^>]*\bstyle\b)([^>]*)>", rf'<span\1 style="color:{CODE_COLOR};">', s)
    return s

def _md(content: str, inline: bool = False) -> str:
    h = md_to_html(content)
    if inline:
        h = re.sub(r"<p(?!re)[^>]*>", '<span style="margin:0;">', h)
        h = h.replace("</p>", "</span>")
    else:
        h = re.sub(r"<p(?!re)[^>]*>", '<p style="margin:0;">', h)
    return _style_code(h)

def _tool_calls_html(tool_calls: List[Dict[str, Any]]) -> str:
    if not tool_calls:
        return ""
    names = ", ".join(tc.get("function", {}).get("name", "unknown") for tc in tool_calls)
    rows = "<br>".join(
        f'<b>{tc.get("function",{}).get("name","unknown")}</b>: '
        f'<code style="color:{CODE_COLOR};">{html.escape(tc.get("function",{}).get("arguments","{}"))}</code>'
        for tc in tool_calls
    )
    return (
        f'<details style="margin:0;padding:0;">'
        f'<summary style="margin:0;display:block;">Tool calls: {names}</summary>'
        f'<div>{rows}</div></details>'
    )

# ---------------------------------------------------------------------------
# Public HTML generators
# ---------------------------------------------------------------------------

def user_message_html(content: str, prefix: str = "<b>User</b> ") -> str:
    return _div(prefix + _md(content, inline=True), BACKGROUND_LIGHT)

def assistant_message_html(content: str, prefix: str = "<b>Assistant</b> ") -> str:
    return _div(prefix + _md(content, inline=True), BACKGROUND_ASSISTANT)

def reasoning_html(content: str, summary: str = "<b>Reasoning</b>", open: bool = False) -> str:
    tag = "open" if open else ""
    inner = (
        f'<details {tag} style="margin:0;padding:0;">'
        f'<summary style="margin:0;display:block;">{summary}</summary>'
        f'<div>{_md(content)}</div></details>'
    )
    return _div(inner, BACKGROUND_LIGHT)

def assistant_full_html(reasoning: str, content: str, tool_calls: List[Dict[str, Any]]) -> str:
    parts = []
    if reasoning:
        parts.append(
            f'<details style="margin:0;padding:0;">'
            f'<summary style="margin:0;display:block;"><b>Reasoning</b></summary>'
            f'<div>{_md(reasoning)}</div></details>'
        )
    if tool_calls:
        parts.append(_tool_calls_html(tool_calls))
    parts.append(f"<b>Assistant:</b> {_md(content)}")
    return _div("".join(parts), BACKGROUND_ASSISTANT)

def assistant_message_with_tools_html(
    content: str,
    tool_calls: List[Dict[str, Any]],
    prefix: str = "<b>Assistant:</b> ",
) -> str:
    inner = prefix + _md(content, inline=True)
    if tool_calls:
        inner += "<br>\n" + _tool_calls_html(tool_calls)
    return _div(inner, BACKGROUND_ASSISTANT)

def tool_result_html(content: str, tool_name: str = "", preview: str = "", tool_args: str = "") -> str:
    if not preview:
        preview = content[:50] + ("..." if len(content) > 50 else "")
    label = f"<b>{html.escape(tool_name)}</b>" if tool_name else "<b>Tool</b>"
    summary = label
    if tool_args:
        _log.debug(f"tool_args: {tool_args}")
        summary += f"|{html.escape(tool_args)}"
    summary += f"|{html.escape(preview)}"
    inner = (
        f'<details style="margin:0;padding:0;"><summary>{summary}</summary>'
        f'<pre style="white-space:pre-wrap;word-wrap:break-word;">{html.escape(content)}</pre>'
        f'</details>'
    )
    return _div(inner, BACKGROUND_LIGHT)

def system_message_html(content: str) -> str:
    return _div(f"<b>System:</b> {content}", BACKGROUND_LIGHT)

# ---------------------------------------------------------------------------
# Widget factory
# ---------------------------------------------------------------------------

def compacted_summary_html(content: str) -> str:
    inner = (
        f'<details style="margin:0;padding:0;">'
        f'<summary style="margin:0;display:block;"><b>🔍 Compacted earlier conversation</b></summary>'
        f'<div>{_md(content)}</div></details>'
    )
    return _div(inner, BACKGROUND_LIGHT)

def make_widget(html_str: str) -> widgets.HTML:
    """Return an :class:`ipywidgets.HTML` widget.

    The original code defined this function inside
    ``compacted_summary_html`` due to a stray indentation.  That made the
    module fail to import.  The function is now defined at module level.
    """
    return widgets.HTML(
        value=html_str,
        layout=widgets.Layout(width="100%", margin="0", background_color="#1a1a1a"),
    )
