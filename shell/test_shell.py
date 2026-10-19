"""Test the Limewheel Explorer context-menu handler DLL end to end.

Checks:
  1. LoadLibrary the built DLL and confirm it exports DllGetClassObject and
     DllCanUnloadNow.
  2. shellreg.py: register (against a temp DLL copy) -> verify all HKCU keys
     exist -> unregister -> verify they are gone.
  3. COM activation: temporarily register the CLSID's InprocServer32 (HKCU,
     ThreadingModel Apartment) pointing at a temp copy of the DLL that sits next
     to a stub "Limewheel.exe" (built here with zig). Create the object with
     CoCreateInstance and check:
       - GetTitle() == "Limewheel"
       - GetFlags() has ECF_HASSUBCOMMANDS
       - EnumSubCommands() yields exactly "Convert...", "Tools...", "Actions..."
       - Invoke() on the "Tools..." sub-command with a 2-file IShellItemArray
         launches the stub, whose log shows `--mode tools --list <file>` and
         both file paths.
       - GetState() is ECS_ENABLED for a non-empty array, ECS_HIDDEN for empty.
  4. All HKCU registrations and temp files are removed at the end (try/finally).

Run:  python test_shell.py
Exit code 0 => all checks passed.

Uses ctypes vtable calls because IExplorerCommand is a custom interface that
pywin32 does not wrap.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import subprocess
import sys
import tempfile
import time
import winreg
from ctypes import (
    POINTER, WINFUNCTYPE, byref, c_bool, c_char_p, c_int, c_long, c_uint,
    c_ulong, c_void_p, c_wchar_p,
)
from pathlib import Path

HERE = Path(__file__).resolve().parent
DLL = HERE / "out" / "LimewheelShell.dll"
INSTALLER_DIR = HERE.parent / "installer"
CLSID = "{5C3E8F2A-7B41-4D9C-9A6E-1F0B2C7D8E91}"

# Interesting constants.
CLSCTX_INPROC_SERVER = 0x1
ECF_HASSUBCOMMANDS = 0x1
ECS_ENABLED = 0x0
ECS_HIDDEN = 0x2
PTR = ctypes.sizeof(c_void_p)

# --- result tracking -------------------------------------------------------
_results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    _results.append((name, bool(ok), detail))
    status = "PASS" if ok else "FAIL"
    line = f"[{status}] {name}"
    if detail:
        line += f"  ({detail})"
    print(line)


# --- ctypes COM plumbing ---------------------------------------------------
ole32 = ctypes.WinDLL("ole32")
shell32 = ctypes.WinDLL("shell32")

ole32.CoInitialize.argtypes = [c_void_p]
ole32.CoInitialize.restype = c_long
ole32.CoUninitialize.argtypes = []
ole32.CoUninitialize.restype = None
ole32.CoFreeUnusedLibraries.argtypes = []
ole32.CoFreeUnusedLibraries.restype = None
ole32.CoTaskMemFree.argtypes = [c_void_p]
ole32.CoTaskMemFree.restype = None
ole32.CoCreateInstance.argtypes = [c_void_p, c_void_p, c_ulong, c_void_p,
                                   POINTER(c_void_p)]
ole32.CoCreateInstance.restype = c_long


class GUID(ctypes.Structure):
    _fields_ = [("Data1", c_ulong), ("Data2", ctypes.c_ushort),
                ("Data3", ctypes.c_ushort), ("Data4", ctypes.c_ubyte * 8)]


ole32.IIDFromString.argtypes = [c_wchar_p, POINTER(GUID)]
ole32.IIDFromString.restype = c_long

shell32.SHParseDisplayName.argtypes = [c_wchar_p, c_void_p, POINTER(c_void_p),
                                       c_ulong, POINTER(c_ulong)]
shell32.SHParseDisplayName.restype = c_long
shell32.SHCreateShellItemArrayFromIDLists.argtypes = [c_uint, POINTER(c_void_p),
                                                      POINTER(c_void_p)]
shell32.SHCreateShellItemArrayFromIDLists.restype = c_long


def guid(s: str) -> GUID:
    g = GUID()
    hr = ole32.IIDFromString(s, byref(g))
    if hr != 0:
        raise OSError(f"IIDFromString({s}) failed: 0x{hr & 0xffffffff:08x}")
    return g


def _read_ptr(addr: int) -> int:
    return c_void_p.from_address(addr).value or 0


def com_call(this: int, index: int, argtypes, *args, restype=c_long):
    """Call method #index on the COM object at address `this`."""
    vtbl = _read_ptr(this)
    func_addr = _read_ptr(vtbl + index * PTR)
    proto = WINFUNCTYPE(restype, c_void_p, *argtypes)
    func = proto(func_addr)
    return func(this, *args)


