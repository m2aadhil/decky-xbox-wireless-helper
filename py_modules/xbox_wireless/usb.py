"""
Finding, resetting and pairing Xbox Wireless Adapters via sysfs.
"""

from __future__ import annotations

import asyncio
import errno
import os
import time
from dataclasses import asdict, dataclass
from typing import Callable, Iterable, Optional

SYSFS_USB_DEVICES = "/sys/bus/usb/devices"
MICROSOFT_VID = "045e"

# Xbox Wireless Adapter product IDs (same table the xone driver matches on)
DEFAULT_PIDS = frozenset({"02e6", "02f9", "02fe", "091e"})


@dataclass
class Dongle:
    sysfs: str
    pid: str
    product: str
    authorized: bool
    pairing_supported: bool
    pairing: bool

    def to_dict(self) -> dict:
        return asdict(self)


def _read(path: str) -> str:
    try:
        with open(path, "r") as f:
            return f.read().strip()
    except OSError:
        return ""


def _write(path: str, value: str) -> None:
    with open(path, "w") as f:
        f.write(value)


def normalize_pids(pids: Iterable[str]) -> set[str]:
    return {p.strip().lower().removeprefix("0x").zfill(4) for p in pids if p and p.strip()}


def pairing_path(device: str, root: str = SYSFS_USB_DEVICES) -> Optional[str]:
    """
    The xone driver exposes a writable `pairing` attribute on the dongle's USB
    interface, e.g. /sys/bus/usb/devices/1-2:1.0/pairing.
    Returns None when the dongle isn't bound to xone.
    """
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return None
    for name in entries:
        if name.startswith(device + ":"):
            p = os.path.join(root, name, "pairing")
            if os.path.exists(p):
                return p
    return None


XONE_DONGLE_DRIVERS = ("xone-dongle", "xone_dongle")


def _bound_to_xone_dongle(device: str, root: str) -> bool:
    """Catches adapter revisions whose product ID isn't in DEFAULT_PIDS yet."""
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    for name in entries:
        if name.startswith(device + ":"):
            drv = os.path.join(root, name, "driver")
            if os.path.lexists(drv) and os.path.basename(os.path.realpath(drv)) in XONE_DONGLE_DRIVERS:
                return True
    return False


def driver_name(device: str, root: str = SYSFS_USB_DEVICES) -> str:
    """Name of the driver bound to the device's first interface ("" if none)."""
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return ""
    for name in entries:
        if name.startswith(device + ":"):
            drv = os.path.join(root, name, "driver")
            if os.path.lexists(drv):
                return os.path.basename(os.path.realpath(drv))
    return ""


def find_dongles(extra_pids: Iterable[str] = (), root: str = SYSFS_USB_DEVICES) -> list[Dongle]:
    pids = set(DEFAULT_PIDS) | normalize_pids(extra_pids)
    found: list[Dongle] = []
    try:
        entries = sorted(os.listdir(root))
    except OSError:
        return found

    for name in entries:
        # "1-2" is a device, "1-2:1.0" is one of its interfaces
        if ":" in name:
            continue
        path = os.path.join(root, name)
        if _read(os.path.join(path, "idVendor")) != MICROSOFT_VID:
            continue
        pid = _read(os.path.join(path, "idProduct"))
        if pid not in pids and not _bound_to_xone_dongle(name, root):
            continue
        pp = pairing_path(name, root)
        found.append(
            Dongle(
                sysfs=name,
                pid=pid,
                product=_read(os.path.join(path, "product")) or "Xbox Wireless Adapter",
                authorized=_read(os.path.join(path, "authorized")) == "1",
                pairing_supported=pp is not None,
                pairing=pp is not None and _read(pp) == "1",
            )
        )
    return found


# ---------------------------------------------------------------- resetting
#
# Right after resume the adapter firmware can be wedged badly enough that the
# kernel gets EPROTO (errno 71) / EIO / ETIMEDOUT talking to it. So a reset is an
# escalation chain, each step closer to a physical replug:
#
#   1. reauthorize  - authorized 0 -> 1: driver unbinds, device re-probed
#   2. port-cycle   - hub port disable 1 -> 0: device is disconnected and
#                     re-enumerated from scratch (the software equivalent of
#                     pulling it out)
#   3. usb-reset    - USBDEVFS_RESET ioctl on /dev/bus/usb/BBB/DDD (what the
#                     `usbreset` tool does)
#
# Transient errors are retried with backoff before escalating, and we never
# leave the adapter de-authorised.

DEV_BUS_USB = "/dev/bus/usb"
USBDEVFS_RESET = 0x5514  # _IO('U', 20)

