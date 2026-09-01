"""ChatUI — main entry point.

Composes ContextMixin and ConversationMixin into a widget-based chat interface.
Widget output hooks are overridden in the "Output hooks" section below.
"""
from __future__ import annotations

import ipywidgets as widgets
import json
import logging
import re
import threading
import time
import urllib.request
import urllib.error
import uuid
from typing import List, Tuple

from IPython.display import display, Javascript, HTML

import nbchat.core.db as db
import nbchat.core.config as config
import nbchat.core.compressor as comp
import nbchat.core.monitoring as mon
from nbchat.ui import chat_renderer as renderer
from nbchat.ui.context_manager import ContextMixin, ImportanceTracker
from nbchat.ui.conversation import ConversationMixin
from nbchat.ui.utils import changed_files

_log = logging.getLogger("nbchat.chatui")


class ChatUI(ContextMixin, ConversationMixin):
    """Chat interface with streaming, reasoning, and tool execution."""

    MAX_TOOL_TURNS = config.MAX_TOOL_TURNS
    MAX_VISIBLE_WIDGETS = config.MAX_VISIBLE_WIDGETS

    def __init__(self):
        db.init_db()
        self.system_prompt = config.DEFAULT_SYSTEM_PROMPT
        self.model_name = config.MODEL_NAME
        self.session_id = str(uuid.uuid4())
        self.history: List[Tuple] = []
        self.task_log: List[str] = []
        self._turn_summary_cache: dict = {}
        self._summary_futures: dict = {}
        self._importance_tracker = ImportanceTracker(persist_fraction=getattr(config, "PERSIST_FRACTION", 0.40))
        self._stop_event = threading.Event()
        self._stream_thread = None
        self._tool_running = False
        self._history_lock = threading.Lock()
        self._reasoning_widget: widgets.HTML | None = None
        self._assistant_widget: widgets.HTML | None = None
        comp.init_session(self.session_id)
        self._create_widgets()
        self._start_metrics_updater()
        self._load_history()
        display(self.layout)
        self._inject_ui_scripts()

    # ── Output hooks ──────────────────────────────────────────────────────

    def _on_stream_token(self, content: str) -> None:
        if self._assistant_widget is None:
            self._assistant_widget = renderer.render_placeholder("assistant")
            children = list(self.chat_history.children)
            if self._reasoning_widget in children:
                children.insert(children.index(self._reasoning_widget) + 1, self._assistant_widget)
            else:
                children.append(self._assistant_widget)
            self.chat_history.children = children
        self._assistant_widget.value = renderer.render_assistant(content).value

    def _on_stream_reasoning(self, reasoning: str) -> None:
        if self._reasoning_widget is None:
            self._reasoning_widget = renderer.render_placeholder("reasoning")
            self._append(self._reasoning_widget)
        self._reasoning_widget.value = renderer.render_reasoning(reasoning).value

    def _on_tool_display(self, raw_result: str, tool_name: str, tool_args: str) -> None:
        self._append(renderer.render_tool(raw_result, tool_name, tool_args))

    def _on_agent_message(self, text: str) -> None:
        self._append(renderer.render_assistant(text))

    def _on_stream_complete(self, content: str, tool_calls: list | None) -> None:
        if tool_calls:
            w = renderer.render_assistant_with_tools(content, tool_calls)
            if self._assistant_widget is not None:
                self._assistant_widget.value = w.value
            else:
                children = list(self.chat_history.children)
                if self._reasoning_widget in children:
                    children.insert(children.index(self._reasoning_widget) + 1, w)
                else:
                    children.append(w)
                self.chat_history.children = children
        elif self._assistant_widget is None and content:
            self._append(renderer.render_assistant(content))
        self._reasoning_widget = None
        self._assistant_widget = None

    # ── UI injection ──────────────────────────────────────────────────────

    def _inject_ui_scripts(self) -> None:
        display(Javascript("""
        (function() {
            function attach() {
                var el = document.querySelector('.nbchat-history');
                if (!el) { setTimeout(attach, 400); return; }
                if (el._nbchatObserver) return;
                var saved = 0;
                el.addEventListener('scroll', function() { saved = el.scrollTop; }, { passive: true });
                el._nbchatObserver = new MutationObserver(function() {
                    requestAnimationFrame(function() { el.scrollTop = saved; });
                });
                el._nbchatObserver.observe(el, { childList: true });
            }
            attach();
        })();
        """))
        display(HTML("""<style>
        .nbchat-history, .jp-OutputArea-output { background-color: #1a1a1a !important; }
        .nbchat-sidebar { background-color: #1a1a1a !important; color: #e0e0e0 !important; }
        .nbchat-sidebar * { color: #e0e0e0 !important; background-color: #1a1a1a !important; }
        .nbchat-input-box textarea, .nbchat-history textarea { background-color: #2d2d2d !important; color: #e0e0e0 !important; }
        .nbchat-history .jp-RenderedHTML { background-color: #1a1a1a !important; }
        </style>"""))

    # ── Session state ─────────────────────────────────────────────────────

    def _reset_session_state(self) -> None:
        try:
            mon.flush_session_monitor(self.session_id, db)
        except Exception:
            pass
        with self._history_lock:
            self.history = []
        self.task_log = []
        self._turn_summary_cache = {}
        try:
            db.clear_core_memory(self.session_id)
            db.delete_episodic_for_session(self.session_id)
        except Exception:
            pass
        try:
            comp.clear_session(self.session_id)
        except Exception:
            pass

    # ── Widget creation ───────────────────────────────────────────────────

    def _create_widgets(self):
        _layout = lambda **kw: widgets.Layout(background_color="#1a1a1a", **kw)
        _bordered = lambda **kw: _layout(border="1px solid #444", **kw)

        self.metrics_output = widgets.HTML(value="<i>Loading...</i>", layout=_bordered(width="100%", padding="10px"))
        self.tools_output = widgets.HTML(layout=_bordered(width="100%", padding="2px"))
        self.monitoring_output = widgets.HTML(value="<i>No monitoring data yet.</i>", layout=_bordered(width="100%", padding="4px"))
        self._refresh_tools_list()

        new_chat_btn = widgets.Button(description="+", button_style="primary", layout=widgets.Layout(width="100%"))
        new_chat_btn.on_click(self._on_new_chat)

        options = list(db.get_session_ids())
        if self.session_id not in options:
            options.append(self.session_id)
        self.session_dropdown = widgets.Dropdown(options=options, value=self.session_id, layout=widgets.Layout(width="100%"))
        self.session_dropdown.observe(self._on_session_change, names="value")

        sidebar = widgets.VBox(
            [self.metrics_output, widgets.HTML("<hr>"), new_chat_btn, widgets.HTML("<hr>"),
             self.tools_output, widgets.HTML("<hr>"), self.monitoring_output, widgets.HTML("<hr>"),
             self.session_dropdown],
            layout=_bordered(width="15%"), _dom_classes=["nbchat-sidebar"],
        )

        self.chat_history = widgets.VBox([], layout=widgets.Layout(
            width="100%", height="100%", max_height="800px", overflow_y="auto",
            border="1px solid #444", background_color="#1a1a1a",
        ), _dom_classes=["nbchat-history"])

        self.input_text = widgets.Textarea(
            placeholder="...", rows=2,
            layout=widgets.Layout(width="90%", min_height="50px", height="auto",
                                   background_color="#2d2d2d", color="#e0e0e0"),
            _dom_classes=["nbchat-input-box"],
        )
        send_btn = widgets.Button(description="Send", button_style="success",
                                   layout=widgets.Layout(width="5%", padding="0", margin="0"))
        stop_btn = widgets.Button(description="Stop", button_style="warning",
                                   layout=widgets.Layout(width="5%", padding="0", margin="0"))
        send_btn.on_click(self._on_send)
        stop_btn.on_click(lambda *_: self._stop_event.set())

        main = widgets.VBox(
            [widgets.HTML(""), self.chat_history, widgets.HBox([self.input_text, send_btn, stop_btn])],
            layout=widgets.Layout(width="100%", padding="0px"),
        )
        self.layout = widgets.HBox([sidebar, main])

    def _refresh_tools_list(self):
        import nbchat.tools as tools_mod
        names = "<br>".join(t["function"]["name"] for t in tools_mod.get_tools())
        self.tools_output.value = f"<b>Tools</b><br>{names}"

    # ── Metrics updater ───────────────────────────────────────────────────

    def _start_metrics_updater(self):
        from nbchat.ui.styles import CODE_COLOR

        def loop():
            while True:
                try:
                    with urllib.request.urlopen(f"{config.SERVER_URL}/metrics", timeout=5) as r:
                        text = r.read().decode("utf-8", errors="ignore")

                    server_proc = prompt_tps = predict_tps = 0.0
                    for line in text.splitlines():
                        if line.startswith("llamacpp:requests_processing"):
                            parts = line.split()
                            if len(parts) >= 2:
                                try:
                                    server_proc = float(parts[1]) > 0
                                except ValueError:
                                    pass
                        if m := re.search(r"prompt_tokens_seconds\s+([\d.]+)", line):
                            try: prompt_tps = float(m.group(1))
                            except ValueError: pass
                        if m := re.search(r"predicted_tokens_seconds\s+([\d.]+)", line):
                            try: predict_tps = float(m.group(1))
                            except ValueError: pass

                    emoji = "🟢" if (server_proc or self._tool_running) else "⚫"
                    content = (
                        f'<b>Server</b> {emoji}<br>'
                        f'<b>PromptTS:</b> <code style="color:{CODE_COLOR};">{prompt_tps:.1f}</code><br>'
                        f'<b>PredictTS:</b> <code style="color:{CODE_COLOR};">{predict_tps:.1f}</code><br>'
                        f'<i>{time.strftime("%H:%M:%S")}</i>'
                    )
                    try:
                        if cf := changed_files():
                            content += "<br><br><b>Changed files:</b><br>" + "<br>".join(cf)
                    except Exception:
                        pass
                except (urllib.error.URLError, urllib.error.HTTPError) as e:
                    content = f"<i>Cannot connect to metrics: {e}</i>"
                except Exception as e:
                    content = f"<i>Error: {e}</i>"

                self.metrics_output.value = content
                time.sleep(1)

        threading.Thread(target=loop, daemon=True).start()

    # ── History ───────────────────────────────────────────────────────────

    def _load_history(self):
        with self._history_lock:
            self.history = list(db.load_history(self.session_id))
        self.task_log = db.load_task_log(self.session_id)
        self._turn_summary_cache = db.load_turn_summaries(self.session_id)
        self._render_history()
        self._refresh_monitoring_panel()

    def _refresh_monitoring_panel(self) -> None:
        try:
            from nbchat.ui.styles import CODE_COLOR
            session_report = mon.get_session_monitor(self.session_id).get_session_report()
            global_report = None
            try:
                raw = db.load_global_monitoring_stats()
                if raw:
                    global_report = mon.get_global_report(raw)
            except Exception:
                pass
            self.monitoring_output.value = mon.format_monitoring_html(
                session_report, global_report, code_color=CODE_COLOR
            )
        except Exception as exc:
            self.monitoring_output.value = f"<i style='color:orange;'>Monitoring error: {exc}</i>"

    def _render_history(self):
        window, effective_cut = self._window()
        children = []
        if effective_cut > 0:
            children.append(renderer.render_system(
                f"[{effective_cut} earlier messages outside context window omitted from view.]"
            ))
        for role, content, tool_id, tool_name, tool_args, _ef in window:
            if role == "user":
                children.append(renderer.render_user(content))
            elif role == "analysis":
                children.append(renderer.render_reasoning(content))
            elif role == "assistant":
                children.append(self._widget_for_assistant(content, tool_id, tool_args))
            elif role == "assistant_full":
                try:
                    msg = json.loads(tool_args)
                    children.append(renderer.render_assistant_full(
                        msg.get("reasoning_content", ""), msg.get("content", ""), msg.get("tool_calls", [])
                    ))
                except Exception as exc:
                    _log.debug("_render_history: failed to parse assistant_full: %r | %r", exc, tool_args)
                    children.append(renderer.render_assistant(tool_args or content))
            elif role == "tool":
                children.append(renderer.render_tool(content, tool_name, str(tool_args)))
            elif role == "system":
                children.append(renderer.render_system(content))
        self.chat_history.children = children

    def _widget_for_assistant(self, content: str, tool_id: str, tool_args: str) -> widgets.HTML:
        if tool_id == "multiple":
            try:
                return renderer.render_assistant_with_tools(content, json.loads(tool_args))
            except Exception:
                pass
        return renderer.render_assistant(content)

    def _append(self, widget: widgets.HTML):
        self.chat_history.children = (*self.chat_history.children, widget)

    def _prune_widgets(self) -> None:
        children = list(self.chat_history.children)
        if len(children) > self.MAX_VISIBLE_WIDGETS:
            trim = len(children) - self.MAX_VISIBLE_WIDGETS
            note = renderer.render_system(
                f"[{trim} earlier messages pruned from view. Full history saved in DB.]"
            )
            self.chat_history.children = [note] + children[trim:]

    # ── Event handlers ────────────────────────────────────────────────────

    def _on_new_chat(self, _):
        self.session_id = str(uuid.uuid4())
        self._reset_session_state()
        comp.init_session(self.session_id)
        options = list(db.get_session_ids())
        if self.session_id not in options:
            options.append(self.session_id)
        self.session_dropdown.options = options
        self.session_dropdown.value = self.session_id
        self._render_history()
        self._refresh_monitoring_panel()

    def _on_session_change(self, change):
        if change["new"]:
            self.session_id = change["new"]
            self._reset_session_state()
            comp.init_session(self.session_id)
            self._load_history()

    def _on_send(self, _):
        user_input = self.input_text.value.strip()
        if not user_input:
            return
        if self._stream_thread and self._stream_thread.is_alive():
            self._stop_event.set()
            self._stream_thread.join()
        self._prune_widgets()
        with self._history_lock:
            self.history.append(("user", user_input, "", "", "", 0))
        self._append(renderer.render_user(user_input))
        db.log_message(self.session_id, "user", user_input)
        self.input_text.value = ""
        self._stop_event.clear()
        self._stream_thread = threading.Thread(target=self._process_conversation_turn, daemon=True)
        self._stream_thread.start()