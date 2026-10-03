"""
Xbox Wireless Helper - Decky Loader backend.

- Soft-replugs the Xbox Wireless Adapter after resume from suspend, fixing the
  Bazzite / SteamOS issue where controllers stop responding until the adapter is
  physically unplugged.
- Toggles the adapter's pairing mode (xone driver).
- Optionally lets the controller wake the PC, by enabling USB wakeup along the
  adapter's path (adapter -> hubs -> USB controller) and re-applying it every
  time the adapter is re-enumerated.

Runs as root (plugin.json flag "root") because it writes to sysfs.
"""

import asyncio
import json
import os
import time

# The decky module is provided by Decky Loader at runtime (see decky.pyi for typings)
import decky

from xbox_wireless import (
    ResetError,
    SuspendDetector,
    apply_acpi_wake,
    apply_wake,
    driver_name,
    read_mem_sleep,
    restore_acpi_wake,
    set_mem_sleep,
    describe_oserror,
    disable_wake,
    find_controllers,
    find_dongles,
    microsoft_usb_devices,
    reset_device,
    set_pairing,
    wait_for_stable,
    wake_chain,
)

DEFAULT_SETTINGS = {
    "auto_reset": True,   # reset automatically after resume
    "delay_seconds": 3,   # wait after resume before resetting
    "extra_pids": [],     # user-added product IDs (hex, e.g. "02ea")
    "wake_enabled": False,  # let the controller wake the PC via the adapter
    "wake_restore": {},     # original power/wakeup of hubs/controller we changed
    "keep_usb_powered": False,  # switch suspend to s2idle so USB stays powered
    "mem_sleep_original": "",   # what /sys/power/mem_sleep was before we changed it
}

POLL_INTERVAL = 2.0

NOT_ROOT_HINT = (
    "Permission denied - the plugin backend isn't running as root. "
    "Reinstall the plugin and restart Decky."
)


RESUME_RETRY_DELAY = 5  # seconds before a second full attempt after resume

# if the adapter was seen this recently, a missing adapter is "reconnecting"
RECONNECT_GRACE = 20.0


def _describe(e: Exception) -> str:
    if isinstance(e, PermissionError):
        return NOT_ROOT_HINT
    if isinstance(e, ResetError):
        if any("EACCES" in a or "EPERM" in a for a in e.attempts):
            return NOT_ROOT_HINT
        # the last strategy's error is usually the most telling
        return "Reset failed - " + (e.attempts[-1] if e.attempts else "unknown error")
    return describe_oserror(e)