def com_release(this: int) -> int:
    if not this:
        return 0
    return com_call(this, 2, [], restype=c_ulong)


# vtable indices
QI, ADDREF, RELEASE = 0, 1, 2
IEC_GETTITLE, IEC_GETICON, IEC_GETTOOLTIP = 3, 4, 5
IEC_GETCANON, IEC_GETSTATE, IEC_INVOKE = 6, 7, 8
IEC_GETFLAGS, IEC_ENUMSUB = 9, 10
ENUM_NEXT = 3


def get_title(this: int) -> str:
    out = c_void_p()
    hr = com_call(this, IEC_GETTITLE, [c_void_p, POINTER(c_void_p)], None,
                  byref(out))
    if hr != 0 or not out.value:
        raise OSError(f"GetTitle failed: 0x{hr & 0xffffffff:08x}")
    s = ctypes.wstring_at(out.value)
    ole32.CoTaskMemFree(out)
    return s


def get_flags(this: int) -> int:
    flags = c_ulong(0)
    hr = com_call(this, IEC_GETFLAGS, [POINTER(c_ulong)], byref(flags))
    if hr != 0:
        raise OSError(f"GetFlags failed: 0x{hr & 0xffffffff:08x}")
    return flags.value


def get_state(this: int, psia: int) -> int:
    state = c_ulong(0)
    hr = com_call(this, IEC_GETSTATE, [c_void_p, c_int, POINTER(c_ulong)],
                  psia, 1, byref(state))
    if hr != 0:
        raise OSError(f"GetState failed: 0x{hr & 0xffffffff:08x}")
    return state.value


def enum_sub(this: int) -> int:
    penum = c_void_p()
    hr = com_call(this, IEC_ENUMSUB, [POINTER(c_void_p)], byref(penum))
    if hr != 0 or not penum.value:
        raise OSError(f"EnumSubCommands failed: 0x{hr & 0xffffffff:08x}")
    return penum.value


def enum_next(penum: int) -> int:
    pcmd = c_void_p()
    fetched = c_ulong(0)
    hr = com_call(penum, ENUM_NEXT,
                  [c_ulong, POINTER(c_void_p), POINTER(c_ulong)],
                  1, byref(pcmd), byref(fetched))
    if hr < 0 or fetched.value == 0:
        return 0
    return pcmd.value or 0


def make_item_array(paths: list[str]) -> int:
    pidls = []
    for p in paths:
        pidl = c_void_p()
        hr = shell32.SHParseDisplayName(p, None, byref(pidl), 0, None)
        if hr != 0 or not pidl.value:
            for old in pidls:
                ole32.CoTaskMemFree(old)
            raise OSError(f"SHParseDisplayName({p}) failed: "
                          f"0x{hr & 0xffffffff:08x}")
        pidls.append(pidl)
    arr = (c_void_p * len(pidls))(*pidls)
    psia = c_void_p()
    hr = shell32.SHCreateShellItemArrayFromIDLists(len(pidls), arr, byref(psia))
    for pidl in pidls:
        ole32.CoTaskMemFree(pidl)
    if hr != 0 or not psia.value:
        raise OSError("SHCreateShellItemArrayFromIDLists failed: "
                      f"0x{hr & 0xffffffff:08x}")
    return psia.value


