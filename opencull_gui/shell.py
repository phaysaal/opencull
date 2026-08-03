"""Open the launcher in a native window, or fall back to the browser.

The desktop launcher was a Tk window, which looks like a Tk window on every
platform. The interface is now an ordinary local page, so it can be hosted in
the system's own web view -- WebKit on macOS and GNOME, WebView2 on Windows --
and inherit the platform's rendering, fonts and window chrome.

pywebview is optional on purpose. When it is unavailable the same page opens
in the default browser, which is a smaller window manager but the identical
interface, so a missing dependency degrades the frame rather than the product.
"""

from __future__ import annotations

import sys
import threading
import webbrowser

WINDOW_TITLE = "Darkimiya"
MIN_WIDTH = 720
MIN_HEIGHT = 520


def native_window_available() -> tuple[bool, str]:
    """Report whether a native web view can be created here, and why not."""
    try:
        import webview  # noqa: F401
    except ImportError:
        return False, (
            "pywebview is not installed, so the launcher opens in your "
            "browser. Install it with: pip install pywebview"
        )
    if sys.platform.startswith("linux"):
        try:
            import gi

            gi.require_version("Gtk", "3.0")
        except (ImportError, ValueError):
            return False, (
                "pywebview is installed but its GTK backend is not. Install "
                "python3-gi and gir1.2-webkit2-4.1, or use the browser."
            )
    return True, ""


def open_launcher(url: str, *, on_close=None, prefer_native: bool = True) -> str:
    """Show the launcher. Returns the shell that was actually used.

    Blocks for the lifetime of the window when a native one is created, which
    is what makes it usable as an application's main loop.
    """
    available, _reason = native_window_available()
    if prefer_native and available:
        import webview

        window = webview.create_window(
            WINDOW_TITLE,
            url,
            width=1040,
            height=720,
            min_size=(MIN_WIDTH, MIN_HEIGHT),
            # The page paints its own background before first paint; matching
            # it here avoids a white flash while WebKit starts.
            background_color="#131211",
            text_select=False,
        )
        if on_close is not None:
            window.events.closed += on_close
        webview.start()
        return "native"

    # Opening in a thread keeps a slow browser launch from delaying the caller.
    threading.Thread(
        target=webbrowser.open, args=(url,), daemon=True).start()
    return "browser"
