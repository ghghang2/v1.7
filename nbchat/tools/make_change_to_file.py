# app/tools/make_change_to_file.py
"""
Tool that applies a unified diff to a file inside the repository.

The function accepts three parameters:

* ``path``   – file path *relative to the repository root* (may contain directories)
* ``op_type`` – one of ``create``, ``update`` or ``delete``
* ``diff``   – the unified‑diff string (or empty string for delete)

It returns a JSON string.  On success the JSON contains a ``result`` key;
on failure it contains an ``error`` key.  The format matches the
OpenAI function‑calling schema produced by ``app/tools/__init__``.
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from dataclasses import dataclass, make_dataclass
from typing import Callable, Literal, Sequence, Optional
import unicodedata

def _safe_resolve(repo_root: Path, rel_path: str) -> Path:
    """Resolve ``rel_path`` against ``repo_root`` and guard against directory traversal.
    Normalize to NFKC to ensure characters like '..' or '/' aren't spoofed"""
    normalized_path = unicodedata.normalize('NFKC', rel_path)
    target = (repo_root / rel_path).resolve()
    if not str(target).startswith(str(repo_root)):
        raise ValueError("Path escapes repository root")
    return target
def _extract_payload(diff: str) -> str:
    # If the agent wraps the patch, find the markers. 
    # If not found, return original to avoid breaking "naked" diff tests.
    match = re.search(r"\*\*\* Begin Patch(.*?)\*\*\* End Patch", diff, re.DOTALL)
    return match.group(1).strip() if match else diff

# --- CORE DIFF ENGINE ---

ApplyDiffMode = Literal["default", "create"]

@dataclass
class Chunk:
    orig_index: int
    del_lines: list[str]
    ins_lines: list[str]

@dataclass
class ParserState:
    lines: list[str]
    index: int = 0
    fuzz: int = 0
    line_offset: int = 1

@dataclass
class ReadSectionResult:
    next_context: list[str]
    section_chunks: list[Chunk]
    end_index: int
    eof: bool

# Correctly define the Match object
Match = make_dataclass("Match", [("new_index", int), ("fuzz", int)])

END_PATCH = "*** End Patch"
END_FILE = "*** End of File"
SECTION_TERMINATORS = [END_PATCH, "*** Update File:", "*** Delete File:", "*** Add File:"]
END_SECTION_MARKERS = [*SECTION_TERMINATORS, END_FILE]

def apply_diff(input_str: str, diff: str, mode: ApplyDiffMode = "default") -> str:
    newline = _detect_newline(input_str, diff, mode)
    diff_lines = _normalize_diff_lines(diff)
    if mode == "create":
        return _parse_create_diff(diff_lines, newline=newline)

    normalized_input = input_str.replace("\r\n", "\n")
    parsed_chunks, total_fuzz = _parse_update_diff(diff_lines, normalized_input)
    return _apply_chunks(normalized_input, parsed_chunks, newline=newline)

def _trim_overlap(ins_lines: list[str], following_lines: list[str]) -> list[str]:
    """
    Trims the end of ins_lines if they already exist at the start of following_lines.
    Prevents duplicate 'stitching' when the diff and file overlap.
    """
    max_check = min(len(ins_lines), len(following_lines))
    for i in range(max_check, 0, -1):
        if ins_lines[-i:] == following_lines[:i]:
            return ins_lines[:-i]
    return ins_lines
    
def _normalize_diff_lines(diff: str) -> list[str]:
    """Clean the diff and strip Unified Diff metadata headers."""
    raw_lines = [line.rstrip("\r") for line in re.split(r"\r?\n", diff.strip())]
    
    clean_lines = []
    for line in raw_lines:
        # Skip headers that look like Unified Diff metadata
        if line.startswith(("--- ", "+++ ", "Index: ", "diff --git", "*** Update File:", "*** Add File:", "*** Delete File:")):
            continue
        # Also skip the End of File markers that LLM might append
        if "*** End of File ***" in line:
            continue
        clean_lines.append(line)
        
    if clean_lines and clean_lines[-1] == "":
        clean_lines.pop()
    return clean_lines

def _detect_newline(input_str: str, diff: str, mode: ApplyDiffMode) -> str:
    text = input_str if mode != "create" and "\n" in input_str else diff
    return "\r\n" if "\r\n" in text else "\n"

def _is_done(state: ParserState, prefixes: Sequence[str]) -> bool:
    if state.index >= len(state.lines): return True
    return any(state.lines[state.index].startswith(p) for p in prefixes)