TRANSIENT_ERRNOS = frozenset({
    errno.EPROTO,     # 71: protocol error - firmware not answering yet
    errno.EIO,
    errno.ETIMEDOUT,
    errno.EPIPE,
    errno.EAGAIN,
    errno.EBUSY,
    errno.ENODEV,     # device vanished mid-reset (re-enumerating)
    errno.ENOENT,
})


class ResetError(Exception):
    def __init__(self, device: str, attempts: list[str]):
        self.device = device
        self.attempts = attempts
        super().__init__(f"all reset strategies failed for {device}: " + "; ".join(attempts))


def describe_oserror(e: BaseException) -> str:
    if isinstance(e, OSError) and e.errno is not None:
        name = errno.errorcode.get(e.errno, str(e.errno))
        return f"{name} ({e.errno}): {e.strerror or e}"
    return str(e)


def _is_transient(e: BaseException) -> bool:
    return isinstance(e, OSError) and e.errno in TRANSIENT_ERRNOS


def _ioctl_reset(dev_node: str) -> None:
    import fcntl

    fd = os.open(dev_node, os.O_WRONLY)
    try:
        fcntl.ioctl(fd, USBDEVFS_RESET, 0)
    finally:
        os.close(fd)


def _driver_bound(device: str, root: str) -> bool:
    """True if any interface of the device (e.g. 1-2:1.0) has a driver bound."""
    try:
        entries = os.listdir(root)
    except OSError:
        return False
    return any(
        name.startswith(device + ":") and os.path.exists(os.path.join(root, name, "driver"))
        for name in entries
    )


def _device_ready(device: str, root: str, need_driver: bool) -> bool:
    path = os.path.join(root, device)
    if _read(os.path.join(path, "idVendor")) != MICROSOFT_VID:
        return False
    if _read(os.path.join(path, "authorized")) != "1":
        return False
    # authorize can succeed while the driver's re-probe fails with EPROTO
    return _driver_bound(device, root) if need_driver else True


async def _retry(fn, attempts: int, backoff: float):
    """Run blocking fn() in a thread, retrying transient OSErrors."""
    for i in range(attempts):
        try:
            return await asyncio.to_thread(fn)
        except OSError as e:
            if not _is_transient(e) or i == attempts - 1:
                raise
            await asyncio.sleep(backoff * (i + 1))


async def _strategy_reauthorize(device: str, root: str, hold: float, backoff: float) -> None:
    auth = os.path.join(root, device, "authorized")
    if not os.path.exists(auth):
        raise OSError(errno.ENODEV, "device not present", auth)
    try:
        # de-authorising can itself hit EPROTO on a wedged device; that's fine,
        # we still want to try re-authorising
        await _retry(lambda: _write(auth, "0"), attempts=2, backoff=backoff)
    except OSError:
        pass
    await asyncio.sleep(hold)
    # never leave it de-authorised: retry harder here
    await _retry(lambda: _write(auth, "1"), attempts=5, backoff=backoff)


async def _strategy_port_cycle(device: str, root: str, hold: float, backoff: float) -> None:
    port_link = os.path.join(root, device, "port")
    port_dir = os.path.realpath(port_link)
    disable = os.path.join(port_dir, "disable")
    if not os.path.lexists(port_link) or not os.path.exists(disable):
        raise OSError(errno.ENOTSUP, "hub port control not available", disable)
    # resolve the path *before* disabling - the device's sysfs dir disappears
    try:
        await _retry(lambda: _write(disable, "1"), attempts=2, backoff=backoff)
        await asyncio.sleep(max(hold, 1.0))
    finally:
        await _retry(lambda: _write(disable, "0"), attempts=5, backoff=backoff)


async def _strategy_usb_reset(device: str, root: str, dev_root: str, backoff: float) -> None:
    base = os.path.join(root, device)
    busnum, devnum = _read(os.path.join(base, "busnum")), _read(os.path.join(base, "devnum"))
    if not busnum or not devnum:
        raise OSError(errno.ENODEV, "device not present", base)
    node = os.path.join(dev_root, f"{int(busnum):03d}", f"{int(devnum):03d}")
    await _retry(lambda: _ioctl_reset(node), attempts=3, backoff=backoff)


def resolve_device(
    device: Optional[str], pid: str, root: str = SYSFS_USB_DEVICES, extra_pids: Iterable[str] = ()
) -> Optional[str]:
    """
    Current sysfs name of the adapter. After resume the kernel may drop and
    re-enumerate it, so `1-4` can vanish and come back as `1-4` again or under
    a different name. Prefer the known name, else find the same model.
    """
    if device:
        base = os.path.join(root, device)
        if _read(os.path.join(base, "idVendor")) == MICROSOFT_VID and _read(os.path.join(base, "idProduct")) == pid:
            return device
    for d in find_dongles(extra_pids, root):
        if d.pid == pid:
            return d.sysfs
    return None


