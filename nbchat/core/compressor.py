"""Tool output compressor — keeps token usage bounded during agentic loops.

Strategy (priority order): passthrough → session-lossless → repeat-read →
syntax-aware skeleton (py/json/yaml/js) → head+tail → LLM summarisation.
"""
from __future__ import annotations

import ast
import json
import logging
import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Optional

from .config import MAX_TOOL_OUTPUT_CHARS

_log = logging.getLogger("nbchat.compaction")
if not _log.handlers:
    _h = logging.FileHandler("compaction.log", mode="a")
    _h.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    _log.addHandler(_h)
    _log.setLevel(logging.DEBUG)

FILE_READ_TOOLS = frozenset({
    "read_file", "cat", "view_file", "get_file",
    "list_files", "list_directory", "tree", "glob",
})
COMMAND_TOOLS = frozenset({
    "bash", "run_command", "execute", "sed", "awk", "head", "tail", "grep", "find",
})
ALWAYS_KEEP_TOOLS = FILE_READ_TOOLS | COMMAND_TOOLS
_STRUCTURED_EXTS = frozenset({".py", ".js", ".ts", ".jsx", ".tsx", ".json", ".yaml", ".yml"})
_LOSSLESS_WINDOW = 10

# Per-session state: lossless tool set + recent-compressed ring buffer
_sessions: dict[str, dict] = {}


def _sess(session_id: str) -> dict:
    if session_id not in _sessions:
        _sessions[session_id] = {"lossless": set(), "recent": deque(maxlen=_LOSSLESS_WINDOW)}
    return _sessions[session_id]


def init_session(session_id: str) -> None:
    _sessions[session_id] = {"lossless": set(), "recent": deque(maxlen=_LOSSLESS_WINDOW)}


def clear_session(session_id: str) -> None:
    _sessions.pop(session_id, None)


# ---------------------------------------------------------------------------
# Compression statistics
# ---------------------------------------------------------------------------

_stats: dict[str, dict] = defaultdict(lambda: {"calls": 0, "compressed": 0, "in": 0, "out": 0, "strategies": defaultdict(int)})


def _record(tool: str, in_len: int, out_len: int, strategy: str) -> None:
    s = _stats[tool]
    s["calls"] += 1
    s["in"] += in_len
    s["out"] += out_len
    s["strategies"][strategy] += 1
    if out_len < in_len:
        s["compressed"] += 1


def get_compression_stats() -> dict:
    return {
        t: {
            "calls": s["calls"],
            "compressed_calls": s["compressed"],
            "compression_rate": s["compressed"] / s["calls"] if s["calls"] else 0.0,
            "avg_ratio": s["out"] / s["in"] if s["in"] else 1.0,
            "strategies": dict(s["strategies"]),
        }
        for t, s in _stats.items()
    }


def reset_compression_stats() -> None:
    _stats.clear()


# ---------------------------------------------------------------------------
# Argument helpers
# ---------------------------------------------------------------------------

def _key_arg(tool_args: str) -> str:
    try:
        args = json.loads(tool_args)
        for k in ("path", "file", "filename", "filepath", "file_path", "target"):
            if isinstance(args.get(k), str):
                return args[k]
        return next((v for v in args.values() if isinstance(v, str)), tool_args[:100])
    except Exception:
        return tool_args[:100]


def _file_ext(tool_args: str) -> str:
    try:
        args = json.loads(tool_args)
        for k in ("path", "file", "filename", "filepath", "file_path", "target"):
            val = args.get(k, "")
            if isinstance(val, str) and "." in val:
                return Path(val).suffix.lower()
    except Exception:
        pass
    m = re.search(r'\b[\w./\-]+(\.[a-zA-Z]{1,6})\b', tool_args)
    return m.group(1).lower() if m else ""


# ---------------------------------------------------------------------------
# Skeleton extractors
# ---------------------------------------------------------------------------

