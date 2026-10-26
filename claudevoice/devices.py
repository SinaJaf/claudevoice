"""Follow the system's audio devices while the voice features run.

PortAudio (under sounddevice) snapshots the device list when it starts, so a
long-lived stream keeps using whatever was the default back then. These helpers
let the recorder and the speaker notice a change and reopen on the new device.
"""
from __future__ import annotations

import logging
import sys

log = logging.getLogger("claudevoice.devices")

_COREAUDIO = None


def _coreaudio_default(selector: str) -> int | None:
    """Read one CoreAudio system default device ID (microseconds per call)."""
    global _COREAUDIO
    if sys.platform != "darwin":
        return None
    try:
        import ctypes
        import ctypes.util

        if _COREAUDIO is None:
            _COREAUDIO = ctypes.CDLL(ctypes.util.find_library("CoreAudio"))

        class _Address(ctypes.Structure):
            _fields_ = [("selector", ctypes.c_uint32), ("scope", ctypes.c_uint32),
                        ("element", ctypes.c_uint32)]

        def fourcc(code: str) -> int:
            return int.from_bytes(code.encode(), "big")

        address = _Address(fourcc(selector), fourcc("glob"), 0)
        device = ctypes.c_uint32(0)
        size = ctypes.c_uint32(ctypes.sizeof(device))
        status = _COREAUDIO.AudioObjectGetPropertyData(
            1, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(device))
        return int(device.value) if status == 0 else None
    except Exception:  # noqa: BLE001
        return None


def default_output_id() -> int | None:
    """The system default output's ID on macOS; None where it cannot be read cheaply."""
    return _coreaudio_default("dOut")


def default_input_id() -> int | None:
    """The system default input's ID on macOS; None where it cannot be read cheaply."""
    return _coreaudio_default("dIn ")


def refresh() -> None:
    """Re-scan devices so newly plugged or newly default ones are visible.

    Every open stream in this process must be closed first.
    """
    import sounddevice as sd

    try:
        sd._terminate()
        sd._initialize()
    except Exception:  # noqa: BLE001 - keep the old list rather than fail
        log.exception("could not refresh the audio device list")
