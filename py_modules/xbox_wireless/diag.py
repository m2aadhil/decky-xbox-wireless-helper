"""
Diagnostics for "No adapter detected":

- Xbox controllers seen as input devices, and whether they're connected through
  the adapter, over Bluetooth, or by cable. Steam Input's virtual pads are
  skipped - they live under /sys/devices/virtual and would look like real ones.
- Other Microsoft USB devices (vendor 045e), which helps spot an adapter
  revision whose product ID isn't in our table yet.
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass
from typing import Iterable

from .usb import MICROSOFT_VID, SYSFS_USB_DEVICES, _read

SYSFS_INPUT = "/sys/class/input"

BUS_USB = "0003"
BUS_BLUETOOTH = "0005"

CONTROLLER_HINTS = ("xbox", "x-box", "microsoft controller")


@dataclass
class Controller:
    name: str
    via: str  # "adapter" | "bluetooth" | "usb cable" | "other"

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class UsbDevice:
    sysfs: str
    id: str
    product: str

    def to_dict(self) -> dict:
        return asdict(self)


def find_controllers(
    adapter_paths: Iterable[str] = (),
    input_root: str = SYSFS_INPUT,
) -> list[Controller]:
    adapters = [os.path.realpath(p) + os.sep for p in adapter_paths]
    seen: set[tuple[str, str]] = set()
    out: list[Controller] = []
    try:
        entries = sorted(os.listdir(input_root))
    except OSError:
        return out
    for entry in entries:
        if not entry.startswith("input"):
            continue
        base = os.path.join(input_root, entry)
        real = os.path.realpath(base)
        if f"{os.sep}virtual{os.sep}" in real:
            continue  # Steam Input / uinput virtual pads
        name = _read(os.path.join(base, "name"))
        if not any(h in name.lower() for h in CONTROLLER_HINTS):
            continue
        bus = _read(os.path.join(base, "id", "bustype")).lower().zfill(4)
        if any(real.startswith(a) for a in adapters):
            via = "adapter"
        elif bus == BUS_BLUETOOTH:
            via = "bluetooth"
        elif bus == BUS_USB:
            via = "usb cable"
        else:
            via = "other"
        key = (name, via)
        if key in seen:
            continue  # one controller often exposes several input nodes
        seen.add(key)
        out.append(Controller(name, via))
    return out


def microsoft_usb_devices(root: str = SYSFS_USB_DEVICES) -> list[UsbDevice]:
    out: list[UsbDevice] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return out
    for name in entries:
        if ":" in name or name.startswith("usb"):
            continue
        path = os.path.join(root, name)
        if _read(os.path.join(path, "idVendor")) != MICROSOFT_VID:
            continue
        pid = _read(os.path.join(path, "idProduct"))
        out.append(UsbDevice(name, f"{MICROSOFT_VID}:{pid}", _read(os.path.join(path, "product")) or "Unknown"))
    return out
