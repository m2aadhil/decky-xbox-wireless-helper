import asyncio
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

from xbox_wireless import SuspendDetector, find_dongles, set_pairing, soft_replug  # noqa: E402
from xbox_wireless.usb import normalize_pids  # noqa: E402


def make_device(root, name, vid, pid, product=None, pairing=None):
    path = os.path.join(root, name)
    os.makedirs(path)
    for attr, value in (("idVendor", vid), ("idProduct", pid), ("authorized", "1")):
        with open(os.path.join(path, attr), "w") as f:
            f.write(value + "\n")
    if product:
        with open(os.path.join(path, "product"), "w") as f:
            f.write(product + "\n")
    if pairing is not None:
        iface = os.path.join(root, f"{name}:1.0")
        os.makedirs(iface)
        with open(os.path.join(iface, "pairing"), "w") as f:
            f.write(pairing + "\n")


def read(root, *parts):
    with open(os.path.join(root, *parts)) as f:
        return f.read().strip()


class FindDonglesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_finds_known_adapter_and_ignores_other_devices(self):
        make_device(self.root, "1-2", "045e", "02fe", "Xbox Wireless Adapter for Windows", pairing="0")
        make_device(self.root, "3-1", "046d", "c52b", "Logitech receiver")
        make_device(self.root, "3-2", "045e", "0b12", "Xbox controller (wired)")

        dongles = find_dongles(root=self.root)

        self.assertEqual([d.sysfs for d in dongles], ["1-2"])
        d = dongles[0]
        self.assertEqual(d.pid, "02fe")
        self.assertTrue(d.authorized)
        self.assertTrue(d.pairing_supported)
        self.assertFalse(d.pairing)

    def test_extra_pids_are_normalized(self):
        make_device(self.root, "1-4", "045e", "02ea")
        self.assertEqual(find_dongles(root=self.root), [])
        self.assertEqual(len(find_dongles(["0x02EA"], root=self.root)), 1)
        self.assertEqual(normalize_pids(["0x2EA", " 02fe ", ""]), {"02ea", "02fe"})

    def test_pairing_unsupported_without_xone(self):
        make_device(self.root, "1-2", "045e", "02e6")
        d = find_dongles(root=self.root)[0]
        self.assertFalse(d.pairing_supported)
        with self.assertRaises(RuntimeError):
            asyncio.run(set_pairing("1-2", True, root=self.root))

    def test_missing_sysfs_returns_empty(self):
        self.assertEqual(find_dongles(root=os.path.join(self.root, "nope")), [])


class ActionsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        make_device(self.root, "1-2", "045e", "02fe", pairing="0")

    def tearDown(self):
        self.tmp.cleanup()

    def test_soft_replug_toggles_authorized_and_leaves_it_on(self):
        writes = []
        import xbox_wireless.usb as usb

        original = usb._write

        def spy(path, value):
            writes.append((os.path.basename(path), value))
            original(path, value)

        usb._write = spy
        try:
            asyncio.run(soft_replug("1-2", root=self.root, hold=0))
        finally:
            usb._write = original

        self.assertEqual(writes, [("authorized", "0"), ("authorized", "1")])
        self.assertEqual(read(self.root, "1-2", "authorized"), "1")

    def test_set_pairing_on_and_off(self):
        asyncio.run(set_pairing("1-2", True, root=self.root))
        self.assertEqual(read(self.root, "1-2:1.0", "pairing"), "1")
        self.assertTrue(find_dongles(root=self.root)[0].pairing)
        asyncio.run(set_pairing("1-2", False, root=self.root))
        self.assertEqual(read(self.root, "1-2:1.0", "pairing"), "0")


class SuspendDetectorTest(unittest.TestCase):
    def test_detects_gap_above_threshold(self):
        t = [100.0]
        det = SuspendDetector(threshold=1.5, clock=lambda: t[0])
        t[0] += 0.2
        self.assertEqual(det.poll(), 0.0)
        t[0] += 60
        self.assertAlmostEqual(det.poll(), 60)
        self.assertEqual(det.poll(), 0.0)

    def test_rebase_ignores_elapsed(self):
        t = [0.0]
        det = SuspendDetector(clock=lambda: t[0])
        t[0] += 10
        det.rebase()
        self.assertEqual(det.poll(), 0.0)


if __name__ == "__main__":
    unittest.main()
