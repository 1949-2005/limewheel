"""Watch folders: a file that lands in a watched folder is converted automatically.

A rule is {"folder": str, "action": "op:<id>" | "preset:<id>", "subfolder": "Converted", "enabled": bool}.
Results go into the subfolder (never next to the source) so they don't trigger the watcher again.
Only files that appear after the app starts are handled; files still being written are waited for.
"""
from __future__ import annotations

import os
import shutil
import uuid
from pathlib import Path

from PySide6.QtCore import QFileSystemWatcher, QObject, QTimer, Signal

from .ops import REGISTRY, OpError, ops_for
from .ops import presets
from .ops.base import unique

PARTIAL = {".tmp", ".crdownload", ".part", ".partial", ".download", ".opdownload", ".!ut", ".aria2"}
KIND = {"img": "Pictures", "vid": "Videos", "aud": "Audio", "pdf": "PDF", "docx": "Word", "doc": "Word",
        "md": "Markdown", "txt": "Text", "html": "HTML", "rtf": "RTF", "epub": "EPUB", "sheet": "Spreadsheets",
        "pptx": "Slides", "ppt": "Slides", "srt": "Subtitles", "vtt": "Subtitles", "arc": "Files", "odt": "Writer"}


def _op_ok(op) -> bool:
    return (op.prompt is None and not op.batch and not op.main_thread and op.min_files <= 1
            and op.mode in ("convert", "tools"))


def actions() -> list[tuple[str, str]]:
    """Every action a watch folder can run: (key, label) sorted by file kind."""
    out = []
    for op in REGISTRY:
        if _op_ok(op):
            out.append((f"op:{op.id}", action_label(f"op:{op.id}")))
    for pr in presets.PRESETS:
        out.append((f"preset:{pr['id']}", action_label(f"preset:{pr['id']}")))
    return sorted(out, key=lambda kv: kv[1].lower())


def action_label(key: str) -> str:
    kind, _, ident = key.partition(":")
    if kind == "preset":
        try:
            pr = presets.find(ident, None)
        except OpError:
            return key
        return f"{'Pictures' if pr['kind'] == 'image' else 'Videos'} → preset: {pr['name']}"
    op = next((o for o in REGISTRY if o.id == ident), None)
    if op is None:
        return key
    noun = KIND.get(ident.split(".")[0], ident.split(".")[0].upper())
    label = op.label.rstrip("…")
    if op.mode == "convert" and op.badge and not label.lower().startswith("to "):
        label = f"to {op.badge}"
    return f"{noun} → {label}"


def matches(key: str, path: Path) -> bool:
    kind, _, ident = key.partition(":")
    if kind == "preset":
        try:
            return presets.kind_of(path) == presets.find(ident, None)["kind"]
        except OpError:
            return False
    op = next((o for o in REGISTRY if o.id == ident), None)
    return op is not None and _op_ok(op) and op in ops_for([path], op.mode)


def run_rule(rule: dict, path: Path, ctx) -> list[Path]:
    """Run one rule on one file. Works on a hard link (or copy) inside the output folder, so the
    watched folder itself only ever sees the final results land in its subfolder."""
    dest = Path(rule["folder"]) / (rule.get("subfolder") or "Converted")
    work = dest / f".work-{uuid.uuid4().hex[:8]}"
    work.mkdir(parents=True, exist_ok=True)
    try:
        src = work / path.name
        try:
            os.link(path, src)
        except OSError:
            shutil.copy2(path, src)
        kind, _, ident = rule["action"].partition(":")
        if kind == "preset":
            made = [presets.apply(src, ident, ctx)]
        else:
            op = next(o for o in REGISTRY if o.id == ident)
            r = op.run(src, {}, ctx)
            made = r if isinstance(r, list) else [r] if r else []
        outs = []
        for m in made:
            m = Path(m)
            if m.resolve() == src.resolve():
                continue
            target = unique(dest / m.name)
            shutil.move(str(m), str(target))
            outs.append(target)
        return outs
    finally:
        shutil.rmtree(work, ignore_errors=True)


def _candidate(p: Path) -> bool:
    n = p.name
    return not (n.startswith(("~$", ".")) or p.suffix.lower() in PARTIAL or n.lower() in ("desktop.ini", "thumbs.db"))


class Watcher(QObject):
    """Emits ready(rule, path) once per new file, after its size stopped changing for ~2 s."""
    ready = Signal(dict, Path)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.fs = QFileSystemWatcher(self)
        self.fs.directoryChanged.connect(self._scan)
        self.rules: list[dict] = []
        self.seen: dict[str, set[str]] = {}
        self.pending: dict[str, tuple[int, float, int, dict]] = {}     # path -> (size, mtime, stable, rule)
        self.tick = QTimer(self)
        self.tick.timeout.connect(self._check)
        self.tick.start(1000)
        self.rescan = QTimer(self)                                      # safety net for missed events
        self.rescan.timeout.connect(lambda: [self._scan(r["folder"]) for r in self.rules])
        self.rescan.start(15000)

    def set_rules(self, rules: list[dict]):
        self.rules = [r for r in rules if r.get("enabled", True) and Path(r.get("folder", "")).is_dir()]
        if self.fs.directories():
            self.fs.removePaths(self.fs.directories())
        folders = sorted({os.path.normcase(str(Path(r["folder"]))) for r in self.rules})
        for f in folders:
            if f not in self.seen:
                self.seen[f] = {e.name for e in os.scandir(f) if e.is_file()}   # existing files are left alone
        self.seen = {f: s for f, s in self.seen.items() if f in folders}
        if folders:
            self.fs.addPaths(folders)
        self.pending = {k: v for k, v in self.pending.items() if os.path.normcase(str(Path(k).parent)) in folders}

    def _rules_for(self, folder: str) -> list[dict]:
        return [r for r in self.rules if os.path.normcase(str(Path(r["folder"]))) == folder]

    def _scan(self, folder: str):
        folder = os.path.normcase(str(Path(folder)))
        if folder not in self.seen:
            return
        try:
            names = {e.name for e in os.scandir(folder) if e.is_file()}
        except OSError:
            return
        new = names - self.seen[folder]
        self.seen[folder] = names
        for name in new:
            p = Path(folder) / name
            if not _candidate(p):
                continue
            for rule in self._rules_for(folder):
                if matches(rule["action"], p):
                    self.pending[str(p)] = (-1, 0.0, 0, rule)
                    break

    def _check(self):
        for key, (size, mtime, stable, rule) in list(self.pending.items()):
            p = Path(key)
            try:
                st = p.stat()
            except OSError:
                self.pending.pop(key, None)                  # deleted or renamed (e.g. a finished download)
                continue
            if st.st_size == size and st.st_mtime == mtime and st.st_size > 0:
                stable += 1
            else:
                stable = 0
            if stable >= 2 and self._unlocked(p):
                self.pending.pop(key, None)
                self.ready.emit(rule, p)
            else:
                self.pending[key] = (st.st_size, st.st_mtime, stable, rule)

    @staticmethod
    def _unlocked(p: Path) -> bool:
        try:
            with open(p, "rb+"):
                return True
        except PermissionError:
            return False
        except OSError:
            return True              # read-only files are fine to convert


def describe(rule: dict) -> str:
    return f"{rule['folder']}\n{action_label(rule['action'])}  →  {rule.get('subfolder') or 'Converted'}\\"

