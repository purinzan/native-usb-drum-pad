"""Platform backends for MIDI I/O, thread priority, and single-instance locking.

Windows drives the WinMM MIDI API through ctypes; macOS drives CoreMIDI the same
way. Both expose the same MidiInput/MidiOutput surface, including the device
index model that the app persists in its settings file.
"""

import ctypes
import os
import platform
import struct
import sys
import tempfile
import time

IS_WINDOWS = sys.platform == "win32"
IS_MACOS = sys.platform == "darwin"

AUDIO_DRIVER = "wasapi" if IS_WINDOWS else "coreaudio" if IS_MACOS else ""

CALLBACK_FUNCTION = 0x00030000
MIM_DATA = 0x3C3
MMSYSERR_NOERROR = 0

ERROR_ALREADY_EXISTS = 183
ABOVE_NORMAL_PRIORITY_CLASS = 0x00008000
THREAD_PRIORITY_HIGHEST = 2
SINGLE_INSTANCE_NAME = "Local\\NativeUsbDrumPad.SingleInstance"
SINGLE_INSTANCE_LOCK = "native-usb-drum-pad.lock"


class MidiInputBase:
    """Shared MIDI decoding so every backend queues identical audio events."""

    def __init__(self, device_id, event_queue):
        self.device_id = int(device_id)
        self.event_queue = event_queue

    def _emit(self, status, data1, data2, received_ns):
        if status in (0xF8, 0xFA, 0xFB, 0xFC):
            self.event_queue.put(("MIDI_CLOCK", status, received_ns))
            return
        command = status & 0xF0
        if command == 0x90:
            event_type = "MIDI" if data2 > 0 else "MIDI_OFF"
            self.event_queue.put((event_type, "N", data1, data2, received_ns))
        elif command == 0x80:
            self.event_queue.put(("MIDI_OFF", "N", data1, data2, received_ns))
        elif command == 0xB0 and data2 > 0:
            self.event_queue.put(("MIDI", "CC", data1, data2, received_ns))
        elif command == 0xC0:
            self.event_queue.put(("MIDI", "PC", data1, 127, received_ns))


class _NullMidiInput(MidiInputBase):
    """Fallback so the UI still runs on platforms without a MIDI backend."""

    def __init__(self, device_id, event_queue):
        raise RuntimeError(f"MIDI input is not supported on {sys.platform}")

    @staticmethod
    def devices():
        return []

    def close(self):
        pass


class _NullMidiOutput:
    def __init__(self, device_id):
        raise RuntimeError(f"MIDI output is not supported on {sys.platform}")

    @staticmethod
    def devices():
        return []

    def send(self, status):
        pass

    def close(self):
        pass


