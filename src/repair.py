"""Code-level repair executor, stdlib only.

A repair is a precise text replacement, not a rewrite.
apply_patch() replaces old_text with new_text in a file,
only when old_text occurs exactly once. Zero matches
means the file is not what the repair expected, and
more than one match means the repair is ambiguous, so
both cases raise ValueError and leave the file untouched,
instead of guessing which occurrence was meant.

Safety, in order:
1. The original content is read into memory first.
2. If backup is True, the original is copied to
   <path>.bak, for example file.py.bak, before any write.
3. The patched content is written.
4. For a .py file, the result is compiled with py_compile.
   On compile failure the original content is written
   back, so a broken .py file is never left behind, and
   the result is ok=False with the compile error, not
   an exception. The backup file, when made, is kept,
   so the caller can still inspect the original.
"""

from __future__ import annotations

import py_compile
import shutil
from pathlib import Path


def apply_patch(path, old_text: str, new_text: str, backup: bool = True) -> dict:
    if not old_text:
        raise ValueError("old_text must not be empty")
    target = Path(path)
    original = target.read_text(encoding="utf-8")
    count = original.count(old_text)
    if count == 0:
        raise ValueError("old_text not found in file")
    if count > 1:
        raise ValueError("old_text found more than once, repair is ambiguous")
    backup_path = None
    if backup:
        backup_path = str(target) + ".bak"
        shutil.copyfile(target, backup_path)
    patched = original.replace(old_text, new_text, 1)
    target.write_text(patched, encoding="utf-8")
    if target.suffix == ".py":
        try:
            py_compile.compile(str(target), doraise=True)
        except Exception as exc:
            target.write_text(original, encoding="utf-8")
            return {"ok": False, "backup_path": backup_path, "error": str(exc)}
    return {"ok": True, "backup_path": backup_path, "error": None}