# --- stub exe --------------------------------------------------------------
STUB_C = r'''
#ifndef UNICODE
#define UNICODE
#endif
#include <windows.h>
#include <shellapi.h>

static void w8(HANDLE h, const wchar_t* ws) {
    if (!ws) return;
    int n = WideCharToMultiByte(CP_UTF8, 0, ws, -1, NULL, 0, NULL, NULL);
    if (n <= 1) return;
    char* buf = (char*)HeapAlloc(GetProcessHeap(), 0, n);
    if (!buf) return;
    WideCharToMultiByte(CP_UTF8, 0, ws, -1, buf, n, NULL, NULL);
    DWORD wr;
    WriteFile(h, buf, (DWORD)(n - 1), &wr, NULL);
    HeapFree(GetProcessHeap(), 0, buf);
}

int main(void) {
    LPWSTR cmdline = GetCommandLineW();
    int argc = 0;
    LPWSTR* argv = CommandLineToArgvW(cmdline, &argc);
    LPWSTR listPath = NULL;
    for (int i = 1; i < argc; i++) {
        if (lstrcmpW(argv[i], L"--list") == 0 && (i + 1) < argc)
            listPath = argv[i + 1];
    }
    wchar_t exePath[MAX_PATH];
    GetModuleFileNameW(NULL, exePath, MAX_PATH);
    wchar_t* slash = wcsrchr(exePath, L'\\');
    if (slash) *slash = 0;
    wchar_t logPath[MAX_PATH];
    wsprintfW(logPath, L"%s\\stub-log.txt", exePath);

    HANDLE h = CreateFileW(logPath, GENERIC_WRITE, 0, NULL, CREATE_ALWAYS,
                           FILE_ATTRIBUTE_NORMAL, NULL);
    if (h == INVALID_HANDLE_VALUE) return 1;
    w8(h, L"CMDLINE: ");
    w8(h, cmdline);
    w8(h, L"\n");
    if (listPath) {
        w8(h, L"LISTPATH: ");
        w8(h, listPath);
        w8(h, L"\nLISTBODY:\n");
        HANDLE lf = CreateFileW(listPath, GENERIC_READ, FILE_SHARE_READ, NULL,
                                OPEN_EXISTING, FILE_ATTRIBUTE_NORMAL, NULL);
        if (lf != INVALID_HANDLE_VALUE) {
            char buf[4096]; DWORD rd;
            while (ReadFile(lf, buf, sizeof(buf), &rd, NULL) && rd > 0) {
                DWORD wr; WriteFile(h, buf, rd, &wr, NULL);
            }
            CloseHandle(lf);
        }
    }
    CloseHandle(h);
    return 0;
}
'''


def build_stub(dest_exe: Path) -> None:
    src = dest_exe.with_name("stub.c")
    src.write_text(STUB_C, encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "ziglang", "cc",
         "-target", "x86_64-windows-gnu", "-O2",
         "-o", str(dest_exe), str(src), "-lshell32"],
        check=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
    )


# --- registry helpers (for the COM-activation section) ---------------------
def reg_inproc(dll_path: Path) -> None:
    key = r"Software\Classes\CLSID\%s\InprocServer32" % CLSID
    with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, key, 0,
                            winreg.KEY_SET_VALUE) as k:
        winreg.SetValueEx(k, "", 0, winreg.REG_SZ, str(dll_path))
        winreg.SetValueEx(k, "ThreadingModel", 0, winreg.REG_SZ, "Apartment")


def delete_clsid_tree() -> None:
    # Reuse shellreg's recursive delete via its private helper.
    sys.path.insert(0, str(INSTALLER_DIR))
    import shellreg
    shellreg._delete_tree(winreg.HKEY_CURRENT_USER,
                          r"Software\Classes\CLSID\%s" % CLSID)


def key_exists(path: str) -> bool:
    try:
        winreg.OpenKey(winreg.HKEY_CURRENT_USER, path).Close()
        return True
    except FileNotFoundError:
        return False


def value_of(path: str, name: str):
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, path) as k:
            return winreg.QueryValueEx(k, name)[0]
    except FileNotFoundError:
        return None


