"""Pack an MSIX from a staging directory with Windows' in-box packaging engine
(AppxPackaging.dll), and validate the result. Used by build_msix.ps1 when
makeappx.exe cannot run.

Why not makeappx.exe? On a machine without the Windows SDK installed, makeappx
(from the Microsoft.Windows.SDK.BuildTools NuGet package) fails to start: its
side-by-side private assembly "Microsoft.Windows.Build.Appx.AppxPackaging.dll"
cannot be activated (ERROR_SXS_CANT_GEN_ACTCTX). The same packaging engine,
however, ships with Windows (System32\\AppxPackaging.dll) with its COM classes
registered machine-wide, so this module drives it directly via ctypes.

Steps:
  1. IAppxFactory::CreateManifestReader validates AppxManifest.xml.
  2. A small self-check verifies every desktop5:Verb/@Clsid refers to a declared
     com:Class/@Id (the engine accepts a dangling reference, which would make
     the Explorer menu silently disappear).
  3. Pack with IAppxPackageWriter (compressed; Close() re-validates the manifest
     and writes the block map, exactly as makeappx pack does). If the writer
     fails, fall back to a built-in stored (uncompressed) OPC writer.
  4. Verify the expected parts are in the zip and reopen it with
     IAppxFactory::CreatePackageReader.

OPC-reserved paths ("_rels" folders, ".rels" parts, a nested
"[Content_Types].xml") cannot exist inside any MSIX - the engine rejects them on
write (ERROR_INVALID_NAME) and on read (APPX_E_INVALID_BLOCKMAP), and so would
makeappx. They are excluded with a warning; they typically come from a build
bundling an *unzipped* .docx/OOXML template, which should be shipped packed.

Usage:
    python -I pack_msix.py <staging_dir> <manifest_path> <out_msix>
"""
from __future__ import annotations

import base64
import ctypes
import hashlib
import os
import re
import string
import struct
import sys
import xml.etree.ElementTree as ET
import zipfile
import zlib
from ctypes import (
    POINTER, WINFUNCTYPE, byref, c_int, c_long, c_ulong, c_void_p, c_wchar_p,
)

BLOCK = 65536
SHA256_URI = "http://www.w3.org/2001/04/xmlenc#sha256"
CLSID_AppxFactory = "{5842A140-FF9F-4166-8F5C-62F5B7B0C781}"
IID_IAppxFactory = "{BEB94909-E451-438B-B5A7-D79E767B75D8}"

CONTENT_TYPES = {
    "dll": "application/x-msdownload",
    "exe": "application/x-msdownload",
    "pyd": "application/x-msdownload",
    "png": "image/png",
    "json": "application/json",
    "txt": "text/plain",
    "xml": "application/octet-stream",  # manifest/blockmap get overrides
}
# Already-compressed formats: store instead of deflating.
NO_COMPRESS = {"png", "jpg", "jpeg", "gif", "zip", "gz", "bz2", "xz", "7z",
               "whl", "mp3", "mp4"}


# ---------------------------------------------------------------------------
# Part collection
# ---------------------------------------------------------------------------
def _ext(arcname: str) -> str:
    base = arcname.rsplit("/", 1)[-1]
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _is_opc_reserved(arc: str) -> bool:
    segs = arc.split("/")
    if any(s.lower() == "_rels" for s in segs):
        return True
    base = segs[-1].lower()
    return base == "[content_types].xml" or base.endswith(".rels")


def collect_parts(staging: str):
    """Return (payload, skipped): payload is [(arcname, abspath)] using '/'
    separators, excluding AppxManifest.xml and OPC-reserved paths."""
    payload, skipped = [], []
    for root, _dirs, files in os.walk(staging):
        for f in files:
            full = os.path.join(root, f)
            rel = os.path.relpath(full, staging).replace("\\", "/")
            if rel.lower() == "appxmanifest.xml":
                continue
            (skipped if _is_opc_reserved(rel) else payload).append((rel, full))
    payload.sort(key=lambda t: t[0].lower())
    skipped = sorted(r for r, _ in skipped)
    return payload, skipped


# ---------------------------------------------------------------------------
# COM plumbing
# ---------------------------------------------------------------------------
ole32 = ctypes.WinDLL("ole32")
shlwapi = ctypes.WinDLL("shlwapi")
urlmon = ctypes.WinDLL("urlmon")
PTR = ctypes.sizeof(c_void_p)


