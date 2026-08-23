"""Find out whether the pad controller's lights can be driven over MIDI.

The Starrypad reports itself as a Jieli Technology chip behind Apple's generic
USB-MIDI driver, so there is no vendor driver and no published LED protocol.
This walks the possibilities from safest to least safe and prints what it sends,
so an answer comes from the hardware rather than from guessing.

    python tools/led_probe.py identify     # ask the device who it is, then listen
    python tools/led_probe.py listen       # dump everything the device sends
    python tools/led_probe.py lights       # visual phases; watch the pads

`identify` and `listen` only read. `lights` sends note and channel messages,
which a pad controller may also treat as sound triggers.
"""

import ctypes
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import platform_backend as backend  # noqa: E402

coremidi = backend.coremidi
corefoundation = backend.corefoundation

# Universal, non-realtime: "identity request" to every device on the port.
DEVICE_INQUIRY = bytes((0xF0, 0x7E, 0x7F, 0x06, 0x01, 0xF7))

# The notes the Starrypad's own pads send, so a light probe targets real pads.
PAD_NOTES = (36, 38, 42, 46, 45, 47, 48, 43, 39, 37, 56, 49, 51, 54, 70, 75)


def send(port, endpoint, payload):
    """Push one raw MIDI message out of `port`."""
    import struct

    blob = struct.pack("<IQH", 1, 0, len(payload)) + bytes(payload)
    buffer = ctypes.create_string_buffer(blob, len(blob))
    status = coremidi.MIDISend(port, endpoint, buffer)
    if status:
        raise RuntimeError(f"MIDISend rc={status}")


def open_output():
    client = backend._shared_client()
    name = backend._cfstring("LED probe out")
    port = ctypes.c_uint32()
    try:
        status = coremidi.MIDIOutputPortCreate(client, name, ctypes.byref(port))
    finally:
        corefoundation.CFRelease(name)
    if status:
        raise RuntimeError(f"MIDIOutputPortCreate rc={status}")
    return port


def open_raw_input(collected):
    """An input port that records raw bytes, SysEx included.

    The app's own parser drops SysEx, and SysEx is exactly where a vendor would
    hide anything interesting.
    """
    def read(packet_list, _ref, _src):
        if not packet_list:
            return
        try:
            count = int.from_bytes(ctypes.string_at(packet_list, 4), sys.byteorder)
            offset = 4
            for _ in range(count):
                header = ctypes.string_at(packet_list + offset, 10)
                length = int.from_bytes(header[8:10], sys.byteorder)
                if length:
                    collected.append(bytes(ctypes.string_at(packet_list + offset + 10, length)))
                end = packet_list + offset + 10 + length
                if backend._PACKET_ALIGNMENT:
                    end = (end + 3) & ~3
                offset = end - packet_list
        except Exception:
            pass

    proc = backend._MIDIReadProc(read)
    client = backend._shared_client()
    name = backend._cfstring("LED probe in")
    port = ctypes.c_uint32()
    try:
        status = coremidi.MIDIInputPortCreate(client, name, proc, None, ctypes.byref(port))
    finally:
        corefoundation.CFRelease(name)
    if status:
        raise RuntimeError(f"MIDIInputPortCreate rc={status}")
    for _index, endpoint, _name in backend.CoreMidiInput.endpoints():
        coremidi.MIDIPortConnectSource(port, endpoint, None)
    return port, proc


def describe(payload):
    return " ".join(f"{byte:02X}" for byte in payload)


def decode_identity(payload):
    """Read a Universal Identity Reply: F0 7E <ch> 06 02 <maker...> ... F7."""
    if len(payload) < 7 or payload[0] != 0xF0 or payload[1] != 0x7E or payload[3:5] != b"\x06\x02":
        return None
    body = payload[5:-1]
    if body[:1] == b"\x00":            # three byte manufacturer id
        maker, rest = body[:3], body[3:]
    else:
        maker, rest = body[:1], body[1:]
    return {
        "manufacturer": describe(maker),
        "family": describe(rest[:2]),
        "member": describe(rest[2:4]),
        "version": describe(rest[4:8]),
    }


def identify():
    collected = []
    port_in, _proc = open_raw_input(collected)
    port_out = open_output()
    destinations = backend.CoreMidiOutput.endpoints()

    for index, endpoint, name in destinations:
        print(f"-> {name}: {describe(DEVICE_INQUIRY)}")
        try:
            send(port_out, endpoint, DEVICE_INQUIRY)
        except RuntimeError as error:
            print(f"   send failed: {error}")
        time.sleep(0.6)

    time.sleep(0.6)
    if not collected:
        print("\nNo reply. The device does not answer a standard identity request,")
        print("which is normal for a class compliant controller with no editor.")
        return
    print()
    for payload in collected:
        print(f"<- {describe(payload)}")
        identity = decode_identity(payload)
        if identity:
            print(f"   identity: {identity}")


def listen(seconds=20.0):
    collected = []
    # Hold both: a collected read proc would be called into freed memory.
    _port, _proc = open_raw_input(collected)
    print(f"Listening for {seconds:.0f}s on all input ports.")
    print("Hit pads, hold them, and try any buttons or knobs on the device.")
    seen = 0
    deadline = time.time() + seconds
    while time.time() < deadline:
        while seen < len(collected):
            print(f"<- {describe(collected[seen])}")
            seen += 1
        time.sleep(0.05)
    print(f"\n{len(collected)} messages. Anything starting F0 is SysEx and worth keeping.")


def lights():
    port_out = open_output()
    destinations = backend.CoreMidiOutput.endpoints()
    print("Watch the pads. Each phase is announced before it is sent.\n")

    for index, endpoint, name in destinations:
        print(f"=== port {index}: {name}")

        print("  phase 1  note on, channel 1, velocity 127")
        for note in PAD_NOTES:
            send(port_out, endpoint, (0x90, note, 127))
        time.sleep(1.5)
        for note in PAD_NOTES:
            send(port_out, endpoint, (0x80, note, 0))
        time.sleep(0.6)

        print("  phase 2  note on, channel 10, velocity 127")
        for note in PAD_NOTES:
            send(port_out, endpoint, (0x99, note, 127))
        time.sleep(1.5)
        for note in PAD_NOTES:
            send(port_out, endpoint, (0x89, note, 0))
        time.sleep(0.6)

        print("  phase 3  velocity as a colour index, 1 to 127 on the first pad")
        for velocity in range(1, 128, 6):
            send(port_out, endpoint, (0x90, PAD_NOTES[0], velocity))
            time.sleep(0.12)
        send(port_out, endpoint, (0x80, PAD_NOTES[0], 0))
        time.sleep(0.6)

        print("  phase 4  the same note on each of the sixteen channels")
        for channel in range(16):
            send(port_out, endpoint, (0x90 | channel, PAD_NOTES[0], 127))
            time.sleep(0.25)
            send(port_out, endpoint, (0x80 | channel, PAD_NOTES[0], 0))
        time.sleep(0.6)
        print()

    print("Which phase and which port lit anything up?")


def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "identify"
    if mode == "identify":
        identify()
    elif mode == "listen":
        listen(float(sys.argv[2]) if len(sys.argv) > 2 else 20.0)
    elif mode == "lights":
        lights()
    else:
        print(__doc__)


if __name__ == "__main__":
    main()
