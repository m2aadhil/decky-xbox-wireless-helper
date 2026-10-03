import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

import xbox_wireless.wake as wake  # noqa: E402
from xbox_wireless import (  # noqa: E402
    WakeLink,
    acpi_entries_for,
    apply_acpi_wake,
    read_mem_sleep,
    restore_acpi_wake,
    set_mem_sleep,
)

WAKEUP = """Device\tS-state\t  Status   Sysfs node
GPP0\t  S4\t*disabled  pci:0000:00:01.1
GP17\t  S4\t*disabled  pci:0000:00:08.1
XHC0\t  S4\t*disabled  pci:0000:0c:00.3
XHC1\t  S4\t*enabled   pci:0000:0c:00.4
PWRB\t  S5\t*enabled   platform:PNP0C0C:00
"""


class FakeProcWakeup:
    """/proc/acpi/wakeup where writing a name toggles that entry, like the kernel."""

    def __init__(self, testcase, path, content=WAKEUP):
        self.path = path
        with open(path, "w") as f:
            f.write(content)
        self.toggles = []
        orig = wake._acpi_toggle

        def toggle(name, p=path):
            self.toggles.append(name)
            with open(p) as f:
                lines = f.read().splitlines()
            out = [lines[0]]
            for line in lines[1:]:
                if line.split()[0] == name:
                    line = line.replace("*disabled", "*ENABLED").replace("*enabled", "*disabled").replace("*ENABLED", "*enabled")
                out.append(line)
            with open(p, "w") as f:
                f.write("\n".join(out) + "\n")

        wake._acpi_toggle = toggle
        testcase.addCleanup(setattr, wake, "_acpi_toggle", orig)

    def state(self):
        return {e.name: e.enabled for e in wake.read_acpi_wakeup(self.path)}


def chain():
    ctrl = "/sys/devices/pci0000:00/0000:00:08.1/0000:0c:00.3"
    return [
        WakeLink(ctrl + "/usb1/1-4", "adapter", "Adapter (1-4)", "enabled"),
        WakeLink(ctrl + "/usb1", "root hub", "Root hub (usb1)", "enabled"),
        WakeLink(ctrl, "usb controller", "USB controller (0000:0c:00.3)", "enabled"),
    ]


class AcpiWakeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.fake = FakeProcWakeup(self, os.path.join(self.tmp.name, "wakeup"))

    def test_finds_controller_and_its_bridge_only(self):
        names = [e.name for e in acpi_entries_for(chain(), self.fake.path)]
        self.assertEqual(names, ["XHC0", "GP17"])  # nearest first; not GPP0 / XHC1

    def test_apply_enables_and_records(self):
        restore = {}
        entries, errors = apply_acpi_wake(chain(), restore, self.fake.path)
        self.assertEqual(errors, [])
        self.assertTrue(all(e.enabled for e in entries))
        self.assertEqual(restore, {"acpi:XHC0": "disabled", "acpi:GP17": "disabled"})
        st = self.fake.state()
        self.assertFalse(st["GPP0"])  # untouched
        self.assertTrue(st["XHC1"])

    def test_apply_is_idempotent_and_never_toggles_enabled_entries(self):
        restore = {}
        apply_acpi_wake(chain(), restore, self.fake.path)
        apply_acpi_wake(chain(), restore, self.fake.path)
        self.assertEqual(sorted(self.fake.toggles), ["GP17", "XHC0"])

    def test_restore_puts_it_back(self):
        restore = {"/sys/devices/x": "disabled"}
        apply_acpi_wake(chain(), restore, self.fake.path)
        errors = restore_acpi_wake(restore, self.fake.path)
        self.assertEqual(errors, [])
        st = self.fake.state()
        self.assertFalse(st["XHC0"])
        self.assertFalse(st["GP17"])
        self.assertEqual(restore, {"/sys/devices/x": "disabled"})  # non-ACPI keys left alone

    def test_restore_skips_already_correct_and_missing(self):
        restore = {"acpi:XHC0": "disabled", "acpi:GONE": "disabled"}
        restore_acpi_wake(restore, self.fake.path)
        self.assertEqual(self.fake.toggles, [])
        self.assertEqual(restore, {})

    def test_no_controller_in_chain(self):
        self.assertEqual(acpi_entries_for(chain()[:2], self.fake.path), [])

    def test_missing_proc_file(self):
        self.assertEqual(wake.read_acpi_wakeup(os.path.join(self.tmp.name, "nope")), [])


class MemSleepTest(unittest.TestCase):
    def test_parse_and_set(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mem_sleep")
            with open(p, "w") as f:
                f.write("s2idle [deep]\n")
            self.assertEqual(read_mem_sleep(p), ("deep", ["s2idle", "deep"]))
            set_mem_sleep("s2idle", p)
            with open(p) as f:
                self.assertEqual(f.read(), "s2idle")

    def test_s2idle_only_systems(self):
        with tempfile.TemporaryDirectory() as d:
            p = os.path.join(d, "mem_sleep")
            with open(p, "w") as f:
                f.write("[s2idle]\n")
            self.assertEqual(read_mem_sleep(p), ("s2idle", ["s2idle"]))

    def test_missing(self):
        self.assertEqual(read_mem_sleep("/nonexistent/mem_sleep"), ("", []))


if __name__ == "__main__":
    unittest.main()