class GUID(ctypes.Structure):
    _fields_ = [("d1", c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]


class APPX_PACKAGE_SETTINGS(ctypes.Structure):
    _fields_ = [("forceZip32", c_int), ("hashMethod", c_void_p)]


ole32.IIDFromString.argtypes = [c_wchar_p, POINTER(GUID)]
ole32.IIDFromString.restype = c_long
ole32.CoInitializeEx.argtypes = [c_void_p, c_ulong]
ole32.CoInitializeEx.restype = c_long
ole32.CoUninitialize.restype = None
ole32.CoCreateInstance.argtypes = [POINTER(GUID), c_void_p, c_ulong,
                                   POINTER(GUID), POINTER(c_void_p)]
ole32.CoCreateInstance.restype = c_long
shlwapi.SHCreateStreamOnFileEx.argtypes = [c_wchar_p, c_ulong, c_ulong, c_int,
                                           c_void_p, POINTER(c_void_p)]
shlwapi.SHCreateStreamOnFileEx.restype = c_long
urlmon.CreateUri.argtypes = [c_wchar_p, c_ulong, ctypes.c_size_t,
                             POINTER(c_void_p)]
urlmon.CreateUri.restype = c_long


def _hr(hr: int) -> str:
    return f"0x{hr & 0xffffffff:08x}"


def _guid(s):
    g = GUID()
    if ole32.IIDFromString(s, byref(g)):
        raise OSError(f"bad guid {s}")
    return g


def _rp(addr):
    return c_void_p.from_address(addr).value or 0


def _call(this, idx, argtypes, *args, restype=c_long):
    fn = _rp(_rp(this) + idx * PTR)
    return WINFUNCTYPE(restype, c_void_p, *argtypes)(fn)(this, *args)


def _release(this):
    if this:
        _call(this, 2, [], restype=c_ulong)


def _stream(path, write=False):
    # read: STGM_READ|STGM_SHARE_DENY_WRITE; write: READWRITE|CREATE|EXCLUSIVE
    mode = 0x2 | 0x1000 | 0x10 if write else 0x20
    s = c_void_p()
    hr = shlwapi.SHCreateStreamOnFileEx(path, mode, 0, write, None, byref(s))
    if hr or not s.value:
        raise OSError(f"stream {path}: {_hr(hr)}")
    return s.value


def _factory():
    fac = c_void_p()
    hr = ole32.CoCreateInstance(byref(_guid(CLSID_AppxFactory)), None, 1,
                                byref(_guid(IID_IAppxFactory)), byref(fac))
    if hr or not fac.value:
        raise OSError(f"CoCreateInstance(AppxFactory): {_hr(hr)}")
    return fac.value


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------
def validate_manifest(manifest_path: str) -> None:
    fac = _factory()
    stm = _stream(manifest_path)
    reader = c_void_p()
    hr = _call(fac, 5, [c_void_p, POINTER(c_void_p)], stm, byref(reader))
    for p in (reader.value, stm, fac):
        _release(p)
    if hr:
        raise OSError(f"AppxManifest.xml is invalid: {_hr(hr)}")
    print("pack_msix: manifest validated (IAppxFactory.CreateManifestReader)")


def check_manifest_guids(manifest_path: str) -> None:
    """Cross-reference check the engine does NOT do (verified: it accepts a
    desktop5:Verb/@Clsid that matches no com:Class/@Id)."""
    guid_re = re.compile(r"^[0-9A-Fa-f]{8}(-[0-9A-Fa-f]{4}){3}-[0-9A-Fa-f]{12}$")
    com_ns = "http://schemas.microsoft.com/appx/manifest/com/windows10"
    d5_ns = "http://schemas.microsoft.com/appx/manifest/desktop/windows10/5"
    root = ET.parse(manifest_path).getroot()
    class_ids = [e.get("Id", "") for e in root.iter(f"{{{com_ns}}}Class")]
    verb_ids = [e.get("Clsid", "") for e in root.iter(f"{{{d5_ns}}}Verb")]
    bad = [g for g in class_ids + verb_ids if not guid_re.match(g)]
    if bad:
        raise OSError(f"malformed GUID(s) in manifest: {bad}")
    declared = {g.lower() for g in class_ids}
    dangling = sorted({g for g in verb_ids if g.lower() not in declared})
    if dangling:
        raise OSError(f"desktop5:Verb Clsid not declared as com:Class: {dangling}")
    print(f"pack_msix: manifest GUIDs OK ({len(class_ids)} com:Class, "
          f"{len(verb_ids)} desktop5:Verb, cross-referenced)")


def validate_package(out_msix: str) -> None:
    fac = _factory()
    stm = _stream(out_msix)
    reader = c_void_p()
    hr = _call(fac, 4, [c_void_p, POINTER(c_void_p)], stm, byref(reader))
    for p in (reader.value, stm, fac):
        _release(p)
    if hr:
        raise OSError(f"package rejected by CreatePackageReader: {_hr(hr)}")
    print("pack_msix: package validated (IAppxFactory.CreatePackageReader)")


def verify_contents(out_msix: str) -> int:
    required = ("Limewheel.exe", "LimewheelShell.dll", "AppxManifest.xml",
                "AppxBlockMap.xml", "[Content_Types].xml")
    with zipfile.ZipFile(out_msix) as z:
        names = {n.replace("\\", "/") for n in z.namelist()}
    missing = [r for r in required if r not in names]
    if missing:
        raise OSError(f"package is missing {missing}")
    print(f"pack_msix: {len(names)} zip entries; exe, shell DLL, manifest, "
          "block map, content types present")
    return len(names)


# ---------------------------------------------------------------------------
# Packer 1: in-box IAppxPackageWriter (compressed; preferred)
# ---------------------------------------------------------------------------
def pack_with_writer(payload, manifest_path: str, out_msix: str) -> None:
    fac = _factory()
    uri, writer = c_void_p(), c_void_p()
    out = 0
    streams = []
    try:
        hr = urlmon.CreateUri(SHA256_URI, 0, 0, byref(uri))
        if hr:
            raise OSError(f"CreateUri: {_hr(hr)}")
        out = _stream(out_msix, write=True)
        settings = APPX_PACKAGE_SETTINGS(0, uri.value)
        hr = _call(fac, 3, [c_void_p, POINTER(APPX_PACKAGE_SETTINGS),
                            POINTER(c_void_p)], out, byref(settings),
                   byref(writer))
        if hr or not writer.value:
            raise OSError(f"CreatePackageWriter: {_hr(hr)}")
        for arc, full in payload:
            s = _stream(full)
            streams.append(s)
            e = _ext(arc)
            hr = _call(writer.value, 3,
                       [c_wchar_p, c_wchar_p, c_ulong, c_void_p],
                       arc.replace("/", "\\"),
                       CONTENT_TYPES.get(e, "application/octet-stream"),
                       0 if e in NO_COMPRESS else 1, s)
            if hr:
                raise OSError(f"AddPayloadFile({arc}): {_hr(hr)}")
        man = _stream(manifest_path)
        streams.append(man)
        hr = _call(writer.value, 4, [c_void_p], man)   # Close(): validates
        if hr:
            raise OSError(f"Close (manifest/package validation): {_hr(hr)}")
    finally:
        for s in streams:
            _release(s)
        _release(writer.value)
        _release(out)
        _release(uri.value)
        _release(fac)


# ---------------------------------------------------------------------------
# Packer 2: built-in stored OPC writer (uncompressed; fallback)
# ---------------------------------------------------------------------------
_UNRESERVED = set(string.ascii_letters + string.digits + "-._~")


def _opc_zip_name(arc: str) -> str:
    """ZIP item names are percent-encoded like the engine does
    ("libstdc++" -> "libstdc%2B%2B"); the block map keeps the raw name and
    LfhSize is computed from the encoded one."""
    return "".join(ch if ch == "/" or ch in _UNRESERVED
                   else "".join("%%%02X" % b for b in ch.encode("utf-8"))
                   for ch in arc)


def _xesc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


def pack_stored(payload, manifest_path: str, out_msix: str) -> None:
    parts = payload + [("AppxManifest.xml", manifest_path)]
    bm = ['<?xml version="1.0" encoding="UTF-8" standalone="no"?>',
          '<BlockMap xmlns="http://schemas.microsoft.com/appx/2010/blockmap" '
          f'HashMethod="{SHA256_URI}">']
    entries, exts, noext = [], set(), []
    for arc, full in parts:
        with open(full, "rb") as fh:
            data = fh.read()
        zname = arc if arc == "AppxManifest.xml" else _opc_zip_name(arc)
        lfh = 30 + len(zname.encode("utf-8"))
        name = _xesc(arc.replace("/", "\\"))
        if data:
            bm.append(f'<File Name="{name}" Size="{len(data)}" LfhSize="{lfh}">')
            for i in range(0, len(data), BLOCK):
                h = base64.b64encode(
                    hashlib.sha256(data[i:i + BLOCK]).digest()).decode()
                bm.append(f'<Block Hash="{h}" />')
            bm.append("</File>")
        else:
            bm.append(f'<File Name="{name}" Size="0" LfhSize="{lfh}" />')
        entries.append((zname, data))
        e = _ext(arc)
        if e:
            exts.add(e)
        else:
            noext.append(zname)
    bm.append("</BlockMap>")
    ct = ['<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
          '<Types xmlns="http://schemas.openxmlformats.org/package/2006/'
          'content-types">']
    ct += [f'<Default Extension="{_xesc(e)}" ContentType="'
           f'{CONTENT_TYPES.get(e, "application/octet-stream")}" />'
           for e in sorted(exts)]
    ct += ['<Override PartName="/AppxManifest.xml" '
           'ContentType="application/vnd.ms-appx.manifest+xml" />',
           '<Override PartName="/AppxBlockMap.xml" '
           'ContentType="application/vnd.ms-appx.blockmap+xml" />']
    ct += [f'<Override PartName="/{_xesc(p)}" '
           'ContentType="application/octet-stream" />' for p in noext]
    ct.append("</Types>")
    entries = ([("[Content_Types].xml", "\n".join(ct).encode())] + entries
               + [("AppxBlockMap.xml", "\n".join(bm).encode())])

    with open(out_msix, "wb") as fp:       # stored zip, fixed 1980 timestamp
        central = []
        for zname, data in entries:
            nb = zname.encode("utf-8")
            flag = 0x800 if any(b > 127 for b in nb) else 0
            crc = zlib.crc32(data) & 0xFFFFFFFF
            off = fp.tell()
            fp.write(struct.pack("<IHHHHHIIIHH", 0x04034B50, 20, flag, 0, 0,
                                 0x21, crc, len(data), len(data), len(nb), 0))
            fp.write(nb)
            fp.write(data)
            central.append((nb, flag, crc, len(data), off))
        cd = fp.tell()
        for nb, flag, crc, n, off in central:
            fp.write(struct.pack("<IHHHHHHIIIHHHHHII", 0x02014B50, 20, 20,
                                 flag, 0, 0, 0x21, crc, n, n, len(nb),
                                 0, 0, 0, 0, 0, off))
            fp.write(nb)
        end = fp.tell()
        fp.write(struct.pack("<IHHHHIIH", 0x06054B50, 0, 0, len(central),
                             len(central), end - cd, cd, 0))


# ---------------------------------------------------------------------------
def main() -> int:
    if len(sys.argv) != 4:
        print("usage: pack_msix.py <staging_dir> <manifest_path> <out_msix>")
        return 2
    staging, manifest_path, out_msix = sys.argv[1:4]

    payload, skipped = collect_parts(staging)
    if skipped:
        print(f"pack_msix: WARNING - excluded {len(skipped)} OPC-reserved "
              "path(s) that no MSIX may contain (ship them packed, not as an "
              "unzipped tree):")
        for s in skipped:
            print(f"    - {s}")

    ole32.CoInitializeEx(None, 2)  # COINIT_APARTMENTTHREADED
    try:
        validate_manifest(manifest_path)
        check_manifest_guids(manifest_path)
        if os.path.exists(out_msix):
            os.remove(out_msix)
        try:
            pack_with_writer(payload, manifest_path, out_msix)
            print("pack_msix: packed with IAppxPackageWriter (compressed)")
        except OSError as exc:
            print(f"pack_msix: writer failed ({exc}); using stored fallback")
            if os.path.exists(out_msix):
                os.remove(out_msix)
            pack_stored(payload, manifest_path, out_msix)
            print("pack_msix: packed with built-in stored writer")
        verify_contents(out_msix)
        validate_package(out_msix)
    finally:
        ole32.CoUninitialize()

    size = os.path.getsize(out_msix)
    print(f"pack_msix: {out_msix} ({size} bytes, {size / 1048576:.2f} MB, "
          f"{len(payload) + 1} block-mapped files)")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except OSError as exc:
        print(f"pack_msix: ERROR: {exc}")
        sys.exit(1)