def _read_str(state: ParserState, prefix: str) -> str:
    if state.index >= len(state.lines): return ""
    current = state.lines[state.index]
    if current.startswith(prefix):
        state.index += 1
        state.line_offset += 1 # NEW: Stay in sync with index
        return current[len(prefix) :]
    return ""

def _parse_create_diff(lines: list[str], newline: str) -> str:
    parser = ParserState(lines=[*lines, END_PATCH])
    output = []
    while not _is_done(parser, SECTION_TERMINATORS):
        line = parser.lines[parser.index]
        parser.index += 1
        if line.startswith("@@"):
            continue
        if not line.startswith("+"): raise ValueError(f"Invalid Add Line: {line}")
        output.append(line[1:])
    return newline.join(output)

def _parse_update_diff(lines: list[str], input_str: str):
    parser = ParserState(lines=[*lines, END_PATCH])
    input_lines = input_str.split("\n")
    chunks, cursor = [], 0

    while not _is_done(parser, END_SECTION_MARKERS):
        anchor = _read_str(parser, "@@ ")
        has_bare_anchor = (
            anchor == "" and parser.index < len(parser.lines) and parser.lines[parser.index] == "@@"
        )
        if has_bare_anchor:
            parser.index += 1
        
        if not (anchor or has_bare_anchor or cursor == 0):
            current_line = parser.lines[parser.index] if parser.index < len(parser.lines) else ""
            raise ValueError(f"Invalid Line:\n{current_line}")
        
        if anchor.strip():
            cursor = _advance_cursor(anchor, input_lines, cursor, parser)

        section = _read_section(parser.lines, parser.index)
        match = _find_context(input_lines, section.next_context, cursor, section.eof)
        
        if match.new_index == -1:
            if section.section_chunks:
                first_chunk_ins = section.section_chunks[0].ins_lines
                already_applied_match = _find_context(input_lines, first_chunk_ins, cursor, section.eof)
                if already_applied_match.new_index != -1:
                    parser.index = section.end_index
                    cursor = already_applied_match.new_index + len(first_chunk_ins)
                    continue
            ctx_text = "\n".join(section.next_context)
            if section.eof:
                raise ValueError(f"Invalid EOF Context {cursor}:\n{ctx_text}")
            raise ValueError(f"Invalid Context {cursor}:\n{ctx_text}")

        match_start_index = match.new_index
        parser.fuzz += match.fuzz
        parser.index = section.end_index

        for ch in section.section_chunks:
            abs_orig_index = ch.orig_index + match_start_index

            after_del_index = abs_orig_index + len(ch.del_lines)

            following_lines = input_lines[after_del_index : after_del_index + len(ch.ins_lines)]
            
            final_ins = _trim_overlap(ch.ins_lines, following_lines)

            if final_ins == ch.del_lines:
                continue

            chunks.append(Chunk(
                orig_index=abs_orig_index,
                del_lines=list(ch.del_lines),
                ins_lines=list(final_ins)
            ))
            
        cursor = match_start_index + len(section.next_context)

    return chunks, parser.fuzz

def _advance_cursor(anchor: str, lines: list[str], cursor: int, parser: ParserState) -> int:
    # Try exact anchor match
    for i in range(cursor, len(lines)):
        if lines[i] == anchor:
            return i + 1
            
    # Try fuzzy anchor match (ignoring whitespace)
    for i in range(cursor, len(lines)):
        if lines[i].strip() == anchor.strip():
            parser.fuzz += 1
            return i + 1
            
    return cursor

def _read_section(lines: list[str], start_index: int) -> ReadSectionResult:
    context, del_lines, ins_lines, chunks = [], [], [], []
    mode, index = "keep", start_index
    
    while index < len(lines):
        raw = lines[index]
        if any(raw.startswith(term) for term in END_SECTION_MARKERS) or raw == "***":
            break
        if raw.startswith("@@"):
            break
        index += 1
        line = raw if raw else " "
        prefix = line[0]
        
        last_mode, mode = mode, {"+": "add", "-": "delete", " ": "keep"}.get(prefix, "keep")
        if mode == "keep" and last_mode != "keep" and (del_lines or ins_lines):
            chunks.append(Chunk(len(context)-len(del_lines), list(del_lines), list(ins_lines)))
            del_lines, ins_lines = [], []
        
        if mode == "delete": del_lines.append(line[1:]); context.append(line[1:])
        elif mode == "add": ins_lines.append(line[1:])
        else: context.append(line[1:])

    if del_lines or ins_lines:
        chunks.append(Chunk(len(context)-len(del_lines), list(del_lines), list(ins_lines)))
    
    is_eof = index < len(lines) and lines[index] == END_FILE
    return ReadSectionResult(context, chunks, index + (1 if is_eof else 0), is_eof)

