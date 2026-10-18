"""Archive creation/extraction. Extraction uses Windows' built-in bsdtar (handles ZIP/TAR/GZ/7Z/RAR)."""
from __future__ import annotations

import gzip
import shutil
import tarfile
import tempfile
import zipfile
from pathlib import Path

from .base import OpError, ext_of, out_dir, out_path, register, run_proc, stem_of, tar_exe, unique

ARCHIVES = frozenset("zip tar tgz gz rar 7z bz2 xz".split())


def _archive_name(paths: list[Path], ext: str) -> Path:
    if len(paths) == 1:
        return unique(paths[0].with_name(f"{paths[0].name if paths[0].is_dir() else stem_of(paths[0])}.{ext}"))
    return unique(paths[0].parent / f"{paths[0].parent.name or 'Archive'}.{ext}")


def _walk(paths):
    for p in paths:
        if p.is_dir():
            yield p, p.name
            for f in p.rglob("*"):
                yield f, f"{p.name}/{f.relative_to(p).as_posix()}"
        else:
            yield p, p.name


def make_zip(paths: list[Path], dst: Path) -> Path:
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for f, arc in _walk(paths):
            if f == dst:
                continue
            z.write(f, arc + ("/" if f.is_dir() else ""))
    return dst


def make_tar(paths: list[Path], dst: Path, mode: str) -> Path:
    with tarfile.open(dst, mode) as t:
        for p in paths:
            t.add(p, arcname=p.name)
    return dst


def extract(p: Path) -> Path:
    e = ext_of(p)
    if e == "gz" and not p.name.lower().endswith(".tar.gz"):
        try:
            with tarfile.open(p, "r:gz"):
                pass
        except tarfile.TarError:
            dst = unique(p.with_suffix(""))
            with gzip.open(p, "rb") as src, open(dst, "wb") as out:
                shutil.copyfileobj(src, out)
            return dst
    dest = out_dir(p)
    try:
        run_proc([tar_exe(), "-xf", p, "-C", dest])
    except OpError:
        shutil.rmtree(dest, ignore_errors=True)
        raise
    # Collapse "name/name/..." into "name/..."
    items = list(dest.iterdir())
    if len(items) == 1 and items[0].is_dir() and items[0].name == dest.name:
        tmp = dest.with_name(dest.name + ".tmp-limewheel")
        items[0].rename(tmp)
        dest.rmdir()
        tmp.rename(dest)
    return dest


def _repack(fmt):
    def run(p: Path, opts, ctx):
        with tempfile.TemporaryDirectory() as tmp:
            run_proc([tar_exe(), "-xf", p, "-C", tmp])
            items = sorted(Path(tmp).iterdir())
            dst = out_path(p, fmt)
            if fmt == "zip":
                with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
                    for f in Path(tmp).rglob("*"):
                        z.write(f, f.relative_to(tmp).as_posix())
            else:
                make_tar(items, dst, "w:gz")
        return dst
    return run


register("arc.zip", "Compress ZIP", "convert", None,
         lambda ps, o, c: make_zip(ps, _archive_name(ps, "zip")), badge="ZIP", batch=True, exclude=ARCHIVES)
register("arc.tgz", "Compress TAR.GZ", "convert", None,
         lambda ps, o, c: make_tar(ps, _archive_name(ps, "tar.gz"), "w:gz"), badge="TGZ", batch=True,
         exclude=ARCHIVES)
register("arc.extract", "Extract", "convert", ARCHIVES, lambda p, o, c: extract(p), badge="Open")
register("arc.tozip", "to ZIP", "convert", ARCHIVES - {"zip", "gz"}, _repack("zip"), badge="ZIP")
register("arc.totgz", "to TAR.GZ", "convert", ARCHIVES - {"tgz", "gz"}, _repack("tar.gz"), badge="TGZ")
