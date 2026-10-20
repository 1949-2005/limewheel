# Limewheel MSIX packaging

Builds an **unsigned** MSIX from a PyInstaller onedir build. The Microsoft Store
signs the package on ingestion, so no certificate is needed to submit.

## Build

```powershell
packaging\build_msix.ps1 `
    -AppDir  <onedir folder with Limewheel.exe + _internal> `
    -Icon    <square PNG, >= 512 px> `
    -Version 1.3.1.0
```

The icon PNG for the brand mark can be rendered from the app code (reads only):

```powershell
python -c "import os,sys; os.environ['LIMEWHEEL_BRAND']='limewheel'; from PySide6.QtWidgets import QApplication; a=QApplication(sys.argv); from limewheel.app import lime_pixmap; lime_pixmap(512).save(sys.argv[1])" out.png
```

What the script does:

1. Caches `makeappx.exe` + `signtool.exe` from the `Microsoft.Windows.SDK.BuildTools`
   NuGet package under `%LOCALAPPDATA%\limewheel-build` (best effort).
2. Builds `shell\out\LimewheelShell.dll` if missing.
3. Stages the app + the shell DLL + generated logo assets (`make_assets.py`) +
   the filled `AppxManifest.xml`.
4. Packs and validates the `.msix`, then writes it to `packaging\out\`.

Output: `packaging\out\Limewheel_<Version>_x64.msix`.

### Packing engine note (important for this machine)

`makeappx.exe` cannot run on a machine **without the Windows SDK installed**: it
depends on a side-by-side private assembly (`Microsoft.Windows.Build.Appx.AppxPackaging.dll`)
whose activation context fails to generate (`ERROR_SXS_CANT_GEN_ACTCTX`). The SDK
installer normally registers that assembly system-wide; the bare NuGet payload
does not, and registering it needs admin.

So `build_msix.ps1` tries `makeappx.exe` first and, when it cannot run, falls
back to `pack_msix.py`, which drives the **same** engine through Windows'
in-box COM classes (`System32\AppxPackaging.dll`):

1. `IAppxFactory::CreateManifestReader` validates the manifest;
2. a self-check confirms each `desktop5:Verb/@Clsid` matches a declared
   `com:Class/@Id` (the engine does not check this cross-reference);
3. `IAppxPackageWriter` packs (compressed) — its `Close()` validates the manifest
   and writes the block map, as `makeappx pack` does;
4. the result is reopened with `IAppxFactory::CreatePackageReader`.

If the writer ever fails, a built-in stored (uncompressed) OPC writer is used
instead and validated the same way. On a machine with the SDK, `makeappx` runs
and is used directly.

**OPC-reserved files.** MSIX is an OPC/zip package, so paths using `_rels\`, the
`.rels` extension, or a nested `[Content_Types].xml` are reserved and **cannot**
be in any MSIX (the engine rejects them on write and on read, and so would
makeappx). python-docx ships an unzipped template folder with such names; the
app only uses its packed `default.docx`, so `build.ps1` deletes the folder. If a
future dependency brings reserved names back, the packer leaves them out and
prints each one.

## Replace with Partner Center values before submitting

From Partner Center -> your app -> **Product identity**, copy these into the
build parameters (they overwrite the dev defaults):

| Build parameter          | Partner Center field          | Dev default        |
|--------------------------|-------------------------------|--------------------|
| `-IdentityName`          | Package/Identity/Name         | `Limewheel.Dev`    |
| `-Publisher`             | Package/Identity/Publisher    | `CN=LimewheelDev`  |
| `-PublisherDisplayName`  | Publisher display name        | `Limewheel`        |

`-Version` must be 4-part (e.g. `1.3.1.0`); the Store requires the revision
(last) field to be `0`.

## runFullTrust justification (paste into the submission)

The Store flags `rescap:runFullTrust` for review. Suggested text:

> Limewheel is a full-trust Win32 desktop app (PySide6/Python). It needs
> `runFullTrust` to: (1) install low-level keyboard and mouse hooks that detect
> the user's drag gesture plus a held modifier key, which is the core
> interaction for dropping a file onto the on-screen wheel; (2) launch bundled
> converter processes and pass them the selected file paths; and (3) provide the
> Windows Explorer context-menu handler (IExplorerCommand) that forwards the
> selected files to the running app. None of this is expressible under the
> restricted app-container capability set.

## Test locally WITHOUT the Store

The package is unsigned, so Windows will not install the `.msix` directly. Pick
one (both need your own action -- the build does not change system settings):

* **Register the staging layout (Developer Mode).** Enable Developer Mode
  yourself (Settings -> System -> For developers), then register the unpacked
  layout in place -- no signing required:

  ```powershell
  packaging\build_msix.ps1 -AppDir <dir> -Icon <png> -KeepStaging   # prints the staging path
  Add-AppxPackage -Register <staging>\AppxManifest.xml
  ```


* **Sign with a self-signed test certificate.** Create a test cert whose subject
  exactly matches the manifest `Publisher` (`CN=LimewheelDev`), trust it on the
  test machine (needs admin), then sign with the cached `signtool.exe`:

  ```powershell
  signtool sign /fd SHA256 /a /f test.pfx /p <pw> packaging\out\Limewheel_1.3.1.0_x64.msix
  Add-AppxPackage packaging\out\Limewheel_1.3.1.0_x64.msix
  ```

Neither is required for Store submission -- upload the unsigned `.msix` to
Partner Center and the Store signs it.

## Files

| File                        | Purpose                                             |
|-----------------------------|-----------------------------------------------------|
| `build_msix.ps1`            | End-to-end build driver.                             |
| `AppxManifest.template.xml` | Manifest with `{PLACEHOLDER}`s filled by the build.  |
| `make_assets.py`            | Generates every tile/logo asset from `-Icon`.        |
| `pack_msix.py`              | System-COM packer + validator (makeappx fallback).   |
