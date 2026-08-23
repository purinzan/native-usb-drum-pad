"""Reach the CoreAudio device buffer, which SDL leaves alone.

SDL takes a chunk size and buffers to it, but never sets the device's own buffer
frame size, so it sits at whatever this process's HAL client defaults to -- 512
frames here -- no matter what the app asks for. PortAudio does set it, which is
why measurements taken through PortAudio described a device configured better
than the app ever configured it.

The buffer frame size is per client, not global: each process gets its own value
and the device runs to satisfy them all. Reading it from another process tells
you nothing about this one, which is worth knowing before trying to verify any
of this from the outside.

Everything here degrades to None off macOS or when a property is unavailable, so
callers can treat it as an optimisation rather than a dependency.
"""

import ctypes
import struct
import sys

IS_MACOS = sys.platform == "darwin"

_GLOBAL = None
_OUTPUT = None
_SYSTEM_OBJECT = 1

if IS_MACOS:
    coreaudio = ctypes.CDLL("/System/Library/Frameworks/CoreAudio.framework/CoreAudio")
    corefoundation = ctypes.CDLL(
        "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
    )

    def _fourcc(code):
        return struct.unpack(">I", code.encode())[0]

    class _Address(ctypes.Structure):
        _fields_ = [
            ("mSelector", ctypes.c_uint32),
            ("mScope", ctypes.c_uint32),
            ("mElement", ctypes.c_uint32),
        ]

    class _Range(ctypes.Structure):
        _fields_ = [("mMinimum", ctypes.c_double), ("mMaximum", ctypes.c_double)]

    _GLOBAL = _fourcc("glob")
    _OUTPUT = _fourcc("outp")

    coreaudio.AudioObjectGetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32), ctypes.c_void_p,
    ]
    coreaudio.AudioObjectSetPropertyData.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.c_uint32, ctypes.c_void_p,
    ]
    coreaudio.AudioObjectGetPropertyDataSize.argtypes = [
        ctypes.c_uint32, ctypes.POINTER(_Address), ctypes.c_uint32,
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_uint32),
    ]


def _get(obj, selector, scope, ctype=ctypes.c_uint32):
    address = _Address(_fourcc(selector), scope, 0)
    value = ctype()
    size = ctypes.c_uint32(ctypes.sizeof(ctype))
    status = coreaudio.AudioObjectGetPropertyData(
        obj, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(value)
    )
    return None if status else value.value


def _set(obj, selector, scope, value):
    address = _Address(_fourcc(selector), scope, 0)
    raw = ctypes.c_uint32(int(value))
    return coreaudio.AudioObjectSetPropertyData(
        obj, ctypes.byref(address), 0, None, 4, ctypes.byref(raw)
    )


def _array(obj, selector, scope, ctype):
    address = _Address(_fourcc(selector), scope, 0)
    size = ctypes.c_uint32()
    if coreaudio.AudioObjectGetPropertyDataSize(
        obj, ctypes.byref(address), 0, None, ctypes.byref(size)
    ):
        return []
    count = size.value // ctypes.sizeof(ctype)
    buffer = (ctype * count)()
    if coreaudio.AudioObjectGetPropertyData(
        obj, ctypes.byref(address), 0, None, ctypes.byref(size), buffer
    ):
        return []
    return list(buffer)


def _name(device):
    address = _Address(_fourcc("lnam"), _GLOBAL, 0)
    reference = ctypes.c_void_p()
    size = ctypes.c_uint32(ctypes.sizeof(reference))
    if coreaudio.AudioObjectGetPropertyData(
        device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(reference)
    ):
        return None
    corefoundation.CFStringGetCString.argtypes = [
        ctypes.c_void_p, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32
    ]
    buffer = ctypes.create_string_buffer(256)
    ok = corefoundation.CFStringGetCString(reference, buffer, 256, 0x08000100)
    corefoundation.CFRelease(reference)
    return buffer.value.decode("utf-8", "replace") if ok else None


def find_output(name=None):
    """The device object for `name`, or the current default output."""
    if not IS_MACOS:
        return None
    if not name:
        return _get(_SYSTEM_OBJECT, "dOut", _GLOBAL)
    for device in _array(_SYSTEM_OBJECT, "dev#", _GLOBAL, ctypes.c_uint32):
        if _name(device) == name and _array(device, "stm#", _OUTPUT, ctypes.c_uint32):
            return device
    return None


def buffer_frames(device):
    return _get(device, "fsiz", _OUTPUT) if device else None


def buffer_frame_range(device):
    if not device:
        return None
    address = _Address(_fourcc("fsz#"), _OUTPUT, 0)
    span = _Range()
    size = ctypes.c_uint32(ctypes.sizeof(span))
    if coreaudio.AudioObjectGetPropertyData(
        device, ctypes.byref(address), 0, None, ctypes.byref(size), ctypes.byref(span)
    ):
        return None
    return int(span.mMinimum), int(span.mMaximum)


def set_buffer_frames(device, frames):
    """Ask the device for `frames`, clamped to what it allows. Returns the result."""
    if not device:
        return None
    span = buffer_frame_range(device)
    if span:
        frames = max(span[0], min(span[1], int(frames)))
    if _set(device, "fsiz", _OUTPUT, frames):
        return None
    return buffer_frames(device)


def latency_breakdown(device):
    """Frames the device adds beyond its buffer, and the rate it runs at.

    safety offset plus device latency plus stream latency is the part no
    setting reaches; the buffer is the part that is worth arguing about.
    """
    if not device:
        return None
    rate = _get(device, "nsrt", _GLOBAL, ctypes.c_double)
    if not rate:
        return None
    streams = _array(device, "stm#", _OUTPUT, ctypes.c_uint32)
    fixed = (
        (_get(device, "saft", _OUTPUT) or 0)
        + (_get(device, "ltnc", _OUTPUT) or 0)
        + ((_get(streams[0], "ltnc", _GLOBAL) or 0) if streams else 0)
    )
    return {
        "rate": rate,
        "buffer_frames": buffer_frames(device) or 0,
        "fixed_frames": fixed,
        "buffer_ms": (buffer_frames(device) or 0) / rate * 1000.0,
        "fixed_ms": fixed / rate * 1000.0,
    }
