import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

from xbox_wireless import apply_wake, disable_wake, wake_chain  # noqa: E402


def put(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value + "\n")


def get(path):
    with open(path) as f:
        return f.read().strip()


class FakeTree:
    """
    /devices/pci0000:00/0000:00:08.1/0000:0c:00.3   xHCI controller (pci)
        usb1                                          root hub
          1-2                                         external hub (class 09)
            1-2.4                                     Xbox adapter
          1-3                                         a mouse (should never be touched)
    /bus/usb/devices/<name> -> symlinks into the tree, like real sysfs
    """

    def __init__(self, base, controller="disabled", root_hub="enabled", hub="disabled", adapter="disabled"):
        self.devices = os.path.join(base, "devices")
        self.bus = os.path.join(base, "bus", "usb", "devices")
        os.makedirs(self.bus)
        pci_bus = os.path.join(base, "bus", "pci")
        os.makedirs(pci_bus)

        self.controller = os.path.join(self.devices, "pci0000:00", "0000:00:08.1", "0000:0c:00.3")
        self.root_hub = os.path.join(self.controller, "usb1")
        self.hub = os.path.join(self.root_hub, "1-2")
        self.adapter = os.path.join(self.hub, "1-2.4")
        self.mouse = os.path.join(self.root_hub, "1-3")

        # PCI bridge above the controller also has power/wakeup - must not be touched
        put(os.path.join(self.devices, "pci0000:00", "0000:00:08.1", "power", "wakeup"), "disabled")
        put(os.path.join(self.controller, "power", "wakeup"), controller)
        os.symlink(pci_bus, os.path.join(self.controller, "subsystem"))

        for path, state, vid, cls in (
            (self.root_hub, root_hub, "1d6b", "09"),
            (self.hub, hub, "05e3", "09"),
            (self.adapter, adapter, "045e", "00"),
            (self.mouse, "disabled", "046d", "00"),
        ):
            put(os.path.join(path, "power", "wakeup"), state)
            put(os.path.join(path, "idVendor"), vid)
            put(os.path.join(path, "bDeviceClass"), cls)
            os.symlink(path, os.path.join(self.bus, os.path.basename(path)))

    def state(self, path):
        return get(os.path.join(path, "power", "wakeup"))


class WakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = FakeTree(self.tmp.name)

    def chain(self):
        return wake_chain("1-2.4", root=self.t.bus, stop=self.t.devices)

    def test_chain_is_adapter_to_controller(self):
        kinds = [(l.kind, os.path.basename(l.path)) for l in self.chain()]
        self.assertEqual(kinds, [
            ("adapter", "1-2.4"),
            ("hub", "1-2"),
            ("root hub", "usb1"),
            ("usb controller", "0000:0c:00.3"),
        ])

    def test_apply_enables_whole_chain_and_records_originals(self):
        restore = {}
        links, errors = apply_wake("1-2.4", restore, root=self.t.bus, stop=self.t.devices)
        self.assertEqual(errors, [])
        self.assertTrue(all(l.state == "enabled" for l in links))
        # only upstream nodes we actually changed are recorded; the adapter is not
        self.assertEqual(restore, {
            os.path.realpath(self.t.hub): "disabled",
            os.path.realpath(self.t.controller): "disabled",
        })
        # neighbours and nodes above the controller are untouched
        self.assertEqual(self.t.state(self.t.mouse), "disabled")
        self.assertEqual(get(os.path.join(os.path.dirname(self.t.controller), "power", "wakeup")), "disabled")

    def test_apply_is_idempotent(self):
        restore = {}
        apply_wake("1-2.4", restore, root=self.t.bus, stop=self.t.devices)
        snapshot = dict(restore)
        # adapter re-enumerated -> its flag is reset, upstream stays enabled
        put(os.path.join(self.t.adapter, "power", "wakeup"), "disabled")
        links, _ = apply_wake("1-2.4", restore, root=self.t.bus, stop=self.t.devices)
        self.assertEqual(self.t.state(self.t.adapter), "enabled")
        # originals are not overwritten with "enabled"
        self.assertEqual(restore, snapshot)

    def test_disable_restores_originals(self):
        restore = {}
        apply_wake("1-2.4", restore, root=self.t.bus, stop=self.t.devices)
        errors = disable_wake("1-2.4", restore, root=self.t.bus)
        self.assertEqual(errors, [])
        self.assertEqual(restore, {})
        self.assertEqual(self.t.state(self.t.adapter), "disabled")
        self.assertEqual(self.t.state(self.t.hub), "disabled")
        self.assertEqual(self.t.state(self.t.root_hub), "enabled")  # was already enabled
        self.assertEqual(self.t.state(self.t.controller), "disabled")

    def test_disable_without_adapter_still_restores(self):
        restore = {}
        apply_wake("1-2.4", restore, root=self.t.bus, stop=self.t.devices)
        disable_wake(None, restore, root=self.t.bus)
        self.assertEqual(self.t.state(self.t.controller), "disabled")
        self.assertEqual(restore, {})

    def test_stale_restore_entries_are_dropped(self):
        restore = {"/nonexistent/0000:99:00.0": "disabled"}
        self.assertEqual(disable_wake(None, restore, root=self.t.bus), [])
        self.assertEqual(restore, {})

    def test_missing_adapter_gives_empty_chain(self):
        self.assertEqual(wake_chain("9-9", root=self.t.bus, stop=self.t.devices), [])


if __name__ == "__main__":
    unittest.main()
