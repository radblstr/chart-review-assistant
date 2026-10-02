# Deployment

Builds `ChartReviewAssistant-Setup.exe`, a per-user Windows installer (no admin rights). The
installer ships a complete Python 3.12 runtime with the app and its dependencies already installed
(pinned to `uv.lock`), native launchers, the config templates and third-party license notices.
Nothing is downloaded at install time or on launch.

## Build (developer)

Needs `uv`, Inno Setup 6 and the .NET Framework C# compiler, plus internet access (uv downloads
Python and the packages at build time). From this folder:

```
pwsh -File build_installer.ps1
```

Options: `-Uv <path>`, `-Iscc <path>`, `-Csc <path>` override the default tool locations (see the
script's `param` block). Output: `<repo>\dist\ChartReviewAssistant-Setup.exe`. The version comes from
`chart_review_assistant/__init__.py` `__version__`.

The script builds the wheel, installs a uv-managed Python 3.12 into `build\python-dl`, copies it to
`build\payload\python`, and installs the wheel and the `uv.lock`-pinned dependencies into that copy.

## What the installer does

- Installs to `%LOCALAPPDATA%\ChartReviewAssistant` with Desktop and Start-menu shortcuts named
  "Physics Chart Review Assistant", plus a Start-menu shortcut "Physics Chart Review Assistant
  (Demo)" to `ChartReviewAssistant-Demo.exe`, which runs the bundled demo (no Mosaiq access).
- Copies the runtime to `<app>\python`. An update deletes the old `python` folder first, so no
  files from an older version remain. If an install or update fails, run the same Setup.exe again.
- Removes the old first-launch layout (`venv`, `cache`, `install`, `uv.exe`, `provision.cmd`) left
  by earlier versions.
- `data\` under the install dir holds `db_config.toml`, `clinic_config.toml`, settings, logs and
  bug reports; it is kept across updates and on uninstall.

## Files

- `build_installer.ps1` — builds the runtime, stages the payload and runs ISCC.
- `installer.iss` — Inno Setup script.
- `files/launcher.cs` — the shortcut target; starts `python\pythonw.exe`. Compiled twice; the
  `-Demo.exe` copy sets `CRA_DEMO_MODE=1`.
- `files/ChartReviewAssistant-debug.cmd` — runs the app with a console for troubleshooting.
- `files/THIRD_PARTY_LICENSES.txt` — where the bundled Python and package licenses are.
