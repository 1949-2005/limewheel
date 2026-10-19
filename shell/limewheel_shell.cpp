// limewheel_shell.cpp
//
// Limewheel Explorer context-menu handler (IExplorerCommand), in-proc COM
// server. Plain C++ (no ATL/WRL). Exports DllGetClassObject and
// DllCanUnloadNow (see limewheel_shell.def).
//
// Top-level command "Limewheel" has three sub-commands: "Convert...",
// "Tools...", "Actions...". Invoking a sub-command writes the selected
// file-system paths to a UTF-8 list file under %TEMP%\Limewheel and launches
// the sibling Limewheel.exe with:
//     "<dir>\Limewheel.exe" --mode <mode> --list "<listfile>"
//
// CLSID {5C3E8F2A-7B41-4D9C-9A6E-1F0B2C7D8E91}

#ifndef UNICODE
#define UNICODE
#endif
#ifndef _UNICODE
#define _UNICODE
#endif

#include <windows.h>
#include <shobjidl.h>
#include <shlwapi.h>
#include <shlobj.h>
#include <objbase.h>
#include <string>
#include <vector>
#include <new>

// ---------------------------------------------------------------------------
// Module globals
// ---------------------------------------------------------------------------

static HMODULE g_hModule = nullptr;
static LONG    g_cRefModule = 0;   // live objects
static LONG    g_cLockModule = 0;  // explicit server locks

static void DllAddRef()  { InterlockedIncrement(&g_cRefModule); }
static void DllRelease() { InterlockedDecrement(&g_cRefModule); }

// CLSID {5C3E8F2A-7B41-4D9C-9A6E-1F0B2C7D8E91}
static const CLSID CLSID_LimewheelCommand =
    { 0x5c3e8f2a, 0x7b41, 0x4d9c, { 0x9a, 0x6e, 0x1f, 0x0b, 0x2c, 0x7d, 0x8e, 0x91 } };

// IIDs we answer for. Defined locally so we do not rely on an import library
// providing them (keeps the static link clean).
static const IID IID_IUnknown_L =
    { 0x00000000, 0x0000, 0x0000, { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const IID IID_IClassFactory_L =
    { 0x00000001, 0x0000, 0x0000, { 0xc0, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x46 } };
static const IID IID_IExplorerCommand_L =
    { 0xa08ce4d0, 0xfa25, 0x44ab, { 0xb5, 0x7c, 0xc7, 0xb1, 0xc3, 0x23, 0xe0, 0xb9 } };
static const IID IID_IEnumExplorerCommand_L =
    { 0xa88826f8, 0x186f, 0x4987, { 0xaa, 0xde, 0xea, 0x0c, 0xef, 0x8f, 0xbf, 0xe8 } };
static const IID IID_IObjectWithSite_L =
    { 0xfc4801a3, 0x2ba9, 0x11cf, { 0xa2, 0x29, 0x00, 0xaa, 0x00, 0x3d, 0x73, 0x52 } };

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

// Directory containing this DLL (no trailing slash).
static std::wstring DllDir()
{
    wchar_t buf[MAX_PATH];
    DWORD n = GetModuleFileNameW(g_hModule, buf, MAX_PATH);
    if (n == 0 || n >= MAX_PATH)
        return std::wstring();
    std::wstring p(buf, n);
    size_t slash = p.find_last_of(L"\\/");
    if (slash != std::wstring::npos)
        p.erase(slash);
    return p;
}

// Duplicate a wide string into a COM-task-allocated buffer via SHStrDupW.
static HRESULT CloneString(const wchar_t* src, wchar_t** out)
{
    if (!out)
        return E_POINTER;
    return SHStrDupW(src ? src : L"", out);
}

// True when at least one item exposes SIGDN_FILESYSPATH. Stops at the first match: Explorer
// calls GetState for every right-click, so this must stay fast even for huge selections.
static bool HasFileSystemItem(IShellItemArray* psia)
{
    if (!psia)
        return false;
    DWORD count = 0;
    if (FAILED(psia->GetCount(&count)) || count == 0)
        return false;

    for (DWORD i = 0; i < count; ++i)
    {
        IShellItem* item = nullptr;
        if (SUCCEEDED(psia->GetItemAt(i, &item)) && item)
        {
            LPWSTR path = nullptr;
            bool found = SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path)) && path;
            if (path)
                CoTaskMemFree(path);
            item->Release();
            if (found)
                return true;
        }
    }
    return false;
}

