"""
Let the controller wake the PC through the Xbox Wireless Adapter.

For a USB remote-wakeup to resume the system, wake has to be allowed at every
level between the device and the platform:

    adapter (1-4)  ->  hub(s) (1-0:1.0 ...)  ->  root hub (usb1)  ->  xHCI PCI controller

Each of these has a `power/wakeup` attribute in sysfs ("enabled"/"disabled").
The adapter is re-created on every (re-)enumeration - including after our
own post-resume reset - and comes back "disabled", so this must be re-applied.

"""

from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from typing import Optional

from .usb import SYSFS_USB_DEVICES, _read, _write

SYSFS_DEVICES = "/sys/devices"


@dataclass
class WakeLink:
    path: str      # real sysfs path of the node
    kind: str      # "adapter" | "hub" | "root hub" | "usb controller" | "other"
    label: str
    state: str     # "enabled" | "disabled" | "" (unreadable)

    def to_dict(self) -> dict:
        return asdict(self)


def _kind(path: str, adapter: str) -> tuple[str, str]:
    name = os.path.basename(path)
    if path == adapter:
        return "adapter", f"Adapter ({name})"
    if name.startswith("usb") and os.path.exists(os.path.join(path, "idVendor")):
        return "root hub", f"Root hub ({name})"
    if _read(os.path.join(path, "bDeviceClass")) == "09":
        return "hub", f"USB hub ({name})"
    subsystem = os.path.basename(os.path.realpath(os.path.join(path, "subsystem")))
    if subsystem == "pci":
        return "usb controller", f"USB controller ({name})"
    return "other", name


def wake_chain(device: str, root: str = SYSFS_USB_DEVICES, stop: str = SYSFS_DEVICES) -> list[WakeLink]:
    """
    Every node with a power/wakeup attribute from the adapter up to the top of
    the device tree, nearest first. Stops at the first PCI node (the USB
    controller) - nothing above it needs to change.
    """
    base = os.path.join(root, device)
    if not os.path.exists(base):
        return []
    adapter = os.path.realpath(base)
    stop = os.path.realpath(stop)
    links: list[WakeLink] = []
    p = adapter
    while p and p != stop and p != os.path.dirname(p):
        wakeup = os.path.join(p, "power", "wakeup")
        if os.path.exists(wakeup):
            kind, label = _kind(p, adapter)
            links.append(WakeLink(p, kind, label, _read(wakeup)))
            if kind == "usb controller":
                break
        p = os.path.dirname(p)
    return links


def apply_wake(
    device: str,
    restore: dict[str, str],
    root: str = SYSFS_USB_DEVICES,
    stop: str = SYSFS_DEVICES,
) -> tuple[list[WakeLink], list[str]]:
    """
    Enable wakeup along the adapter's chain. `restore` is updated in place with
    the original value of every *upstream* node we change, so it can be undone
    later (the adapter itself is recreated on re-enumeration, so it isn't
    recorded). Returns (chain after the change, errors).
    """
    errors: list[str] = []
    for link in wake_chain(device, root, stop):
        if link.state == "enabled":
            continue
        try:
            _write(os.path.join(link.path, "power", "wakeup"), "enabled")
            if link.kind != "adapter":
                restore.setdefault(link.path, link.state or "disabled")
        except OSError as e:
            errors.append(f"{link.label}: {e.strerror or e}")
    return wake_chain(device, root, stop), errors


def disable_wake(
    device: Optional[str],
    restore: dict[str, str],
    root: str = SYSFS_USB_DEVICES,
) -> list[str]:
    """
    Turn wake off on the adapter and put every upstream node we changed back
    to its original value. Clears `restore` for nodes that were restored.
    """
    errors: list[str] = []
    if device:
        wakeup = os.path.join(root, device, "power", "wakeup")
        if os.path.exists(wakeup):
            try:
                _write(wakeup, "disabled")
            except OSError as e:
                errors.append(f"Adapter ({device}): {e.strerror or e}")
    for path, original in list(restore.items()):
        if path.startswith("acpi:"):
            continue  # handled by restore_acpi_wake()
        wakeup = os.path.join(path, "power", "wakeup")
        if not os.path.exists(wakeup):
            # controller/hub no longer exists (e.g. different boot) - forget it
            restore.pop(path, None)
            continue
        try:
            _write(wakeup, original)
            restore.pop(path, None)
        except OSError as e:
            errors.append(f"{os.path.basename(path)}: {e.strerror or e}")
    return errors