if IS_WINDOWS:

    class MIDIINCAPS(ctypes.Structure):
        _fields_ = [
            ("wMid", ctypes.c_ushort),
            ("wPid", ctypes.c_ushort),
            ("vDriverVersion", ctypes.c_uint),
            ("szPname", ctypes.c_wchar * 32),
            ("dwSupport", ctypes.c_uint),
        ]

    class MIDIOUTCAPS(ctypes.Structure):
        _fields_ = [
            ("wMid", ctypes.c_ushort), ("wPid", ctypes.c_ushort),
            ("vDriverVersion", ctypes.c_uint), ("szPname", ctypes.c_wchar * 32),
            ("wTechnology", ctypes.c_ushort), ("wVoices", ctypes.c_ushort),
            ("wNotes", ctypes.c_ushort), ("wChannelMask", ctypes.c_ushort),
            ("dwSupport", ctypes.c_uint),
        ]

    MidiCallback = ctypes.WINFUNCTYPE(
        None,
        ctypes.c_void_p,
        ctypes.c_uint,
        ctypes.c_size_t,
        ctypes.c_size_t,
        ctypes.c_size_t,
    )

    winmm = ctypes.WinDLL("winmm")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    avrt = ctypes.WinDLL("avrt", use_last_error=True)

    winmm.midiInGetNumDevs.restype = ctypes.c_uint
    winmm.midiInGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(MIDIINCAPS), ctypes.c_uint]
    winmm.midiInGetDevCapsW.restype = ctypes.c_uint
    winmm.midiInOpen.argtypes = [
        ctypes.POINTER(ctypes.c_void_p),
        ctypes.c_uint,
        MidiCallback,
        ctypes.c_size_t,
        ctypes.c_uint,
    ]
    winmm.midiInOpen.restype = ctypes.c_uint
    winmm.midiInStart.argtypes = [ctypes.c_void_p]
    winmm.midiInStart.restype = ctypes.c_uint
    winmm.midiInStop.argtypes = [ctypes.c_void_p]
    winmm.midiInStop.restype = ctypes.c_uint
    winmm.midiInClose.argtypes = [ctypes.c_void_p]
    winmm.midiInClose.restype = ctypes.c_uint
    winmm.midiOutGetNumDevs.restype = ctypes.c_uint
    winmm.midiOutGetDevCapsW.argtypes = [ctypes.c_size_t, ctypes.POINTER(MIDIOUTCAPS), ctypes.c_uint]
    winmm.midiOutGetDevCapsW.restype = ctypes.c_uint
    winmm.midiOutOpen.argtypes = [ctypes.POINTER(ctypes.c_void_p), ctypes.c_uint, ctypes.c_size_t, ctypes.c_size_t, ctypes.c_uint]
    winmm.midiOutOpen.restype = ctypes.c_uint
    winmm.midiOutShortMsg.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    winmm.midiOutShortMsg.restype = ctypes.c_uint
    winmm.midiOutClose.argtypes = [ctypes.c_void_p]
    winmm.midiOutClose.restype = ctypes.c_uint

    kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel32.CreateMutexW.restype = ctypes.c_void_p
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    kernel32.CloseHandle.restype = ctypes.c_bool
    kernel32.GetCurrentProcess.restype = ctypes.c_void_p
    kernel32.GetCurrentThread.restype = ctypes.c_void_p
    kernel32.SetPriorityClass.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    kernel32.SetPriorityClass.restype = ctypes.c_bool
    kernel32.SetThreadPriority.argtypes = [ctypes.c_void_p, ctypes.c_int]
    kernel32.SetThreadPriority.restype = ctypes.c_bool

    avrt.AvSetMmThreadCharacteristicsW.argtypes = [ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint)]
    avrt.AvSetMmThreadCharacteristicsW.restype = ctypes.c_void_p
    avrt.AvRevertMmThreadCharacteristics.argtypes = [ctypes.c_void_p]
    avrt.AvRevertMmThreadCharacteristics.restype = ctypes.c_bool

    class WinMidiInput(MidiInputBase):
        def __init__(self, device_id, event_queue):
            super().__init__(device_id, event_queue)
            self.handle = ctypes.c_void_p()
            self.callback = MidiCallback(self._callback)
            rc = winmm.midiInOpen(
                ctypes.byref(self.handle),
                ctypes.c_uint(device_id),
                self.callback,
                0,
                CALLBACK_FUNCTION,
            )
            if rc != MMSYSERR_NOERROR:
                raise RuntimeError(f"midiInOpen rc={rc}")
            rc = winmm.midiInStart(self.handle)
            if rc != MMSYSERR_NOERROR:
                self.close()
                raise RuntimeError(f"midiInStart rc={rc}")

        @staticmethod
        def devices():
            devices = []
            count = winmm.midiInGetNumDevs()
            for device_id in range(count):
                caps = MIDIINCAPS()
                rc = winmm.midiInGetDevCapsW(
                    ctypes.c_size_t(device_id),
                    ctypes.byref(caps),
                    ctypes.sizeof(caps),
                )
                if rc == MMSYSERR_NOERROR:
                    devices.append((device_id, caps.szPname))
            return devices

        def _callback(self, _handle, message, _instance, param1, _param2):
            if message != MIM_DATA:
                return
            received_ns = time.perf_counter_ns()
            data = int(param1)
            self._emit(data & 0xFF, (data >> 8) & 0xFF, (data >> 16) & 0xFF, received_ns)

        def close(self):
            if self.handle:
                try:
                    winmm.midiInStop(self.handle)
                except Exception:
                    pass
                try:
                    winmm.midiInClose(self.handle)
                except Exception:
                    pass
                self.handle = None

    class WinMidiOutput:
        def __init__(self, device_id):
            self.device_id = int(device_id)
            self.handle = ctypes.c_void_p()
            rc = winmm.midiOutOpen(ctypes.byref(self.handle), self.device_id, 0, 0, 0)
            if rc != MMSYSERR_NOERROR:
                raise RuntimeError(f"midiOutOpen rc={rc}")

        @staticmethod
        def devices():
            devices = []
            for device_id in range(winmm.midiOutGetNumDevs()):
                caps = MIDIOUTCAPS()
                rc = winmm.midiOutGetDevCapsW(device_id, ctypes.byref(caps), ctypes.sizeof(caps))
                if rc == MMSYSERR_NOERROR:
                    devices.append((device_id, caps.szPname))
            return devices

        def send(self, status):
            if self.handle:
                rc = winmm.midiOutShortMsg(self.handle, int(status) & 0xFF)
                if rc != MMSYSERR_NOERROR:
                    raise RuntimeError(f"midiOutShortMsg rc={rc}")

        def close(self):
            if self.handle:
                winmm.midiOutClose(self.handle)
                self.handle = None

    MidiInput = WinMidiInput
    MidiOutput = WinMidiOutput

    def acquire_single_instance():
        ctypes.set_last_error(0)
        mutex = kernel32.CreateMutexW(None, False, SINGLE_INSTANCE_NAME)
        if not mutex:
            raise ctypes.WinError(ctypes.get_last_error())
        if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
            kernel32.CloseHandle(mutex)
            return None
        return mutex

    def release_single_instance(token):
        if token:
            kernel32.CloseHandle(token)

    def enable_process_priority():
        kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), ABOVE_NORMAL_PRIORITY_CLASS)

    def enable_audio_thread_priority():
        task_index = ctypes.c_uint()
        task_handle = avrt.AvSetMmThreadCharacteristicsW("Pro Audio", ctypes.byref(task_index))
        kernel32.SetThreadPriority(kernel32.GetCurrentThread(), THREAD_PRIORITY_HIGHEST)
        return task_handle

    def release_audio_thread_priority(token):
        if token:
            avrt.AvRevertMmThreadCharacteristics(token)


