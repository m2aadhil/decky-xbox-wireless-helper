import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

from xbox_wireless import find_controllers, find_dongles, microsoft_usb_devices  # noqa: E402


def put(path, value):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(value + "\n")


class Tree:
    """Minimal /sys/devices + /sys/bus/usb/devices + /sys/class/input."""

    def __init__(self, base):
        self.base = base
        self.devices = os.path.join(base, "devices")
        self.usb = os.path.join(base, "bus", "usb", "devices")
        self.input = os.path.join(base, "class", "input")
        self.drivers = os.path.join(base, "bus", "usb", "drivers")
        for d in (self.usb, self.input, self.drivers):
            os.makedirs(d)
        self.n = 0

    def usb_device(self, name, vid, pid, product="", driver=None):
        real = os.path.join(self.devices, "pci0000:00", "usb1", name)
        put(os.path.join(real, "idVendor"), vid)
        put(os.path.join(real, "idProduct"), pid)
        put(os.path.join(real, "authorized"), "1")
        if product:
            put(os.path.join(real, "product"), product)
        os.symlink(real, os.path.join(self.usb, name))
        iface = os.path.join(real, f"{name}:1.0")
        os.makedirs(iface)
        os.symlink(iface, os.path.join(self.usb, f"{name}:1.0"))
        if driver:
            drv = os.path.join(self.drivers, driver)
            os.makedirs(drv, exist_ok=True)
            os.symlink(drv, os.path.join(iface, "driver"))
        return real

    def input_device(self, parent, name, bustype):
        self.n += 1
        real = os.path.join(parent, "input", f"input{self.n}")
        put(os.path.join(real, "name"), name)
        put(os.path.join(real, "id", "bustype"), bustype)
        os.symlink(real, os.path.join(self.input, f"input{self.n}"))
        return real


class DetectionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Tree(self.tmp.name)

    def test_unknown_pid_found_via_xone_driver(self):
        self.t.usb_device("1-4", "045e", "0b99", "Xbox Wireless Adapter (new rev)", driver="xone-dongle")
        self.assertEqual([d.sysfs for d in find_dongles(root=self.t.usb)], ["1-4"])

    def test_unknown_pid_without_xone_is_not_an_adapter(self):
        self.t.usb_device("1-4", "045e", "0b12", "Xbox Wireless Controller", driver="xpad")
        self.assertEqual(find_dongles(root=self.t.usb), [])
        self.assertEqual([u.id for u in microsoft_usb_devices(self.t.usb)], ["045e:0b12"])


class ControllerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.t = Tree(self.tmp.name)

    def test_classifies_connection_types_and_skips_virtual(self):
        adapter = self.t.usb_device("1-4", "045e", "02fe", driver="xone-dongle")
        # xone puts controllers below the adapter's interface (gip bus)
        gip = os.path.join(adapter, "1-4:1.0", "gip0", "gip0.1")
        self.t.input_device(gip, "Microsoft Xbox Controller", "0006")
        self.t.input_device(gip, "Microsoft Xbox Controller", "0006")  # 2nd node, same pad
        bt = os.path.join(self.t.devices, "pci0000:00", "bluetooth", "hci0", "hci0:256")
        self.t.input_device(bt, "Xbox Wireless Controller", "0005")
        cable = self.t.usb_device("1-6", "045e", "0b12", driver="xpad")
        self.t.input_device(os.path.join(cable, "1-6:1.0"), "Microsoft X-Box One pad", "0003")
        # Steam Input's virtual pad must be ignored
        self.t.input_device(os.path.join(self.t.devices, "virtual"), "Microsoft X-Box 360 pad 0", "0003")
        self.t.input_device(os.path.join(self.t.devices, "platform", "i8042"), "AT keyboard", "0011")

        got = [(c.name, c.via) for c in find_controllers([os.path.join(self.t.usb, "1-4")], self.t.input)]
        self.assertEqual(sorted(got), sorted([
            ("Microsoft Xbox Controller", "adapter"),
            ("Xbox Wireless Controller", "bluetooth"),
            ("Microsoft X-Box One pad", "usb cable"),
        ]))

    def test_missing_input_root(self):
        self.assertEqual(find_controllers([], os.path.join(self.tmp.name, "nope")), [])


if __name__ == "__main__":
    unittest.main()
