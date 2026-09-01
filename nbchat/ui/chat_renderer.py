"""Widget rendering utilities for nbchat.

Thin wrappers around styles.py that return ipywidgets.HTML instances.
Add new roles here without touching the rest of the codebase.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

import ipywidgets as widgets

from nbchat.ui.styles import (
    make_widget,
    user_message_html,
    assistant_message_html,
    reasoning_html,
    tool_result_html,
    assistant_message_with_tools_html,
    assistant_full_html,
    system_message_html,
    compacted_summary_html,
)

__all__ = [
    "render_user",
    "render_assistant",
    "render_reasoning",
    "render_tool",
    "render_assistant_with_tools",
    "render_assistant_full",
    "render_system",
    "render_placeholder",
    "render_compacted_summary",
]


def render_user(content: str) -> widgets.HTML:
    return make_widget(user_message_html(content))

def render_assistant(content: str) -> widgets.HTML:
    return make_widget(assistant_message_html(content))

def render_reasoning(content: str, open: bool = True) -> widgets.HTML:
    return make_widget(reasoning_html(content, open=open))

def render_tool(
    content: str,
    tool_name: str,
    tool_args: str = "",
    preview: Optional[str] = None,
) -> widgets.HTML:
    return make_widget(tool_result_html(content, tool_name=tool_name, preview=preview or "", tool_args=tool_args))

def render_assistant_with_tools(content: str, tool_calls: List[Dict[str, Any]]) -> widgets.HTML:
    return make_widget(assistant_message_with_tools_html(content, tool_calls))

def render_assistant_full(reasoning: str, content: str, tool_calls: List[Dict[str, Any]]) -> widgets.HTML:
    return make_widget(assistant_full_html(reasoning, content, tool_calls))

def render_system(content: str) -> widgets.HTML:
    return make_widget(system_message_html(content))

def render_placeholder(role: str) -> widgets.HTML:
    if role == "assistant":
        return render_assistant("")
    if role == "reasoning":
        return render_reasoning("")
    raise ValueError(f"Unknown placeholder role: {role!r}")

def render_compacted_summary(content: str) -> widgets.HTML:
    return make_widget(compacted_summary_html(content))