// Collect SIGDN_FILESYSPATH of each item (skipping non-filesystem ones).
static std::vector<std::wstring> CollectPaths(IShellItemArray* psia)
{
    std::vector<std::wstring> paths;
    if (!psia)
        return paths;
    DWORD count = 0;
    if (FAILED(psia->GetCount(&count)) || count == 0)
        return paths;

    for (DWORD i = 0; i < count; ++i)
    {
        IShellItem* item = nullptr;
        if (SUCCEEDED(psia->GetItemAt(i, &item)) && item)
        {
            LPWSTR path = nullptr;
            if (SUCCEEDED(item->GetDisplayName(SIGDN_FILESYSPATH, &path)) && path)
            {
                paths.emplace_back(path);
                CoTaskMemFree(path);
            }
            item->Release();
        }
    }
    return paths;
}

// Convert a wide string to UTF-8 bytes.
static std::string ToUtf8(const std::wstring& ws)
{
    if (ws.empty())
        return std::string();
    int n = WideCharToMultiByte(CP_UTF8, 0, ws.c_str(), (int)ws.size(),
                                nullptr, 0, nullptr, nullptr);
    if (n <= 0)
        return std::string();
    std::string out((size_t)n, '\0');
    WideCharToMultiByte(CP_UTF8, 0, ws.c_str(), (int)ws.size(),
                        &out[0], n, nullptr, nullptr);
    return out;
}

// Write the paths (UTF-8, no BOM, \n separated) to
// %TEMP%\Limewheel\open-<pid>-<tick>.txt and return its full path (empty on
// failure).
static std::wstring WriteListFile(const std::vector<std::wstring>& paths)
{
    wchar_t tmp[MAX_PATH];
    DWORD n = GetTempPathW(MAX_PATH, tmp);
    if (n == 0 || n >= MAX_PATH)
        return std::wstring();

    std::wstring dir(tmp);
    if (!dir.empty() && dir.back() != L'\\')
        dir.push_back(L'\\');
    dir += L"Limewheel";
    // Create the folder (ignore "already exists").
    CreateDirectoryW(dir.c_str(), nullptr);

    // Note: wsprintfW (user32) supports the I64 size prefix, not the C99
    // "ll" modifier, so GetTickCount64 (64-bit) must use %I64u.
    wchar_t name[128];
    wsprintfW(name, L"\\open-%lu-%I64u.txt",
              (unsigned long)GetCurrentProcessId(),
              (unsigned long long)GetTickCount64());
    std::wstring full = dir + name;

    HANDLE h = CreateFileW(full.c_str(), GENERIC_WRITE, 0, nullptr,
                           CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, nullptr);
    if (h == INVALID_HANDLE_VALUE)
        return std::wstring();

    std::string body;
    for (size_t i = 0; i < paths.size(); ++i)
    {
        body += ToUtf8(paths[i]);
        body.push_back('\n');
    }
    if (!body.empty())
    {
        DWORD written = 0;
        WriteFile(h, body.data(), (DWORD)body.size(), &written, nullptr);
    }
    CloseHandle(h);
    return full;
}

// Launch Limewheel.exe for the given mode with the written list file.
static void LaunchLimewheel(const wchar_t* mode, IShellItemArray* psia)
{
    std::vector<std::wstring> paths = CollectPaths(psia);
    if (paths.empty())
        return;

    std::wstring listFile = WriteListFile(paths);
    if (listFile.empty())
        return;

    std::wstring dir = DllDir();
    if (dir.empty())
        return;
    std::wstring exe = dir + L"\\Limewheel.exe";

    // Command line: "exe" --mode <mode> --list "<listfile>"
    std::wstring cmd;
    cmd += L"\"";
    cmd += exe;
    cmd += L"\" --mode ";
    cmd += mode;
    cmd += L" --list \"";
    cmd += listFile;
    cmd += L"\"";

    // Let the (already running or new) Limewheel take the foreground.
    AllowSetForegroundWindow(ASFW_ANY);

    std::vector<wchar_t> mutableCmd(cmd.begin(), cmd.end());
    mutableCmd.push_back(L'\0');

    STARTUPINFOW si;
    ZeroMemory(&si, sizeof(si));
    si.cb = sizeof(si);
    PROCESS_INFORMATION pi;
    ZeroMemory(&pi, sizeof(pi));

    BOOL ok = CreateProcessW(
        exe.c_str(),          // application name
        mutableCmd.data(),    // command line (mutable)
        nullptr, nullptr, FALSE,
        CREATE_NO_WINDOW,
        nullptr,
        dir.c_str(),          // working directory = DLL dir
        &si, &pi);

    if (ok)
    {
        CloseHandle(pi.hThread);
        CloseHandle(pi.hProcess);
    }
}