elif IS_MACOS:

    _COREMIDI_PATH = "/System/Library/Frameworks/CoreMIDI.framework/CoreMIDI"
    _COREFOUNDATION_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"

    coremidi = ctypes.CDLL(_COREMIDI_PATH)
    corefoundation = ctypes.CDLL(_COREFOUNDATION_PATH)
    libc = ctypes.CDLL(None, use_errno=True)

    _CFStringRef = ctypes.c_void_p
    _MIDIObjectRef = ctypes.c_uint32
    _ItemCount = ctypes.c_ulong
    _OSStatus = ctypes.c_int32
    _MIDIReadProc = ctypes.CFUNCTYPE(None, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)

    _CF_UTF8 = 0x08000100

    corefoundation.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
    corefoundation.CFStringCreateWithCString.restype = _CFStringRef
    corefoundation.CFStringGetCString.argtypes = [_CFStringRef, ctypes.c_char_p, ctypes.c_long, ctypes.c_uint32]
    corefoundation.CFStringGetCString.restype = ctypes.c_bool
    corefoundation.CFRelease.argtypes = [ctypes.c_void_p]
    corefoundation.CFRelease.restype = None

    coremidi.MIDIClientCreate.argtypes = [_CFStringRef, ctypes.c_void_p, ctypes.c_void_p, ctypes.POINTER(_MIDIObjectRef)]
    coremidi.MIDIClientCreate.restype = _OSStatus
    coremidi.MIDIClientDispose.argtypes = [_MIDIObjectRef]
    coremidi.MIDIClientDispose.restype = _OSStatus
    coremidi.MIDIInputPortCreate.argtypes = [
        _MIDIObjectRef, _CFStringRef, _MIDIReadProc, ctypes.c_void_p, ctypes.POINTER(_MIDIObjectRef)
    ]
    coremidi.MIDIInputPortCreate.restype = _OSStatus
    coremidi.MIDIOutputPortCreate.argtypes = [_MIDIObjectRef, _CFStringRef, ctypes.POINTER(_MIDIObjectRef)]
    coremidi.MIDIOutputPortCreate.restype = _OSStatus
    coremidi.MIDIPortConnectSource.argtypes = [_MIDIObjectRef, _MIDIObjectRef, ctypes.c_void_p]
    coremidi.MIDIPortConnectSource.restype = _OSStatus
    coremidi.MIDIPortDisconnectSource.argtypes = [_MIDIObjectRef, _MIDIObjectRef]
    coremidi.MIDIPortDisconnectSource.restype = _OSStatus
    coremidi.MIDIPortDispose.argtypes = [_MIDIObjectRef]
    coremidi.MIDIPortDispose.restype = _OSStatus
    coremidi.MIDIGetNumberOfSources.argtypes = []
    coremidi.MIDIGetNumberOfSources.restype = _ItemCount
    coremidi.MIDIGetSource.argtypes = [_ItemCount]
    coremidi.MIDIGetSource.restype = _MIDIObjectRef
    coremidi.MIDIGetNumberOfDestinations.argtypes = []
    coremidi.MIDIGetNumberOfDestinations.restype = _ItemCount
    coremidi.MIDIGetDestination.argtypes = [_ItemCount]
    coremidi.MIDIGetDestination.restype = _MIDIObjectRef
    coremidi.MIDIObjectGetStringProperty.argtypes = [_MIDIObjectRef, _CFStringRef, ctypes.POINTER(_CFStringRef)]
    coremidi.MIDIObjectGetStringProperty.restype = _OSStatus
    coremidi.MIDISend.argtypes = [_MIDIObjectRef, _MIDIObjectRef, ctypes.c_void_p]
    coremidi.MIDISend.restype = _OSStatus

    _kMIDIPropertyDisplayName = ctypes.c_void_p.in_dll(coremidi, "kMIDIPropertyDisplayName")
    _kMIDIPropertyName = ctypes.c_void_p.in_dll(coremidi, "kMIDIPropertyName")

    # MIDIPacket is timeStamp(8) + length(2) + data[], packed to 4 bytes on 64-bit.
    # MIDIPacketNext also rounds the next packet up to a 4-byte boundary on ARM.
    _PACKET_HEADER = 10
    _PACKET_ALIGNMENT = 4 if platform.machine().startswith("arm") else 0

    def _cfstring(text):
        return corefoundation.CFStringCreateWithCString(None, text.encode("utf-8"), _CF_UTF8)

    def _endpoint_name(endpoint):
        for key in (_kMIDIPropertyDisplayName, _kMIDIPropertyName):
            value = _CFStringRef()
            if coremidi.MIDIObjectGetStringProperty(endpoint, key, ctypes.byref(value)) or not value:
                continue
            buffer = ctypes.create_string_buffer(512)
            copied = corefoundation.CFStringGetCString(value, buffer, ctypes.sizeof(buffer), _CF_UTF8)
            corefoundation.CFRelease(value)
            if copied and buffer.value:
                return buffer.value.decode("utf-8", "replace")
        return None

    def _endpoints(count_call, get_call):
        endpoints = []
        for index in range(int(count_call())):
            endpoint = get_call(index)
            if endpoint:
                endpoints.append((index, endpoint, _endpoint_name(endpoint) or f"MIDI {index}"))
        return endpoints

    _client = _MIDIObjectRef()

    def _shared_client():
        """One CoreMIDI client is enough for every port this app opens."""
        global _client
        if not _client.value:
            name = _cfstring("Native USB Drum Pad")
            try:
                rc = coremidi.MIDIClientCreate(name, None, None, ctypes.byref(_client))
            finally:
                corefoundation.CFRelease(name)
            if rc:
                _client = _MIDIObjectRef()
                raise RuntimeError(f"MIDIClientCreate rc={rc}")
        return _client

    class _MidiStreamParser:
        """Rebuilds complete messages from CoreMIDI packet bytes.

        Packets carry a raw byte stream, so this tracks running status, ignores
        SysEx payloads, and passes realtime bytes through immediately.
        """

        _LENGTHS = {0x80: 3, 0x90: 3, 0xA0: 3, 0xB0: 3, 0xC0: 2, 0xD0: 2, 0xE0: 3}

        def __init__(self, emit):
            self.emit = emit
            self.status = 0
            self.expected = 0
            self.data = []
            self.in_sysex = False

        def feed(self, payload, received_ns):
            for byte in payload:
                if byte >= 0xF8:
                    self.emit(byte, 0, 0, received_ns)
                elif byte >= 0xF0:
                    self.in_sysex = byte == 0xF0
                    self.status = 0
                    self.expected = 0
                    self.data = []
                elif byte >= 0x80:
                    self.in_sysex = False
                    self.status = byte
                    self.expected = self._LENGTHS.get(byte & 0xF0, 0)
                    self.data = []
                elif self.in_sysex or not self.status:
                    continue
                else:
                    self.data.append(byte)
                    if len(self.data) >= self.expected - 1:
                        data1 = self.data[0]
                        data2 = self.data[1] if len(self.data) > 1 else 0
                        self.data = []
                        self.emit(self.status, data1, data2, received_ns)

    class CoreMidiInput(MidiInputBase):
        def __init__(self, device_id, event_queue):
            super().__init__(device_id, event_queue)
            self.parser = _MidiStreamParser(self._emit)
            self.port = _MIDIObjectRef()
            self.endpoint = 0
            # ctypes callbacks are collected with their owner, so keep a reference.
            self.read_proc = _MIDIReadProc(self._read)

            sources = self.endpoints()
            match = next((item for item in sources if item[0] == self.device_id), None)
            if match is None:
                raise RuntimeError(f"MIDI source {self.device_id} is not connected")
            self.endpoint = match[1]

            client = _shared_client()
            name = _cfstring("Native USB Drum Pad Input")
            try:
                rc = coremidi.MIDIInputPortCreate(
                    client, name, self.read_proc, None, ctypes.byref(self.port)
                )
            finally:
                corefoundation.CFRelease(name)
            if rc:
                self.port = _MIDIObjectRef()
                raise RuntimeError(f"MIDIInputPortCreate rc={rc}")

            rc = coremidi.MIDIPortConnectSource(self.port, self.endpoint, None)
            if rc:
                self.close()
                raise RuntimeError(f"MIDIPortConnectSource rc={rc}")

        @staticmethod
        def endpoints():
            return _endpoints(coremidi.MIDIGetNumberOfSources, coremidi.MIDIGetSource)

        @staticmethod
        def devices():
            return [(index, name) for index, _endpoint, name in CoreMidiInput.endpoints()]

        def _read(self, packet_list, _read_ref, _source_ref):
            received_ns = time.perf_counter_ns()
            if not packet_list:
                return
            try:
                count = int.from_bytes(ctypes.string_at(packet_list, 4), sys.byteorder)
                offset = 4
                for _ in range(count):
                    header = ctypes.string_at(packet_list + offset, _PACKET_HEADER)
                    length = int.from_bytes(header[8:10], sys.byteorder)
                    if length:
                        payload = ctypes.string_at(packet_list + offset + _PACKET_HEADER, length)
                        self.parser.feed(payload, received_ns)
                    end = packet_list + offset + _PACKET_HEADER + length
                    if _PACKET_ALIGNMENT:
                        end = (end + _PACKET_ALIGNMENT - 1) & ~(_PACKET_ALIGNMENT - 1)
                    offset = end - packet_list
            except Exception:
                # This runs on a CoreMIDI thread; an exception here would be fatal.
                pass

        def close(self):
            if self.port.value:
                if self.endpoint:
                    coremidi.MIDIPortDisconnectSource(self.port, self.endpoint)
                coremidi.MIDIPortDispose(self.port)
                self.port = _MIDIObjectRef()
            self.endpoint = 0

    class CoreMidiOutput:
        def __init__(self, device_id):
            self.device_id = int(device_id)
            self.port = _MIDIObjectRef()
            self.endpoint = 0

            match = next((item for item in self.endpoints() if item[0] == self.device_id), None)
            if match is None:
                raise RuntimeError(f"MIDI destination {self.device_id} is not connected")
            self.endpoint = match[1]

            client = _shared_client()
            name = _cfstring("Native USB Drum Pad Output")
            try:
                rc = coremidi.MIDIOutputPortCreate(client, name, ctypes.byref(self.port))
            finally:
                corefoundation.CFRelease(name)
            if rc:
                self.port = _MIDIObjectRef()
                raise RuntimeError(f"MIDIOutputPortCreate rc={rc}")

        @staticmethod
        def endpoints():
            return _endpoints(coremidi.MIDIGetNumberOfDestinations, coremidi.MIDIGetDestination)

        @staticmethod
        def devices():
            return [(index, name) for index, _endpoint, name in CoreMidiOutput.endpoints()]

        def send(self, status):
            if not self.port.value:
                return
            payload = bytes((int(status) & 0xFF,))
            # MIDIPacketList: numPackets, then one packet of timeStamp/length/data.
            packet_list = ctypes.create_string_buffer(
                struct.pack("<IQH", 1, 0, len(payload)) + payload
            )
            rc = coremidi.MIDISend(self.port, self.endpoint, packet_list)
            if rc:
                raise RuntimeError(f"MIDISend rc={rc}")

        def close(self):
            if self.port.value:
                coremidi.MIDIPortDispose(self.port)
                self.port = _MIDIObjectRef()
            self.endpoint = 0

    MidiInput = CoreMidiInput
    MidiOutput = CoreMidiOutput

    _SCHED_FIFO = 4
    _SCHED_PARAM_SIZE = 4

    class _sched_param(ctypes.Structure):
        _fields_ = [
            ("sched_priority", ctypes.c_int),
            ("opaque", ctypes.c_char * _SCHED_PARAM_SIZE),
        ]

    libc.pthread_self.restype = ctypes.c_void_p
    libc.pthread_setschedparam.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.POINTER(_sched_param)]
    libc.pthread_setschedparam.restype = ctypes.c_int
    libc.pthread_getschedparam.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(_sched_param)]
    libc.pthread_getschedparam.restype = ctypes.c_int
    libc.sched_get_priority_max.argtypes = [ctypes.c_int]
    libc.sched_get_priority_max.restype = ctypes.c_int

    def acquire_single_instance():
        import fcntl

        path = os.path.join(tempfile.gettempdir(), SINGLE_INSTANCE_LOCK)
        handle = open(path, "w")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return None
        handle.write(f"{os.getpid()}\n")
        handle.flush()
        return handle

    def release_single_instance(token):
        if token:
            try:
                token.close()
            except OSError:
                pass

    def enable_process_priority():
        try:
            os.setpriority(os.PRIO_PROCESS, 0, -5)
        except (OSError, PermissionError):
            # Unprivileged processes cannot raise their own priority; not fatal.
            pass

    def enable_audio_thread_priority():
        """Promote the trigger thread to SCHED_FIFO so drum hits are not delayed."""
        thread = ctypes.c_void_p(libc.pthread_self())
        previous_policy = ctypes.c_int()
        previous_param = _sched_param()
        if libc.pthread_getschedparam(thread, ctypes.byref(previous_policy), ctypes.byref(previous_param)):
            return None

        param = _sched_param()
        param.sched_priority = max(1, libc.sched_get_priority_max(_SCHED_FIFO) - 2)
        if libc.pthread_setschedparam(thread, _SCHED_FIFO, ctypes.byref(param)):
            return None
        return (thread, previous_policy.value, previous_param)

    def release_audio_thread_priority(token):
        if not token:
            return
        thread, policy, param = token
        libc.pthread_setschedparam(thread, policy, ctypes.byref(param))


else:

    MidiInput = _NullMidiInput
    MidiOutput = _NullMidiOutput

    def acquire_single_instance():
        return object()

    def release_single_instance(token):
        pass

    def enable_process_priority():
        pass

    def enable_audio_thread_priority():
        return None

    def release_audio_thread_priority(token):
        pass