def _python_skeleton(source: str, max_chars: int) -> Optional[str]:
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    lines = source.splitlines()
    get = lambda n: lines[n - 1] if 0 <= n - 1 < len(lines) else ""
    out = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            out.append(get(node.lineno))
        elif isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
            out.append(get(node.lineno))
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for d in node.decorator_list:
                out.append(get(d.lineno))
            out.append(get(node.lineno))
            ds = ast.get_docstring(node)
            if ds and len(ds) <= 120:
                out.append(f'    """{ds}"""')
            out.append("    ...")
        elif isinstance(node, ast.ClassDef):
            for d in node.decorator_list:
                out.append(get(d.lineno))
            out.append(get(node.lineno))
            ds = ast.get_docstring(node)
            if ds and len(ds) <= 120:
                out.append(f'    """{ds}"""')
            for item in node.body:
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    for d in item.decorator_list:
                        out.append(f"    {get(d.lineno).strip()}")
                    out.append(f"    {get(item.lineno).strip()}")
                    ds2 = ast.get_docstring(item)
                    if ds2 and len(ds2) <= 80:
                        out.append(f'        """{ds2}"""')
                    out.append("        ...")
            out.append("")
    sk = "\n".join(out)
    if len(sk) > max_chars:
        sk = sk[:max_chars] + "\n...[skeleton truncated]"
    return f"[Python skeleton — {len(lines)} lines total]\n{sk}"


def _json_skeleton(text: str, max_chars: int) -> Optional[str]:
    try:
        obj = json.loads(text)
    except Exception:
        return None
    if isinstance(obj, dict):
        preview = {}
        for k, v in list(obj.items())[:30]:
            if isinstance(v, dict):
                preview[k] = f"{{...{len(v)} keys}}"
            elif isinstance(v, list):
                preview[k] = f"[...{len(v)} items]"
            elif isinstance(v, str) and len(v) > 80:
                preview[k] = v[:80] + "..."
            else:
                preview[k] = v
        extra = f" ({len(obj) - 30} more keys)" if len(obj) > 30 else ""
        sk = f"[JSON object — {len(obj)} keys{extra}]\n" + json.dumps(preview, indent=2)
    elif isinstance(obj, list):
        first = json.dumps(obj[0], indent=2)[:400] if obj else "(empty)"
        sk = f"[JSON array — {len(obj)} items]\nFirst item:\n{first}"
    else:
        return None
    return sk[:max_chars] + "\n...[truncated]" if len(sk) > max_chars else sk


def _yaml_skeleton(text: str, max_chars: int) -> Optional[str]:
    lines = text.splitlines()
    top = [l[:120] for l in lines if l and not l[0].isspace() and not l.startswith("#") and ":" in l]
    if not top:
        return None
    sk = f"[YAML — {len(lines)} lines, top-level keys]\n" + "\n".join(top)
    return sk[:max_chars] + "\n...[truncated]" if len(sk) > max_chars else sk


def _js_skeleton(text: str, max_chars: int) -> Optional[str]:
    top_re = re.compile(
        r'^(?:export\s+(?:default\s+)?)?(?:async\s+)?'
        r'(?:function\*?\s+\w+|class\s+\w+|const\s+\w+\s*='
        r'|let\s+\w+\s*=|var\s+\w+\s*=|interface\s+\w+'
        r'|type\s+\w+[\s<]*[\w,<>=\s?]*[\s>=]*|enum\s+\w+)'
    )
    method_re = re.compile(
        r'^[ \t]{2,4}(?:(?:public|private|protected|static|async|override|readonly)\s+)*\w+\s*[(<]'
    )
    sigs = [l[:120] for l in text.splitlines() if top_re.match(l) or method_re.match(l)]
    if not sigs:
        return None
    sk = f"[JS/TS skeleton — {len(text.splitlines())} lines]\n" + "\n".join(sigs)
    return sk[:max_chars] + "\n...[truncated]" if len(sk) > max_chars else sk


def _syntax_skeleton(text: str, ext: str, max_chars: int) -> Optional[str]:
    dispatch = {".py": _python_skeleton, ".json": _json_skeleton,
                ".yaml": _yaml_skeleton, ".yml": _yaml_skeleton,
                ".js": _js_skeleton, ".ts": _js_skeleton,
                ".jsx": _js_skeleton, ".tsx": _js_skeleton}
    fn = dispatch.get(ext)
    return fn(text, max_chars) if fn else None