// ---------------------------------------------------------------------------
// SubCommand: a leaf IExplorerCommand ("Convert...", "Tools...", "Actions...")
// ---------------------------------------------------------------------------

class SubCommand : public IExplorerCommand
{
public:
    SubCommand(const wchar_t* title, const wchar_t* mode)
        : m_cRef(1), m_title(title), m_mode(mode) { DllAddRef(); }

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv)
            return E_POINTER;
        if (IsEqualIID(riid, IID_IUnknown_L) ||
            IsEqualIID(riid, IID_IExplorerCommand_L))
        {
            *ppv = static_cast<IExplorerCommand*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override
    {
        return (ULONG)InterlockedIncrement(&m_cRef);
    }
    IFACEMETHODIMP_(ULONG) Release() override
    {
        LONG c = InterlockedDecrement(&m_cRef);
        if (c == 0)
            delete this;
        return (ULONG)c;
    }

    // IExplorerCommand
    IFACEMETHODIMP GetTitle(IShellItemArray*, LPWSTR* ppszName) override
    {
        return CloneString(m_title.c_str(), ppszName);
    }
    IFACEMETHODIMP GetIcon(IShellItemArray*, LPWSTR* ppszIcon) override
    {
        std::wstring icon = DllDir();
        if (icon.empty())
            return E_FAIL;
        icon += L"\\Limewheel.exe,0";
        return CloneString(icon.c_str(), ppszIcon);
    }
    IFACEMETHODIMP GetToolTip(IShellItemArray*, LPWSTR* ppszInfotip) override
    {
        if (ppszInfotip)
            *ppszInfotip = nullptr;
        return E_NOTIMPL;
    }
    IFACEMETHODIMP GetCanonicalName(GUID* pguid) override
    {
        if (pguid)
            *pguid = GUID_NULL;
        return S_OK;
    }
    IFACEMETHODIMP GetState(IShellItemArray* psia, BOOL, EXPCMDSTATE* pState) override
    {
        if (!pState)
            return E_POINTER;
        *pState = HasFileSystemItem(psia) ? ECS_ENABLED : ECS_HIDDEN;
        return S_OK;
    }
    IFACEMETHODIMP Invoke(IShellItemArray* psia, IBindCtx*) override
    {
        LaunchLimewheel(m_mode.c_str(), psia);
        return S_OK;  // never block or surface UI
    }
    IFACEMETHODIMP GetFlags(EXPCMDFLAGS* pFlags) override
    {
        if (!pFlags)
            return E_POINTER;
        *pFlags = ECF_DEFAULT;
        return S_OK;
    }
    IFACEMETHODIMP EnumSubCommands(IEnumExplorerCommand** ppEnum) override
    {
        if (ppEnum)
            *ppEnum = nullptr;
        return E_NOTIMPL;
    }

private:
    ~SubCommand() { DllRelease(); }
    LONG         m_cRef;
    std::wstring m_title;
    std::wstring m_mode;
};

// ---------------------------------------------------------------------------
// EnumCommands: IEnumExplorerCommand over the three sub-commands
// ---------------------------------------------------------------------------