def _equals_slice(
    source: list[str], target: list[str], start: int, map_fn: Callable[[str], str]
) -> bool:
    """Helper to compare a slice of lines using a transformation function (like strip)."""
    if start + len(target) > len(source):
        return False
    for offset, target_value in enumerate(target):
        if map_fn(source[start + offset]) != map_fn(target_value):
            return False
    return True

def _find_context(lines: list[str], context: list[str], start: int, eof: bool) -> Match:
    if not context:
        return Match(start, 0)

    # Helper performing the tiered search from a given index.
    def _search_from(idx: int) -> Match:
        # Tier 1: Exact match.
        for i in range(idx, len(lines) - len(context) + 1):
            if _equals_slice(lines, context, i, lambda v: v):
                return Match(i, 0)
        # Tier 2: Ignore trailing whitespace.
        for i in range(idx, len(lines) - len(context) + 1):
            if _equals_slice(lines, context, i, lambda v: v.rstrip()):
                return Match(i, 1)
        # Tier 3: Ignore all whitespace.
        for i in range(idx, len(lines) - len(context) + 1):
            if _equals_slice(lines, context, i, lambda v: v.strip()):
                return Match(i, 2)
        return Match(-1, 0)

    # If EOF is indicated, search from the end of the file first.
    if eof:
        end_start = max(0, len(lines) - len(context))
        match = _search_from(end_start)
        if match.new_index != -1:
            return match
        # If not found at EOF, continue with forward search.
    return _search_from(start)

def _apply_chunks(input_str: str, chunks: list[Chunk], newline: str) -> str:
    orig_lines = input_str.split("\n")
    dest_lines, cursor = [], 0
    for chunk in chunks:

        if chunk.orig_index > len(orig_lines):
            raise ValueError(
                f"_apply_chunks: chunk.origIndex {chunk.orig_index} > input length {len(orig_lines)}"
            )
        if cursor > chunk.orig_index:
            raise ValueError(
                f"_apply_chunks: overlapping chunk at {chunk.orig_index} (cursor {cursor})"
            )

        dest_lines.extend(orig_lines[cursor : chunk.orig_index])
        if chunk.ins_lines:
            dest_lines.extend(chunk.ins_lines)
        cursor = chunk.orig_index + len(chunk.del_lines)
    dest_lines.extend(orig_lines[cursor:])
    return newline.join(dest_lines)


def make_change_to_file(path: str, op_type: str, diff: str) -> str:
    """
    Apply a unified diff to a file inside the repository.

    Parameters
    ----------
    path : str
        Relative file path (under the repo root).
    op_type : str
        One of ``create``, ``update`` or ``delete``.
    diff : str
        Unified diff string (ignored for ``delete``).

    Returns
    -------
    str
        JSON string with either ``result`` or ``error``.
    """
    try:
        repo_root = Path(__file__).resolve().parents[2]  # app/tools -> app -> repo root
        target = _safe_resolve(repo_root, path)
        diff = _extract_payload(diff)

        if op_type == "create":
            target.parent.mkdir(parents=True, exist_ok=True)
            content = apply_diff("", diff, mode="create")
            target.write_text(content, encoding="utf-8")
            return json.dumps({"result": f"File created: {path}"})

        elif op_type == "update":
            if not target.exists():
                raise FileNotFoundError(f"File not found: {path}")
            original = target.read_text(encoding="utf-8")
            # Handle simple replace diffs that lack +/– prefixes
            diff_lines = diff.splitlines()
            if diff_lines and diff_lines[0].startswith("@@") and not any(l.startswith(("+", "-")) for l in diff_lines[1:]):
                # Treat lines after the header as the new content
                patched = "\n".join(diff_lines[1:])
            else:
                patched = apply_diff(original, diff)
            target.write_text(patched, encoding="utf-8")
            return json.dumps({
                "result": f"File updated: {path}",
                "summary": {"modified": [path]} # NEW: Added for programmatic use
            })

        elif op_type == "delete":
            target.unlink(missing_ok=True)
            return json.dumps({"result": f"File deleted: {path}"})

        else:
            raise ValueError(f"Unsupported operation type: {op_type}")

    except Exception as exc:
        # Any exception becomes an error JSON
        return json.dumps({"error": str(exc)})

# Exported names for automatic tool discovery
func = make_change_to_file
name = "make_change_to_file"
description = (
    "Apply a unified diff to a file. It is critical that the Unified diff is formatted correctly."
    "Supports create, update and delete operations. op_type: create, update or delete"
    "Returns a JSON string with either a `result` key or an `error` key."
)

__all__ = ["func", "name", "description"]