def _head_tail(text: str, max_chars: int, label: str = "") -> str:
    half = max_chars // 2
    suffix = f" of {label} output" if label else ""
    return (text[:half] + f"\n[...{len(text) - max_chars} chars omitted (middle{suffix})...]\n"
            + text[-half:])


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def compress_tool_output(tool_name: str, tool_args: str, result: str,
                         model: str, client, session_id: str = "") -> str:
    in_len = len(result)
    if in_len <= MAX_TOOL_OUTPUT_CHARS:
        _record(tool_name, in_len, in_len, "passthrough")
        return result

    state = _sess(session_id) if session_id else {"lossless": set(), "recent": deque()}
    key = _key_arg(tool_args)

    if tool_name in state["lossless"]:
        out = _head_tail(result, MAX_TOOL_OUTPUT_CHARS, tool_name)
        _record(tool_name, in_len, len(out), "lossless_headtail")
        return out

    if session_id and (tool_name, key) in state["recent"]:
        state["lossless"].add(tool_name)
        out = _head_tail(result, MAX_TOOL_OUTPUT_CHARS, tool_name)
        _record(tool_name, in_len, len(out), "lossless_learned")
        _log.debug("compress: %s(%r) repeated — marked lossless", tool_name, key[:40])
        return out

    if tool_name in FILE_READ_TOOLS:
        ext = _file_ext(tool_args)
        if ext in _STRUCTURED_EXTS:
            sk = _syntax_skeleton(result, ext, MAX_TOOL_OUTPUT_CHARS)
            if sk is not None:
                if session_id:
                    state["recent"].append((tool_name, key))
                _record(tool_name, in_len, len(sk), f"syntax_{ext.lstrip('.')}")
                return sk
        out = _head_tail(result, MAX_TOOL_OUTPUT_CHARS, tool_name)
        if session_id:
            state["recent"].append((tool_name, key))
        _record(tool_name, in_len, len(out), "headtail_file")
        return out

    if tool_name in COMMAND_TOOLS:
        out = _head_tail(result, MAX_TOOL_OUTPUT_CHARS, tool_name)
        if session_id:
            state["recent"].append((tool_name, key))
        _record(tool_name, in_len, len(out), "headtail_command")
        return out

    # LLM summarisation
    truncated = result[:MAX_TOOL_OUTPUT_CHARS]
    if in_len > MAX_TOOL_OUTPUT_CHARS:
        truncated += f"\n[...{in_len - MAX_TOOL_OUTPUT_CHARS} chars truncated for compression...]"
    prompt = (
        f"Tool: {tool_name}\nArgs: {tool_args}\nOutput:\n{truncated}\n\n"
        "Summarise concisely. Preserve: signatures, error messages/tracebacks verbatim, "
        "file paths, line numbers, returned values. Omit: blank lines, boilerplate, repeated blocks.\n"
        "If output is empty or a bare confirmation, respond exactly: NO_RELEVANT_OUTPUT\n"
        "Write only the summary, no preamble."
    )
    try:
        resp = client.chat.completions.create(
            model=model, messages=[{"role": "user", "content": prompt}], max_tokens=MAX_TOOL_OUTPUT_CHARS//2.5,
        )
        out = resp.choices[0].message.content.strip()
        if session_id:
            state["recent"].append((tool_name, key))
        _record(tool_name, in_len, len(out), "llm")
        return out
    except Exception as exc:
        _log.debug("compress: LLM failed (%s), falling back to head+tail", exc)
        out = _head_tail(result, MAX_TOOL_OUTPUT_CHARS, tool_name)
        _record(tool_name, in_len, len(out), "headtail_llm_fallback")
        return out


__all__ = [
    "compress_tool_output", "get_compression_stats", "reset_compression_stats",
    "init_session", "clear_session", "ALWAYS_KEEP_TOOLS", "FILE_READ_TOOLS", "COMMAND_TOOLS",
]