class EnumCommands : public IEnumExplorerCommand
{
public:
    EnumCommands() : m_cRef(1), m_index(0) { DllAddRef(); }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv)
            return E_POINTER;
        if (IsEqualIID(riid, IID_IUnknown_L) ||
            IsEqualIID(riid, IID_IEnumExplorerCommand_L))
        {
            *ppv = static_cast<IEnumExplorerCommand*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override
    {
        return (ULONG)InterlockedIncrement(&m_cRef);
    }
    IFACEMETHODIMP_(ULONG) Release() override
    {
        LONG c = InterlockedDecrement(&m_cRef);
        if (c == 0)
            delete this;
        return (ULONG)c;
    }

    IFACEMETHODIMP Next(ULONG celt, IExplorerCommand** rg, ULONG* pFetched) override
    {
        ULONG fetched = 0;
        while (fetched < celt && m_index < kCount)
        {
            IExplorerCommand* cmd = MakeAt(m_index);
            if (!cmd)
                break;
            rg[fetched] = cmd;
            ++fetched;
            ++m_index;
        }
        if (pFetched)
            *pFetched = fetched;
        return (fetched == celt) ? S_OK : S_FALSE;
    }
    IFACEMETHODIMP Skip(ULONG celt) override
    {
        m_index += celt;
        if (m_index > kCount)
            m_index = kCount;
        return S_OK;
    }
    IFACEMETHODIMP Reset() override
    {
        m_index = 0;
        return S_OK;
    }
    IFACEMETHODIMP Clone(IEnumExplorerCommand** ppenum) override
    {
        if (!ppenum)
            return E_POINTER;
        EnumCommands* e = new (std::nothrow) EnumCommands();
        if (!e)
            return E_OUTOFMEMORY;
        e->m_index = m_index;
        *ppenum = e;
        return S_OK;
    }

private:
    ~EnumCommands() { DllRelease(); }

    static IExplorerCommand* MakeAt(ULONG i)
    {
        switch (i)
        {
        case 0: return new (std::nothrow) SubCommand(L"Convert…", L"convert");
        case 1: return new (std::nothrow) SubCommand(L"Tools…",   L"tools");
        case 2: return new (std::nothrow) SubCommand(L"Actions…", L"actions");
        default: return nullptr;
        }
    }

    static const ULONG kCount = 3;
    LONG  m_cRef;
    ULONG m_index;
};

// ---------------------------------------------------------------------------
// RootCommand: top-level "Limewheel" with sub-commands. Also IObjectWithSite.
// ---------------------------------------------------------------------------

class RootCommand : public IExplorerCommand, public IObjectWithSite
{
public:
    RootCommand() : m_cRef(1), m_site(nullptr) { DllAddRef(); }

    // IUnknown
    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv)
            return E_POINTER;
        if (IsEqualIID(riid, IID_IUnknown_L) ||
            IsEqualIID(riid, IID_IExplorerCommand_L))
        {
            *ppv = static_cast<IExplorerCommand*>(this);
            AddRef();
            return S_OK;
        }
        if (IsEqualIID(riid, IID_IObjectWithSite_L))
        {
            *ppv = static_cast<IObjectWithSite*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override
    {
        return (ULONG)InterlockedIncrement(&m_cRef);
    }
    IFACEMETHODIMP_(ULONG) Release() override
    {
        LONG c = InterlockedDecrement(&m_cRef);
        if (c == 0)
            delete this;
        return (ULONG)c;
    }

    // IExplorerCommand
    IFACEMETHODIMP GetTitle(IShellItemArray*, LPWSTR* ppszName) override
    {
        return CloneString(L"Limewheel", ppszName);
    }
    IFACEMETHODIMP GetIcon(IShellItemArray*, LPWSTR* ppszIcon) override
    {
        std::wstring icon = DllDir();
        if (icon.empty())
            return E_FAIL;
        icon += L"\\Limewheel.exe,0";
        return CloneString(icon.c_str(), ppszIcon);
    }
    IFACEMETHODIMP GetToolTip(IShellItemArray*, LPWSTR* ppszInfotip) override
    {
        if (ppszInfotip)
            *ppszInfotip = nullptr;
        return E_NOTIMPL;
    }
    IFACEMETHODIMP GetCanonicalName(GUID* pguid) override
    {
        if (pguid)
            *pguid = GUID_NULL;
        return S_OK;
    }
    IFACEMETHODIMP GetState(IShellItemArray* psia, BOOL, EXPCMDSTATE* pState) override
    {
        if (!pState)
            return E_POINTER;
        *pState = HasFileSystemItem(psia) ? ECS_ENABLED : ECS_HIDDEN;
        return S_OK;
    }
    IFACEMETHODIMP Invoke(IShellItemArray*, IBindCtx*) override
    {
        // Parent of a submenu is not itself invoked.
        return S_OK;
    }
    IFACEMETHODIMP GetFlags(EXPCMDFLAGS* pFlags) override
    {
        if (!pFlags)
            return E_POINTER;
        *pFlags = ECF_HASSUBCOMMANDS;
        return S_OK;
    }
    IFACEMETHODIMP EnumSubCommands(IEnumExplorerCommand** ppEnum) override
    {
        if (!ppEnum)
            return E_POINTER;
        *ppEnum = new (std::nothrow) EnumCommands();
        return *ppEnum ? S_OK : E_OUTOFMEMORY;
    }

    // IObjectWithSite (trivial)
    IFACEMETHODIMP SetSite(IUnknown* pUnkSite) override
    {
        if (m_site)
            m_site->Release();
        m_site = pUnkSite;
        if (m_site)
            m_site->AddRef();
        return S_OK;
    }
    IFACEMETHODIMP GetSite(REFIID riid, void** ppv) override
    {
        if (!ppv)
            return E_POINTER;
        if (m_site)
            return m_site->QueryInterface(riid, ppv);
        *ppv = nullptr;
        return E_FAIL;
    }

private:
    ~RootCommand()
    {
        if (m_site)
            m_site->Release();
        DllRelease();
    }
    LONG      m_cRef;
    IUnknown* m_site;
};