# ---------------------------------------------------------------- ACPI wake
#
# Besides sysfs power/wakeup, the firmware keeps its own wake list in
# /proc/acpi/wakeup, e.g.
#
#   Device  S-state   Status   Sysfs node
#   GP17      S4    *disabled  pci:0000:00:08.1
#   XHC0      S4    *disabled  pci:0000:0c:00.3
#
# On many (especially AMD) boards the USB controller's entry - and sometimes
# the PCIe bridge above it, which owns the wake GPE - must be enabled here too.
# Writing a device name to the file *toggles* it, so we only write when it's
# disabled, and record it so it can be toggled back.

PROC_ACPI_WAKEUP = "/proc/acpi/wakeup"
PCI_ADDR = re.compile(r"^[0-9a-f]{4}:[0-9a-f]{2}:[0-9a-f]{2}\.[0-7]$")
ACPI_PREFIX = "acpi:"


@dataclass
class AcpiWake:
    name: str
    sstate: str
    enabled: bool
    pci: str

    def to_dict(self) -> dict:
        return asdict(self)


def read_acpi_wakeup(path: str = PROC_ACPI_WAKEUP) -> list[AcpiWake]:
    out: list[AcpiWake] = []
    try:
        with open(path) as f:
            lines = f.read().splitlines()
    except OSError:
        return out
    for line in lines[1:]:
        parts = line.split()
        if len(parts) < 3:
            continue
        name, sstate, status = parts[0], parts[1], parts[2]
        pci = ""
        for p in parts[3:]:
            if p.startswith("pci:"):
                pci = p[4:]
                break
        out.append(AcpiWake(name, sstate, status.lstrip("*") == "enabled", pci))
    return out


def pci_path(chain: list[WakeLink]) -> list[str]:
    """PCI addresses from the USB controller up to the root complex, nearest first."""
    controller = next((l for l in chain if l.kind == "usb controller"), None)
    if controller is None:
        return []
    out = []
    p = controller.path
    while p and p != os.path.dirname(p):
        name = os.path.basename(p)
        if PCI_ADDR.match(name):
            out.append(name)
        elif name.startswith("pci"):
            break
        p = os.path.dirname(p)
    return out


def _acpi_toggle(name: str, path: str = PROC_ACPI_WAKEUP) -> None:
    with open(path, "w") as f:
        f.write(name)


def acpi_entries_for(chain: list[WakeLink], path: str = PROC_ACPI_WAKEUP) -> list[AcpiWake]:
    addrs = pci_path(chain)
    entries = {e.pci: e for e in read_acpi_wakeup(path) if e.pci}
    return [entries[a] for a in addrs if a in entries]


def apply_acpi_wake(
    chain: list[WakeLink], restore: dict[str, str], path: str = PROC_ACPI_WAKEUP
) -> tuple[list[AcpiWake], list[str]]:
    errors: list[str] = []
    for e in acpi_entries_for(chain, path):
        if e.enabled:
            continue
        try:
            _acpi_toggle(e.name, path)
            restore.setdefault(ACPI_PREFIX + e.name, "disabled")
        except OSError as err:
            errors.append(f"ACPI {e.name}: {err.strerror or err}")
    return acpi_entries_for(chain, path), errors


def restore_acpi_wake(restore: dict[str, str], path: str = PROC_ACPI_WAKEUP) -> list[str]:
    errors: list[str] = []
    current = {e.name: e for e in read_acpi_wakeup(path)}
    for key in [k for k in restore if k.startswith(ACPI_PREFIX)]:
        name = key[len(ACPI_PREFIX):]
        e = current.get(name)
        if e is None:
            restore.pop(key, None)
            continue
        want_enabled = restore[key] == "enabled"
        if e.enabled != want_enabled:
            try:
                _acpi_toggle(name, path)
            except OSError as err:
                errors.append(f"ACPI {name}: {err.strerror or err}")
                continue
        restore.pop(key, None)
    return errors


# ---------------------------------------------------------------- sleep mode
#
# /sys/power/mem_sleep picks what "suspend" means: "deep" (S3) or "s2idle".
# In S3 many boards cut USB power, so the adapter can't hear the Xbox button.
# s2idle usually keeps ports powered. It's a runtime setting (reset at boot),
# so the plugin re-applies it on every start.

SYS_MEM_SLEEP = "/sys/power/mem_sleep"


def read_mem_sleep(path: str = SYS_MEM_SLEEP) -> tuple[str, list[str]]:
    """Returns (current, available), e.g. ("deep", ["s2idle", "deep"])."""
    raw = _read(path)
    if not raw:
        return "", []
    options = [o.strip("[]") for o in raw.split()]
    current = next((o.strip("[]") for o in raw.split() if o.startswith("[")), "")
    return current, options


def set_mem_sleep(mode: str, path: str = SYS_MEM_SLEEP) -> None:
    _write(path, mode)