class Plugin:
    settings: dict = {}
    last_reset: dict = {}
    wake_status: dict = {"chain": [], "acpi": [], "errors": []}
    activity = None        # "settling" | "resetting" | None
    last_seen = 0.0        # time.monotonic() when an adapter was last present
    watcher_task = None

    # ------------------------------------------------------------------ settings

    def _settings_path(self) -> str:
        return os.path.join(decky.DECKY_PLUGIN_SETTINGS_DIR, "settings.json")

    def _load_settings(self) -> None:
        self.settings = dict(DEFAULT_SETTINGS)
        try:
            with open(self._settings_path(), "r") as f:
                self.settings.update(json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            decky.logger.warning(f"Could not read settings, using defaults: {e}")

    def _save_settings(self) -> None:
        os.makedirs(decky.DECKY_PLUGIN_SETTINGS_DIR, exist_ok=True)
        with open(self._settings_path(), "w") as f:
            json.dump(self.settings, f, indent=2)

    def _dongles(self):
        dongles = find_dongles(self.settings.get("extra_pids", []))
        if dongles:
            self.last_seen = time.monotonic()
        return dongles

    def _adapter_status(self, dongles) -> str:
        if dongles:
            return "ok"
        if self.activity:
            return "reconnecting"
        if self.last_seen and time.monotonic() - self.last_seen < RECONNECT_GRACE:
            return "reconnecting"
        return "missing"

    # ------------------------------------------------------------------ wake

    def _ensure_wake(self, dongles=None, quiet: bool = False) -> None:
        """
        Re-apply wakeup along every adapter's chain. Cheap when nothing needs
        changing, so it's safe to call often: the adapter comes back with
        wakeup disabled after every (re-)enumeration.
        """
        if not self.settings.get("wake_enabled"):
            self.wake_status = {"chain": [], "acpi": [], "errors": []}
            return
        if dongles is None:
            dongles = self._dongles()
        restore = self.settings.setdefault("wake_restore", {})
        before = dict(restore)
        chain, acpi, errors = [], [], []
        for d in dongles:
            pre = {l.path: l.state for l in wake_chain(d.sysfs)}
            links, errs = apply_wake(d.sysfs, restore)
            changed = [l.label for l in links if pre.get(l.path) != l.state]
            # firmware-level wake list (/proc/acpi/wakeup) for controller + bridges
            pre_acpi = {k for k in restore if k.startswith("acpi:")}
            entries, acpi_errs = apply_acpi_wake(links, restore)
            changed += [f"ACPI {k[5:]}" for k in restore if k.startswith("acpi:") and k not in pre_acpi]
            errs += acpi_errs
            if changed and not quiet:
                decky.logger.info(f"Wake enabled on: {', '.join(changed)}")
            for e in errs:
                if e not in self.wake_status.get("errors", []):
                    decky.logger.error(f"Could not enable wake: {e}")
            chain = chain or [l.to_dict() for l in links]
            acpi = acpi or [e.to_dict() for e in entries]
            errors += errs
        self.wake_status = {"chain": chain, "acpi": acpi, "errors": errors}
        if restore != before:
            self._save_settings()

    # ------------------------------------------------------------------ sleep mode

    def _apply_mem_sleep(self) -> str:
        """Switch suspend to s2idle so the board keeps USB powered. Returns an error or ""."""
        current, options = read_mem_sleep()
        if "s2idle" not in options:
            return "This system doesn't support s2idle"
        if current == "s2idle":
            return ""
        try:
            set_mem_sleep("s2idle")
        except OSError as e:
            return f"Could not change sleep mode: {e.strerror or e}"
        if not self.settings.get("mem_sleep_original"):
            self.settings["mem_sleep_original"] = current
        decky.logger.info(f"Sleep mode {current} -> s2idle (keeps USB powered)")
        return ""

    def _restore_mem_sleep(self) -> str:
        original = self.settings.get("mem_sleep_original")
        if not original:
            return ""
        current, options = read_mem_sleep()
        if original in options and current != original:
            try:
                set_mem_sleep(original)
            except OSError as e:
                return f"Could not restore sleep mode: {e.strerror or e}"
            decky.logger.info(f"Sleep mode restored to {original}")
        self.settings["mem_sleep_original"] = ""
        return ""

    # ------------------------------------------------------------------ actions

    async def _reset_all(self, reason: str, dongles=None) -> dict:
        prev, self.activity = self.activity, "resetting"
        try:
            return await self._do_reset_all(reason, dongles)
        finally:
            self.activity = prev
            self._dongles()  # refresh last_seen

    async def _do_reset_all(self, reason: str, dongles=None) -> dict:
        if dongles is None:
            dongles = self._dongles()
        errors = []
        used = []
        for d in dongles:
            def log(msg: str, sysfs: str = d.sysfs) -> None:
                decky.logger.warning(f"[{reason}] {sysfs}: {msg}")

            try:
                decky.logger.info(f"[{reason}] resetting {d.sysfs} (045e:{d.pid})")
                strategy = await reset_device(
                    d.sysfs,
                    pid=d.pid,
                    extra_pids=self.settings.get("extra_pids", []),
                    log=log,
                )
                used.append(strategy)
                decky.logger.info(f"[{reason}] {d.sysfs} reset OK via {strategy}")
            except Exception as e:
                decky.logger.error(f"[{reason}] reset failed on {d.sysfs}: {e}")
                errors.append(_describe(e))

        if not dongles:
            message = "No Xbox Wireless Adapter found"
        elif errors:
            message = "; ".join(dict.fromkeys(errors))
        elif used and any(s != "reauthorize" for s in used):
            message = f"Adapter reset ({', '.join(dict.fromkeys(used))})"
        else:
            message = "Adapter reset"

        # a reset re-creates the adapter, which loses its wakeup flag
        self._ensure_wake()

        self.last_reset = {
            "ok": bool(dongles) and not errors,
            "count": len(dongles),
            "reason": reason,
            "message": message,
            "time": time.time(),
        }
        return self.last_reset

    async def _watch_suspend(self) -> None:
        detector = SuspendDetector()
        decky.logger.info("Suspend watcher started")
        while True:
            try:
                await asyncio.sleep(POLL_INTERVAL)
                slept = detector.poll()
                if not slept:
                    # covers hot-plug and re-enumeration we didn't cause
                    self._ensure_wake()
                    continue
                decky.logger.info(f"Resume detected (slept ~{slept:.0f}s)")
                if not self.settings.get("auto_reset", True):
                    continue
                await asyncio.sleep(max(0, int(self.settings.get("delay_seconds", 3))))
                # the kernel may still be dropping / re-enumerating the adapter
                self.activity = "settling"
                try:
                    dongles = await wait_for_stable(self.settings.get("extra_pids", []))
                finally:
                    self.activity = None
                decky.logger.info(f"USB settled: {[d.sysfs for d in dongles] or 'no adapter'}")
                result = await self._reset_all("resume", dongles)
                if result["count"] > 0 and not result["ok"]:
                    # the adapter is sometimes still waking up; give it one more go
                    decky.logger.info(f"Retrying resume reset in {RESUME_RETRY_DELAY}s")
                    await asyncio.sleep(RESUME_RETRY_DELAY)
                    result = await self._reset_all("resume")
                if result["count"] > 0:
                    await decky.emit("dongle_reset", result)
                detector.rebase()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                decky.logger.error(f"Watcher error: {e}")

    # ------------------------------------------------------------------ callables (frontend)

    async def get_state(self) -> dict:
        from xbox_wireless.usb import SYSFS_USB_DEVICES

        dongles = self._dongles()
        status = self._adapter_status(dongles)
        controllers = find_controllers([os.path.join(SYSFS_USB_DEVICES, d.sysfs) for d in dongles])
        return {
            "settings": self.settings,
            "dongles": [d.to_dict() for d in dongles],
            "adapter_status": status,
            "activity": self.activity,
            "controllers": [c.to_dict() for c in controllers],
            # only needed to explain a missing adapter
            "microsoft_usb": [u.to_dict() for u in microsoft_usb_devices()] if status == "missing" else [],
            "last_reset": self.last_reset,
            "wake": {
                "enabled": bool(self.settings.get("wake_enabled")),
                **self.wake_status,
                **self._sleep_info(dongles),
            },
        }

    def _sleep_info(self, dongles) -> dict:
        mode, options = read_mem_sleep()
        return {
            "mem_sleep": mode,
            "mem_sleep_options": options,
            "keep_usb_powered": bool(self.settings.get("keep_usb_powered")),
            "driver": driver_name(dongles[0].sysfs) if dongles else "",
        }

    async def set_auto_reset(self, enabled: bool) -> dict:
        self.settings["auto_reset"] = bool(enabled)
        self._save_settings()
        return self.settings

    async def set_delay(self, seconds: int) -> dict:
        self.settings["delay_seconds"] = max(0, min(30, int(seconds)))
        self._save_settings()
        return self.settings

    async def set_wake(self, enabled: bool) -> dict:
        self.settings["wake_enabled"] = bool(enabled)
        if enabled:
            self._ensure_wake()
            errors = self.wake_status["errors"]
            if not self._dongles():
                message = "Enabled - will apply when the adapter is connected"
            elif errors:
                message = "; ".join(errors)
            else:
                message = "Controller can now wake the PC"
        else:
            restore = self.settings.setdefault("wake_restore", {})
            errors = []
            for d in self._dongles():
                errors += disable_wake(d.sysfs, restore)
            errors += disable_wake(None, restore)
            errors += restore_acpi_wake(restore)
            self.wake_status = {"chain": [], "acpi": [], "errors": errors}
            message = "; ".join(errors) if errors else "Wake from controller turned off"
            decky.logger.info(f"Wake disabled, restored original settings (errors: {errors})")
        self._save_settings()
        return {"ok": not errors, "message": message, **(await self.get_state())}

    async def set_keep_usb_powered(self, enabled: bool) -> dict:
        self.settings["keep_usb_powered"] = bool(enabled)
        err = self._apply_mem_sleep() if enabled else self._restore_mem_sleep()
        if err and enabled:
            self.settings["keep_usb_powered"] = False
        self._save_settings()
        message = err or ("Sleep mode set to s2idle" if enabled else "Sleep mode restored")
        return {"ok": not err, "message": message, **(await self.get_state())}

    async def reset_now(self) -> dict:
        return await self._reset_all("manual")

    async def set_pairing(self, enabled: bool) -> dict:
        dongles = self._dongles()
        if not dongles:
            return {"ok": False, "message": "No Xbox Wireless Adapter found"}
        targets = [d for d in dongles if d.pairing_supported]
        if not targets:
            return {
                "ok": False,
                "message": "Pairing needs the xone driver - use the button on the adapter instead.",
            }
        errors = []
        for d in targets:
            try:
                await set_pairing(d.sysfs, bool(enabled))
                decky.logger.info(f"Pairing {'on' if enabled else 'off'} for {d.sysfs}")
            except Exception as e:
                decky.logger.error(f"Pairing toggle failed on {d.sysfs}: {e}")
                errors.append(_describe(e))
        if errors:
            return {"ok": False, "message": "; ".join(dict.fromkeys(errors))}
        return {
            "ok": True,
            "message": "Pairing mode on - hold the pair button on your controller"
            if enabled
            else "Pairing mode off",
        }

    # ------------------------------------------------------------------ lifecycle

    async def _main(self):
        self._load_settings()
        if os.geteuid() != 0:
            decky.logger.error(NOT_ROOT_HINT)
        decky.logger.info(
            f"Xbox Wireless Helper loaded: settings={self.settings}, "
            f"dongles={[d.to_dict() for d in self._dongles()]}"
        )
        if self.settings.get("keep_usb_powered"):
            # /sys/power/mem_sleep resets at boot, so re-apply
            err = self._apply_mem_sleep()
            if err:
                decky.logger.warning(err)
            else:
                self._save_settings()
        self._ensure_wake()
        self.watcher_task = asyncio.get_event_loop().create_task(self._watch_suspend())

    async def _unload(self):
        if self.watcher_task:
            self.watcher_task.cancel()
            self.watcher_task = None
        decky.logger.info("Xbox Wireless Helper unloaded")

    async def _uninstall(self):
        # put hubs / USB controller back the way we found them
        restore = self.settings.get("wake_restore", {})
        for d in self._dongles():
            disable_wake(d.sysfs, restore)
        disable_wake(None, restore)
        restore_acpi_wake(restore)
        self._restore_mem_sleep()
        decky.logger.info("Uninstalled, original wake/sleep settings restored")

    async def _migration(self):
        pass
