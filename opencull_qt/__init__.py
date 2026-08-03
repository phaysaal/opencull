"""Darkimiya's native desktop interface."""

from __future__ import annotations


def describe_environment() -> list[str]:
    """Report what the toolkit sees, for when a window does not appear."""
    import os

    from PySide6.QtGui import QGuiApplication

    application = QGuiApplication.instance()
    lines = [
        f"pid {os.getpid()}",
        f"platform plugin: {application.platformName() if application else 'none'}",
        f"DISPLAY={os.environ.get('DISPLAY', 'unset')} "
        f"WAYLAND_DISPLAY={os.environ.get('WAYLAND_DISPLAY', 'unset')} "
        f"QT_QPA_PLATFORM={os.environ.get('QT_QPA_PLATFORM', 'unset')}",
    ]
    for screen in QGuiApplication.screens():
        marker = "*" if screen is QGuiApplication.primaryScreen() else " "
        geometry = screen.geometry()
        lines.append(
            f" {marker} {screen.name()}: {geometry.width()}x{geometry.height()}"
            f" at {geometry.x()},{geometry.y()}")
    return lines


def run(
    paths, argv: list[str] | None = None, diagnose: bool = False,
    screen: str = "",
) -> int:
    """Open the launcher and run until the window closes."""
    import sys

    from PySide6.QtWidgets import QApplication

    from opencull_desktop import build_desktop_services

    from . import theme

    application = QApplication.instance() or QApplication(argv or sys.argv[:1])
    application.setApplicationName("Darkimiya")
    application.setApplicationDisplayName("Darkimiya")
    application.setFont(theme.body(10))

    from .launcher import Launcher

    services = build_desktop_services(paths)
    window = Launcher(services, screen=screen)
    window.show()

    # Always say where the window went. A window on a monitor that is off, or
    # one the manager declined to map, is otherwise indistinguishable from the
    # application failing to start.
    geometry = window.frameGeometry()
    handle = window.windowHandle()
    screen = handle.screen().name() if handle and handle.screen() else "unknown"
    print(
        f"Darkimiya window: {geometry.width()}x{geometry.height()} at "
        f"{geometry.x()},{geometry.y()} on {screen}", flush=True)
    if diagnose:
        for line in describe_environment():
            print(f"  {line}", flush=True)

    try:
        return int(application.exec())
    finally:
        services.close()
