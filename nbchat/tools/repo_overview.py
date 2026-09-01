"""Tool to generate a markdown table of Python files, functions, and descriptions.

This tool scans the repository for Python files, extracts top‑level
functions (including their docstrings), writes a Markdown table to
``repo_overview.md`` in the repository root, and returns a confirmation
message.
"""

import ast
from pathlib import Path
from typing import Dict, List, Tuple

# ---------------------------------------------------------------------------
# Helper: walk Python files in the repo
# ---------------------------------------------------------------------------

def walk_python_files(root: Path) -> List[Path]:
    """Return a sorted list of all ``.py`` files under *root*.
    """
    return sorted(p for p in root.rglob("*.py") if p.is_file())

# ---------------------------------------------------------------------------
# Extract function info from a module file
# ---------------------------------------------------------------------------

def extract_functions_from_file(path: Path) -> List[Tuple[str, str]]:
    """Return a list of (function_name, docstring) for top‑level functions.

    Functions defined inside classes or other functions are ignored.
    """
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    funcs: List[Tuple[str, str]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef):
            name = node.name
            doc = ast.get_docstring(node) or ""
            funcs.append((name, doc.strip()))
    return funcs

# ---------------------------------------------------------------------------
# Build markdown table
# ---------------------------------------------------------------------------

def build_markdown_table(file_funcs: Dict[Path, List[Tuple[str, str]]]) -> str:
    header = ["| Relative path | Function | Description |",
              "|---------------|----------|-------------|"]
    rows = []
    for path, funcs in sorted(file_funcs.items()):
        rel = str(path.relative_to(Path.cwd()))
        for name, doc in funcs:
            doc = doc.replace("\n", " ")  # single line
            rows.append(f"| {rel} | {name} | {doc} |")
    return "\n".join(header + rows)

# ---------------------------------------------------------------------------
# The tool entry point
# ---------------------------------------------------------------------------

def func() -> str:
    """Generate a markdown table of all top‑level Python functions.

    The table is written to ``repo_overview.md`` in the repository root.
    """
    repo_root = Path.cwd()
    py_files = walk_python_files(repo_root)
    file_funcs: Dict[Path, List[Tuple[str, str]]] = {}
    for p in py_files:
        funcs = extract_functions_from_file(p)
        if funcs:
            file_funcs[p] = funcs
    md = build_markdown_table(file_funcs)
    out_path = repo_root / "repo_overview.md"
    out_path.write_text(md, encoding="utf-8")
    return f"Markdown table written to {out_path.name}"

# ---------------------------------------------------------------------------
# Tool metadata
# ---------------------------------------------------------------------------
name = "repo_overview"
description = "Generate a markdown table of all Python functions in the repo and save it to repo_overview.md"

# ---------------------------------------------------------------------------
# If run directly, execute the function
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print(func())
