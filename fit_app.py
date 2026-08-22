"""
FIT Forge — desktop launcher.

Starts the local merge engine on a background thread and displays the UI in a
native application window (no browser tab, no console).

Run from source:   python fit_app.py
Build to .exe:     build.bat
"""

import sys
import os
import ctypes
import threading
import traceback

# ---------------------------------------------------------------------------
# In a windowed PyInstaller build (console=False) there is no console attached,
# so sys.stdout / sys.stderr are None. Any print() then raises AttributeError
# and can kill an otherwise-healthy operation. Give them a harmless sink.
# ---------------------------------------------------------------------------
class _NullStream:
    def write(self, *args, **kwargs):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False

    def fileno(self):
        raise OSError("no fileno")


if sys.stdout is None:
    sys.stdout = _NullStream()
if sys.stderr is None:
    sys.stderr = _NullStream()

APP_NAME = "FIT Forge"
WINDOW_W = 1180
WINDOW_H = 900
MIN_W = 900
MIN_H = 640


def _error_box(title, message):
    """Show a native message box (works even with no console attached)."""
    try:
        ctypes.windll.user32.MessageBoxW(0, str(message), str(title), 0x10)
    except Exception:
        # Non-Windows or ctypes unavailable — fall back to stderr.
        sys.stderr.write(f"{title}: {message}\n")


class Api:
    """Bridge exposed to the UI as window.pywebview.api."""

    def __init__(self):
        self._window = None

    def bind(self, window):
        self._window = window

    def save_fit(self, b64, suggested_name="merged_activity.fit"):
        """Save the merged FIT file, preferring a native Save As dialog.

        Returns:
          {"ok": True,  "path": "..."}        saved
          {"ok": False, "cancelled": True}    user closed the dialog
          {"ok": False, "error": "..."}       something went wrong
        """
        import base64
        import webview

        try:
            data = base64.b64decode(b64)
        except Exception as e:
            return {"ok": False, "error": f"Bad file data: {e}"}

        path = None

        # --- Try the native Save As dialog (API differs across pywebview versions)
        try:
            if self._window is not None:
                dialog_type = getattr(
                    getattr(webview, "FileDialog", None), "SAVE", None
                )
                if dialog_type is None:
                    dialog_type = webview.SAVE_DIALOG

                result = self._window.create_file_dialog(
                    dialog_type,
                    directory=os.path.join(os.path.expanduser("~"), "Downloads"),
                    save_filename=suggested_name,
                )
                if result:
                    path = result[0] if isinstance(result, (list, tuple)) else result
                else:
                    # Explicit, unambiguous cancel.
                    return {"ok": False, "cancelled": True}
        except Exception as e:
            print(f"[save_fit] dialog unavailable ({e}); using fallback path")
            path = None

        # --- Fallback: write straight to Downloads with a unique name
        if not path:
            downloads = os.path.join(os.path.expanduser("~"), "Downloads")
            if not os.path.isdir(downloads):
                downloads = os.path.expanduser("~")
            base, ext = os.path.splitext(suggested_name)
            path = os.path.join(downloads, suggested_name)
            n = 1
            while os.path.exists(path):
                path = os.path.join(downloads, f"{base}_{n}{ext}")
                n += 1

        if not str(path).lower().endswith(".fit"):
            path = str(path) + ".fit"

        try:
            with open(path, "wb") as f:
                f.write(data)
            print(f"[save_fit] wrote {len(data)} bytes -> {path}")
            return {"ok": True, "path": path}
        except Exception as e:
            return {"ok": False, "error": str(e)}


def main():
    try:
        import webview
    except ImportError:
        _error_box(
            APP_NAME,
            "pywebview is not installed.\n\nRun:\n    pip install pywebview",
        )
        return 1

    try:
        from fit_server import start_server
    except Exception as e:
        _error_box(APP_NAME, f"Could not load the merge engine:\n\n{e}")
        return 1

    # Start the local engine on a free port.
    try:
        server, port = start_server()
    except Exception as e:
        _error_box(APP_NAME, f"Could not start the local engine:\n\n{e}")
        return 1

    url = f"http://127.0.0.1:{port}"

    api = Api()

    window = webview.create_window(
        APP_NAME,
        url,
        width=WINDOW_W,
        height=WINDOW_H,
        min_size=(MIN_W, MIN_H),
        resizable=True,
        text_select=True,
        confirm_close=False,
        js_api=api,
    )
    api.bind(window)

    def _shutdown():
        try:
            server.shutdown()
        except Exception:
            pass

    # Stop the engine when the window closes so no process lingers.
    try:
        window.events.closed += _shutdown
    except Exception:
        pass

    try:
        # gui=None lets pywebview pick the best available backend
        # (EdgeChromium/WebView2 on Windows 10/11).
        webview.start()
    except Exception as e:
        _error_box(
            APP_NAME,
            "Could not open the application window.\n\n"
            f"{e}\n\n"
            "On Windows this usually means the Microsoft Edge WebView2 "
            "Runtime is missing. Install it from:\n"
            "https://developer.microsoft.com/microsoft-edge/webview2/",
        )
        return 1
    finally:
        _shutdown()

    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        _error_box(APP_NAME, "Unexpected error:\n\n" + traceback.format_exc())
        sys.exit(1)