// ---------------------------------------------------------------------------
// ClassFactory
// ---------------------------------------------------------------------------

class ClassFactory : public IClassFactory
{
public:
    ClassFactory() : m_cRef(1) { DllAddRef(); }

    IFACEMETHODIMP QueryInterface(REFIID riid, void** ppv) override
    {
        if (!ppv)
            return E_POINTER;
        if (IsEqualIID(riid, IID_IUnknown_L) ||
            IsEqualIID(riid, IID_IClassFactory_L))
        {
            *ppv = static_cast<IClassFactory*>(this);
            AddRef();
            return S_OK;
        }
        *ppv = nullptr;
        return E_NOINTERFACE;
    }
    IFACEMETHODIMP_(ULONG) AddRef() override
    {
        return (ULONG)InterlockedIncrement(&m_cRef);
    }
    IFACEMETHODIMP_(ULONG) Release() override
    {
        LONG c = InterlockedDecrement(&m_cRef);
        if (c == 0)
            delete this;
        return (ULONG)c;
    }

    IFACEMETHODIMP CreateInstance(IUnknown* pUnkOuter, REFIID riid, void** ppv) override
    {
        if (!ppv)
            return E_POINTER;
        *ppv = nullptr;
        if (pUnkOuter)
            return CLASS_E_NOAGGREGATION;
        RootCommand* cmd = new (std::nothrow) RootCommand();
        if (!cmd)
            return E_OUTOFMEMORY;
        HRESULT hr = cmd->QueryInterface(riid, ppv);
        cmd->Release();
        return hr;
    }
    IFACEMETHODIMP LockServer(BOOL fLock) override
    {
        if (fLock)
            InterlockedIncrement(&g_cLockModule);
        else
            InterlockedDecrement(&g_cLockModule);
        return S_OK;
    }

private:
    ~ClassFactory() { DllRelease(); }
    LONG m_cRef;
};

// ---------------------------------------------------------------------------
// Exports
// ---------------------------------------------------------------------------

extern "C" HRESULT __stdcall DllGetClassObject(REFCLSID rclsid, REFIID riid, void** ppv)
{
    if (!ppv)
        return E_POINTER;
    *ppv = nullptr;
    if (!IsEqualCLSID(rclsid, CLSID_LimewheelCommand))
        return CLASS_E_CLASSNOTAVAILABLE;

    ClassFactory* factory = new (std::nothrow) ClassFactory();
    if (!factory)
        return E_OUTOFMEMORY;
    HRESULT hr = factory->QueryInterface(riid, ppv);
    factory->Release();
    return hr;
}

extern "C" HRESULT __stdcall DllCanUnloadNow(void)
{
    return (g_cRefModule == 0 && g_cLockModule == 0) ? S_OK : S_FALSE;
}

BOOL WINAPI DllMain(HINSTANCE hinst, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_hModule = (HMODULE)hinst;
        DisableThreadLibraryCalls(hinst);
    }
    return TRUE;
}
