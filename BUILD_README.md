# FIT Forge — building the desktop app

## What you get

A single file, `FitForge.exe`. Double-click it and FIT Forge opens in its own
application window — no browser tab, no console, nothing to install on the
machine that runs it.

## Files in this build

| File | Purpose |
|---|---|
| `fit_app.py` | Desktop launcher — starts the engine, opens the window |
| `fit_server.py` | The local merge engine (HTTP server) |
| `fit_writer.py` | FIT file writer |
| `fit_forge.html` | The UI (gets baked into the .exe) |
| `FitForge.spec` | PyInstaller build recipe |
| `build.bat` | One-command build |
| `icon.ico` | *Optional* — drop one in and it's used automatically |

All of these must sit in the **same folder** to build.

## Build it

1. Put all the files above in one folder (e.g. `E:\Dev\github\Fit-Forge\`)
2. Double-click **`build.bat`**
3. Wait 1–3 minutes
4. Your app is at **`dist\FitForge.exe`**

That's it. Copy that one `.exe` anywhere — it runs on any Windows 10/11 machine
with no Python and no dependencies.

## Test before building

To check the window works before waiting on a full build:

```
pip install pywebview garmin-fit-sdk
python fit_app.py
```

If that opens the app window correctly, the build will too.

---

## Notes and gotchas

**WebView2 runtime** — the app window is powered by Microsoft Edge WebView2,
which is preinstalled on essentially all Windows 10/11 machines. On a rare
machine without it, the app shows a message box with the download link rather
than failing silently. If you sell this, worth mentioning in your system
requirements.

**Antivirus false positives** — PyInstaller one-file executables are sometimes
flagged by Windows SmartScreen or antivirus, because self-extracting binaries
look suspicious. This is normal and affects most indie tools. For a paid
product the real fix is a **code-signing certificate** (~$100–300/yr from
Sectigo/DigiCert); without one, buyers may see "Windows protected your PC" and
have to click "More info → Run anyway."

**AdGuard** — the app still talks to `127.0.0.1` internally, so if AdGuard's
"Filter localhost" is on, it can interfere the same way it did during
development. The port is now chosen dynamically, which helps, but if you
distribute this it's worth noting in a FAQ.

**File size** — expect roughly 40–80 MB. That's Python plus the FIT SDK bundled
inside. UPX compression is enabled in the spec to keep it down.

**Rebuilding after changes** — any time you edit `fit_forge.html`,
`fit_server.py`, or `fit_writer.py`, re-run `build.bat` to produce a new `.exe`.
The HTML is baked in at build time, not read from disk at runtime.

**Icon** — drop an `icon.ico` into the folder before building and it's picked up
automatically. (A `.png` won't work; it must be `.ico`.)