# --- sections --------------------------------------------------------------
def section_exports() -> None:
    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    k32.LoadLibraryW.argtypes = [c_wchar_p]
    k32.LoadLibraryW.restype = c_void_p
    k32.GetProcAddress.argtypes = [c_void_p, c_char_p]
    k32.GetProcAddress.restype = c_void_p
    k32.FreeLibrary.argtypes = [c_void_p]
    k32.FreeLibrary.restype = c_bool

    h = k32.LoadLibraryW(str(DLL))
    check("LoadLibrary(LimewheelShell.dll)", bool(h), str(DLL))
    if not h:
        return
    try:
        a1 = k32.GetProcAddress(h, b"DllGetClassObject")
        a2 = k32.GetProcAddress(h, b"DllCanUnloadNow")
        check("export DllGetClassObject", bool(a1))
        check("export DllCanUnloadNow", bool(a2))
    finally:
        k32.FreeLibrary(h)


def section_shellreg(dll_copy: Path) -> None:
    sys.path.insert(0, str(INSTALLER_DIR))
    import shellreg

    clsid_key = r"Software\Classes\CLSID\%s" % CLSID
    inproc_key = clsid_key + r"\InprocServer32"
    star_key = r"Software\Classes\*\shell\Limewheel"
    dir_key = r"Software\Classes\Directory\shell\Limewheel"
    exe = dll_copy.with_name("Limewheel.exe")

    try:
        shellreg.register(dll_copy)
        ok_inproc = (value_of(inproc_key, "") == str(dll_copy.resolve())
                     and value_of(inproc_key, "ThreadingModel") == "Apartment")
        check("shellreg InprocServer32 path + Apartment", ok_inproc,
              f"{value_of(inproc_key, '')}")
        for label, kp in (("*", star_key), ("Directory", dir_key)):
            ok = (value_of(kp, "ExplorerCommandHandler") == CLSID
                  and value_of(kp, "MUIVerb") == "Limewheel"
                  and value_of(kp, "Icon") == f"{exe},0"
                  and value_of(kp, "MultiSelectModel") == "Player")
            check(f"shellreg verb on {label}", ok,
                  f"Icon={value_of(kp, 'Icon')}")
    finally:
        shellreg.unregister()

    gone = (not key_exists(clsid_key) and not key_exists(star_key)
            and not key_exists(dir_key))
    check("shellreg unregister removed all keys", gone)


def _dll_list_dir() -> Path:
    """%TEMP%\\Limewheel exactly as the DLL computes it (GetTempPathW)."""
    buf = ctypes.create_unicode_buffer(261)
    ctypes.windll.kernel32.GetTempPathW(261, buf)
    return Path(buf.value) / "Limewheel"