async def await_device(
    device: Optional[str], pid: str, root: str, timeout: float, extra_pids: Iterable[str] = (), poll: float = 0.25
) -> Optional[str]:
    deadline = time.monotonic() + timeout
    while True:
        name = resolve_device(device, pid, root, extra_pids)
        if name or time.monotonic() >= deadline:
            return name
        await asyncio.sleep(poll)


async def wait_for_stable(
    extra_pids: Iterable[str] = (),
    root: str = SYSFS_USB_DEVICES,
    stable_for: float = 1.5,
    timeout: float = 10.0,
    poll: float = 0.25,
) -> list[Dongle]:
    """
    Wait until USB enumeration has settled after resume: the set of adapters
    (and their authorised / driver-bound state) unchanged for `stable_for`
    seconds. Returns whatever is present when it settles or times out.
    """
    extra = list(extra_pids)

    def snapshot():
        ds = find_dongles(extra, root)
        return ds, tuple((d.sysfs, d.pid, d.authorized, _driver_bound(d.sysfs, root)) for d in ds)

    deadline = time.monotonic() + timeout
    dongles, last = snapshot()
    since = time.monotonic()
    while time.monotonic() < deadline:
        await asyncio.sleep(poll)
        dongles, cur = snapshot()
        if cur != last:
            last, since = cur, time.monotonic()
        elif cur and time.monotonic() - since >= stable_for:
            break
    return dongles


async def reset_device(
    device: str,
    pid: Optional[str] = None,
    root: str = SYSFS_USB_DEVICES,
    dev_root: str = DEV_BUS_USB,
    hold: float = 1.0,
    backoff: float = 1.0,
    settle_timeout: float = 8.0,
    appear_timeout: float = 8.0,
    extra_pids: Iterable[str] = (),
    log: Optional[Callable[[str], None]] = None,
) -> str:
    """
    Reset the adapter, escalating through strategies until one works.
    The adapter is looked up again before every step, so it's fine if the
    kernel re-enumerates it under us. Returns the strategy that succeeded;
    raises ResetError otherwise.
    """
    log = log or (lambda _m: None)
    extra = list(extra_pids)
    if pid is None:
        pid = _read(os.path.join(root, device, "idProduct"))

    current: Optional[str] = device
    need_driver: Optional[bool] = None
    attempts: list[str] = []

    async def locate() -> str:
        nonlocal current, need_driver
        name = await await_device(current, pid, root, appear_timeout, extra)
        if name is None:
            raise OSError(errno.ENODEV, f"adapter 045e:{pid} not present (gone after resume?)")
        if name != current:
            log(f"adapter is now at {name} (was {current})")
            current = name
        if need_driver is None:
            need_driver = _driver_bound(name, root)
        return name

    strategies = [
        ("reauthorize", lambda d: _strategy_reauthorize(d, root, hold, backoff)),
        ("port-cycle", lambda d: _strategy_port_cycle(d, root, hold, backoff)),
        ("usb-reset", lambda d: _strategy_usb_reset(d, root, dev_root, backoff)),
    ]
    for name, run in strategies:
        try:
            await run(await locate())
        except Exception as e:
            msg = f"{name}: {describe_oserror(e)}"
            log(msg)
            attempts.append(msg)
            continue

        # the device may have been re-enumerated by the step itself
        deadline = time.monotonic() + settle_timeout
        while True:
            dev = resolve_device(current, pid, root, extra)
            if dev and _device_ready(dev, root, bool(need_driver)):
                return name
            if time.monotonic() >= deadline:
                break
            await asyncio.sleep(0.25)
        msg = f"{name}: adapter did not come back (driver not re-bound) within {settle_timeout:.0f}s"
        log(msg)
        attempts.append(msg)

    # last line of defence: don't leave it switched off
    dev = resolve_device(current, pid, root, extra)
    if dev:
        auth = os.path.join(root, dev, "authorized")
        if _read(auth) == "0":
            try:
                await asyncio.to_thread(_write, auth, "1")
            except OSError:
                pass
    raise ResetError(current or device, attempts)


async def soft_replug(device: str, root: str = SYSFS_USB_DEVICES, hold: float = 1.0) -> None:
    """Backwards-compatible alias: full escalating reset."""
    await reset_device(device, root=root, hold=hold)


async def set_pairing(device: str, enabled: bool, root: str = SYSFS_USB_DEVICES) -> None:
    pp = pairing_path(device, root)
    if pp is None:
        raise RuntimeError("pairing not supported (adapter is not using the xone driver)")
    await asyncio.to_thread(_write, pp, "1" if enabled else "0")
