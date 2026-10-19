"""Verify a built DLL's PE import table and exports.

Confirms:
  * the DLL exports DllGetClassObject and DllCanUnloadNow (undecorated), and
  * every imported DLL is a known Windows system DLL (so the handler has no
    dependency on a redistributable C++ runtime etc.).

Usage: python -I check_imports.py <path-to-dll>
Dev-only dependency: pefile.
"""
import sys

try:
    import pefile
except ImportError:
    print("ERROR: pefile not installed (pip install pefile)")
    sys.exit(2)

# Windows system DLLs that are always present. MinGW/clang may emit imports
# against the UCRT (api-ms-win-crt-*) and kernel base forwarders; all are part
# of the OS on Windows 10+.
SYSTEM_PREFIXES = (
    "api-ms-win-",
    "ext-ms-win-",
)
SYSTEM_DLLS = {
    "kernel32.dll", "kernelbase.dll", "ole32.dll", "oleaut32.dll",
    "shell32.dll", "shlwapi.dll", "user32.dll", "advapi32.dll",
    "gdi32.dll", "msvcrt.dll", "ntdll.dll", "rpcrt4.dll", "sechost.dll",
    "combase.dll", "ucrtbase.dll", "bcrypt.dll", "win32u.dll",
    "ws2_32.dll", "crypt32.dll", "version.dll", "imm32.dll",
}

REQUIRED_EXPORTS = {"DllGetClassObject", "DllCanUnloadNow"}


def is_system(name: str) -> bool:
    low = name.lower()
    if low in SYSTEM_DLLS:
        return True
    return any(low.startswith(p) for p in SYSTEM_PREFIXES)


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: check_imports.py <dll>")
        return 2
    path = sys.argv[1]
    pe = pefile.PE(path, fast_load=True)
    pe.parse_data_directories(directories=[
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
        pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_EXPORT"],
    ])

    # Exports
    exports = set()
    if hasattr(pe, "DIRECTORY_ENTRY_EXPORT"):
        for exp in pe.DIRECTORY_ENTRY_EXPORT.symbols:
            if exp.name:
                exports.add(exp.name.decode("ascii", "replace"))
    print("Exports:", sorted(exports))
    missing = REQUIRED_EXPORTS - exports
    if missing:
        print("ERROR: missing required exports:", sorted(missing))
        return 1

    # Imports
    imported = []
    if hasattr(pe, "DIRECTORY_ENTRY_IMPORT"):
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            imported.append(entry.dll.decode("ascii", "replace"))
    print("Imported DLLs:")
    for dll in sorted(imported, key=str.lower):
        tag = "ok" if is_system(dll) else "NON-SYSTEM"
        print(f"  [{tag}] {dll}")

    nonsys = [d for d in imported if not is_system(d)]
    if nonsys:
        print("ERROR: non-system DLL dependencies:", nonsys)
        return 1

    print("OK: all imports are system DLLs; required exports present.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