def section_com(dll_copy: Path, stub_exe: Path, files: list[str]) -> None:
    ole32.CoInitialize(None)
    pcmd = 0
    penum = 0
    psia = 0
    subcmds: list[int] = []
    list_file: Path | None = None
    lw_dir = _dll_list_dir()
    lw_dir_existed = lw_dir.exists()
    try:
        reg_inproc(dll_copy)

        pcmd_v = c_void_p()
        iid = guid("{a08ce4d0-fa25-44ab-b57c-c7b1c323e0b9}")  # IExplorerCommand
        clsid = guid(CLSID)
        rc = ole32.CoCreateInstance(byref(clsid), None, CLSCTX_INPROC_SERVER,
                                    byref(iid), byref(pcmd_v))
        ok = (rc == 0 and bool(pcmd_v.value))
        check("CoCreateInstance(IExplorerCommand)", ok,
              f"hr=0x{rc & 0xffffffff:08x}")
        if not ok:
            return
        pcmd = pcmd_v.value

        title = get_title(pcmd)
        check('root GetTitle == "Limewheel"', title == "Limewheel",
              f"got {title!r}")

        flags = get_flags(pcmd)
        check("root GetFlags has ECF_HASSUBCOMMANDS",
              bool(flags & ECF_HASSUBCOMMANDS), f"flags=0x{flags:x}")

        # Enumerate sub-commands.
        penum = enum_sub(pcmd)
        titles: list[str] = []
        while True:
            sub = enum_next(penum)
            if not sub:
                break
            subcmds.append(sub)
            titles.append(get_title(sub))
        expected = ["Convert…", "Tools…", "Actions…"]
        check("EnumSubCommands yields 3 items", len(titles) == 3,
              f"got {len(titles)}")
        check("sub-command titles match", titles == expected,
              f"got {titles!r}")

        # GetState: ENABLED with files, HIDDEN with none (NULL array).
        psia = make_item_array(files)
        st = get_state(pcmd, psia)
        check("GetState ENABLED with files", st == ECS_ENABLED,
              f"state=0x{st:x}")
        st0 = get_state(pcmd, None)
        check("GetState HIDDEN with no items", st0 == ECS_HIDDEN,
              f"state=0x{st0:x}")

        # Invoke "Tools..." (sub-command index 1) with the two-file array.
        if len(subcmds) >= 2:
            tools = subcmds[1]
            tt = get_title(tools)
            check('sub-command #2 is "Tools..."', tt == "Tools…",
                  f"got {tt!r}")
            log = stub_exe.with_name("stub-log.txt")
            if log.exists():
                log.unlink()
            rc = com_call(tools, IEC_INVOKE, [c_void_p, c_void_p], psia, None)
            check("Invoke(Tools) returned S_OK", rc == 0,
                  f"hr=0x{rc & 0xffffffff:08x}")

            body = ""
            for _ in range(150):  # up to ~15s
                if log.exists():
                    body = log.read_text(encoding="utf-8", errors="replace")
                    if "LISTBODY:" in body and all(f in body for f in files):
                        break
                time.sleep(0.1)
            for line in body.splitlines():
                if line.startswith("LISTPATH: "):
                    list_file = Path(line[len("LISTPATH: "):].strip())
            check("stub launched and wrote log", bool(body), str(log))
            check("stub cmdline has --mode tools --list",
                  "--mode tools" in body and "--list" in body,
                  body.splitlines()[0] if body else "")
            check("stub list file contains both paths",
                  all(f in body for f in files), f"files={files}")
    except OSError as e:
        check("COM activation section", False, str(e))
    finally:
        # Release everything, drop the registration, let the DLL unload.
        for sub in subcmds:
            com_release(sub)
        if penum:
            com_release(penum)
        if psia:
            com_release(psia)
        if pcmd:
            com_release(pcmd)
        delete_clsid_tree()
        ole32.CoFreeUnusedLibraries()
        ole32.CoUninitialize()
        # Remove the list file the DLL wrote (and its folder if we created it).
        try:
            if list_file and list_file.exists():
                list_file.unlink()
            if (not lw_dir_existed and lw_dir.exists()
                    and not any(lw_dir.iterdir())):
                lw_dir.rmdir()
        except OSError:
            pass


def main() -> int:
    if not DLL.exists():
        print(f"ERROR: {DLL} not found. Build it first (build_shell.ps1).")
        return 2

    temp = Path(tempfile.mkdtemp(prefix="limewheel-shelltest-"))
    dll_copy = temp / "LimewheelShell.dll"
    stub_exe = temp / "Limewheel.exe"
    files = []
    try:
        shutil.copy2(DLL, dll_copy)
        build_stub(stub_exe)

        # Two real files to select.
        for i in (1, 2):
            f = temp / f"sample-{i}.txt"
            f.write_text(f"hello {i}\n", encoding="utf-8")
            files.append(str(f))

        print("=== Section 1: exports ===")
        section_exports()
        print("=== Section 2: shellreg register/unregister ===")
        section_shellreg(dll_copy)
        print("=== Section 3: COM activation + Invoke ===")
        section_com(dll_copy, stub_exe, files)
    finally:
        # Best-effort: the COM-loaded DLL copy may linger if Windows defers the
        # unload; everything else (registry) is already cleaned above.
        for attempt in range(5):
            shutil.rmtree(temp, ignore_errors=True)
            if not temp.exists():
                break
            time.sleep(0.3)
        # Final safety net on the registry (idempotent).
        try:
            delete_clsid_tree()
        except Exception:
            pass

    print("\n=== Summary ===")
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    for name, ok, detail in _results:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    print(f"\n{passed}/{total} checks passed.")
    return 0 if passed == total and total > 0 else 1


if __name__ == "__main__":
    sys.exit(main())
