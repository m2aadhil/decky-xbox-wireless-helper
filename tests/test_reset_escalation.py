import asyncio
import errno
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "py_modules"))

import xbox_wireless.usb as usb  # noqa: E402
from xbox_wireless import ResetError, reset_device  # noqa: E402

FAST = dict(hold=0, backoff=0, settle_timeout=0.3, appear_timeout=0.3)


def eproto(path):
    return OSError(errno.EPROTO, "Protocol error", path)


class FakeAdapter:
    """
    Fake sysfs for one adapter at 1-2 behind root-hub port usb1-port2, with the
    xone driver bound to interface 1-2:1.0. Writes go through `on_write`, which
    can raise or mutate state to simulate the kernel.
    """

    def __init__(self, root, driver=True, name="1-2"):
        self.root = root
        self.name = name
        self.dev = os.path.join(root, name)
        self.iface = os.path.join(root, f"{name}:1.0")
        self.port = os.path.join(root, "1-0:1.0", "usb1-port2")
        os.makedirs(self.dev)
        os.makedirs(self.iface)
        os.makedirs(self.port)
        self._put(self.dev, "idVendor", "045e")
        self._put(self.dev, "idProduct", "02fe")
        self._put(self.dev, "authorized", "1")
        self._put(self.dev, "busnum", "1")
        self._put(self.dev, "devnum", "5")
        self._put(self.port, "disable", "0")
        os.symlink(self.port, os.path.join(self.dev, "port"))
        if driver:
            self.bind()
        self.writes = []
        self.ioctls = []

    def _put(self, d, name, value):
        with open(os.path.join(d, name), "w") as f:
            f.write(value + "\n")

    def bind(self):
        drv = os.path.join(self.root, "_drivers", "xone-dongle")
        os.makedirs(drv, exist_ok=True)
        link = os.path.join(self.iface, "driver")
        if not os.path.lexists(link):
            os.symlink(drv, link)

    def unbind(self):
        link = os.path.join(self.iface, "driver")
        if os.path.lexists(link):
            os.remove(link)

    def move_to(self, new):
        """Kernel dropped the device and re-enumerated it under a new name."""
        new_dev, new_iface = os.path.join(self.root, new), os.path.join(self.root, f"{new}:1.0")
        os.rename(self.dev, new_dev)
        os.rename(self.iface, new_iface)
        self.name, self.dev, self.iface = new, new_dev, new_iface
        self._put(self.dev, "authorized", "1")
        self.bind()

    def vanish(self):
        import shutil
        shutil.rmtree(self.dev)
        shutil.rmtree(self.iface)

    def authorized(self):
        with open(os.path.join(self.dev, "authorized")) as f:
            return f.read().strip()

    # default kernel behaviour; tests override pieces of it
    def on_write(self, path, value):
        name = os.path.basename(path)
        if name == "authorized":
            if value == "0":
                self.unbind()
            else:
                self.bind()
        elif name == "disable" and value == "0":
            # port re-enabled -> device re-enumerated, fresh & authorised
            self._put(self.dev, "authorized", "1")
            self.bind()

    def install(self, testcase):
        orig_write, orig_ioctl = usb._write, usb._ioctl_reset

        def write(path, value):
            self.writes.append((os.path.basename(path), value))
            self.on_write(path, value)  # may raise
            orig_write(path, value)

        def ioctl(node):
            self.ioctls.append(node)
            self.on_ioctl(node)

        usb._write, usb._ioctl_reset = write, ioctl
        testcase.addCleanup(setattr, usb, "_write", orig_write)
        testcase.addCleanup(setattr, usb, "_ioctl_reset", orig_ioctl)
        return self

    def on_ioctl(self, node):
        self.bind()


def run(coro):
    return asyncio.run(coro)


class ResetEscalationTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def reset(self, device="1-2", **kw):
        return run(reset_device(device, pid="02fe", root=self.root, dev_root="/dev/bus/usb", **{**FAST, **kw}))

    def test_happy_path_reauthorize(self):
        a = FakeAdapter(self.root).install(self)
        self.assertEqual(self.reset(), "reauthorize")
        self.assertEqual(a.writes, [("authorized", "0"), ("authorized", "1")])
        self.assertEqual(a.authorized(), "1")

    def test_transient_eproto_on_reauthorize_is_retried(self):
        a = FakeAdapter(self.root)
        fails = {"n": 2}
        base = a.on_write

        def flaky(path, value):
            if os.path.basename(path) == "authorized" and value == "1" and fails["n"]:
                fails["n"] -= 1
                raise eproto(path)
            base(path, value)

        a.on_write = flaky
        a.install(self)
        self.assertEqual(self.reset(), "reauthorize")
        self.assertEqual(a.writes.count(("authorized", "1")), 3)
        self.assertEqual(a.authorized(), "1")

    def test_eproto_on_deauthorize_still_reauthorizes(self):
        a = FakeAdapter(self.root)
        base = a.on_write

        def wedged(path, value):
            if os.path.basename(path) == "authorized" and value == "0":
                raise eproto(path)
            base(path, value)

        a.on_write = wedged
        a.install(self)
        self.assertEqual(self.reset(), "reauthorize")
        self.assertEqual(a.authorized(), "1")

    def test_persistent_eproto_escalates_to_port_cycle(self):
        a = FakeAdapter(self.root)
        base = a.on_write

        def dead(path, value):
            if os.path.basename(path) == "authorized" and value == "1":
                raise eproto(path)
            base(path, value)

        a.on_write = dead
        a.install(self)
        self.assertEqual(self.reset(), "port-cycle")
        self.assertIn(("disable", "1"), a.writes)
        self.assertEqual(a.writes[-1], ("disable", "0"))
        self.assertEqual(a.authorized(), "1")

    def test_driver_probe_failure_counts_as_not_ready(self):
        # authorize "succeeds" but xone's re-probe fails, so no driver gets bound
        a = FakeAdapter(self.root)
        base = a.on_write

        def probe_fails(path, value):
            if os.path.basename(path) == "authorized" and value == "1":
                return  # authorised, but driver stays unbound
            base(path, value)

        a.on_write = probe_fails
        a.install(self)
        self.assertEqual(self.reset(), "port-cycle")

    def test_falls_back_to_usb_reset_without_port_control(self):
        a = FakeAdapter(self.root)
        os.remove(os.path.join(a.port, "disable"))
        base = a.on_write

        def probe_fails(path, value):
            if os.path.basename(path) == "authorized" and value == "1":
                return
            base(path, value)

        a.on_write = probe_fails
        a.install(self)
        self.assertEqual(self.reset(), "usb-reset")
        self.assertEqual(a.ioctls, ["/dev/bus/usb/001/005"])

    def test_everything_fails_raises_and_logs(self):
        a = FakeAdapter(self.root)

        def always(path, value):
            raise eproto(path)

        def ioctl_fails(node):
            raise eproto(node)

        a.on_write = always
        a.on_ioctl = ioctl_fails
        a.install(self)
        logs = []
        with self.assertRaises(ResetError) as ctx:
            self.reset(log=logs.append)
        self.assertEqual([m.split(":")[0] for m in ctx.exception.attempts],
                         ["reauthorize", "port-cycle", "usb-reset"])
        self.assertTrue(all("EPROTO (71)" in m for m in ctx.exception.attempts))
        self.assertEqual(len(logs), 3)

    def test_permission_error_is_not_retried(self):
        a = FakeAdapter(self.root)

        def denied(path, value):
            raise PermissionError(errno.EACCES, "Permission denied", path)

        a.on_write = denied
        a.on_ioctl = lambda node: (_ for _ in ()).throw(PermissionError(errno.EACCES, "Permission denied"))
        a.install(self)
        with self.assertRaises(ResetError) as ctx:
            self.reset()
        self.assertTrue(any("EACCES" in m for m in ctx.exception.attempts))
        # EACCES is not transient, so each write is tried exactly once:
        # deauth + reauth + port disable + port re-enable
        self.assertEqual(len(a.writes), 4)
        self.assertEqual(a.authorized(), "1")



class ReEnumerationTest(unittest.TestCase):
    """After resume the kernel may drop the adapter and bring it back elsewhere."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def reset(self, device, **kw):
        return run(reset_device(device, pid="02fe", root=self.root, dev_root="/dev/bus/usb", **{**FAST, **kw}))

    def test_follows_adapter_to_new_name(self):
        a = FakeAdapter(self.root, name="1-4").install(self)
        a.move_to("1-5")  # stale name from before resume
        logs = []
        self.assertEqual(self.reset("1-4", log=logs.append), "reauthorize")
        self.assertTrue(any("now at 1-5" in m for m in logs))
        self.assertEqual(a.authorized(), "1")

    def test_adapter_vanishes_mid_reset(self):
        # de-authorise works, then the kernel drops the device and re-enumerates
        # it as 1-5, so re-authorising the old path fails with ENOENT
        a = FakeAdapter(self.root, name="1-4")
        base = a.on_write

        def drop_after_deauth(path, value):
            base(path, value)
            if os.path.basename(path) == "authorized" and value == "0" and a.name == "1-4":
                a.move_to("1-5")
                # make the write itself land on the old (now missing) path
                raise FileNotFoundError(2, "No such file or directory", path)

        a.on_write = drop_after_deauth
        a.install(self)
        self.assertIn(self.reset("1-4"), ("reauthorize", "port-cycle"))
        self.assertEqual(a.name, "1-5")
        self.assertEqual(a.authorized(), "1")

    def test_adapter_reappears_late(self):
        a = FakeAdapter(self.root, name="1-4")
        moved = os.path.join(self.root, "_hidden")
        os.makedirs(moved)
        os.rename(a.dev, os.path.join(moved, "dev"))
        os.rename(a.iface, os.path.join(moved, "iface"))
        a.install(self)

        async def scenario():
            async def comeback():
                await asyncio.sleep(0.2)
                os.rename(os.path.join(moved, "dev"), a.dev)
                os.rename(os.path.join(moved, "iface"), a.iface)

            task = asyncio.create_task(comeback())
            result = await reset_device("1-4", pid="02fe", root=self.root, **{**FAST, "appear_timeout": 2})
            await task
            return result

        self.assertEqual(run(scenario()), "reauthorize")

    def test_gone_for_good_raises_quickly(self):
        a = FakeAdapter(self.root, name="1-4").install(self)
        a.vanish()
        with self.assertRaises(ResetError) as ctx:
            self.reset("1-4")
        self.assertTrue(all("not present" in m for m in ctx.exception.attempts))
        self.assertEqual(a.writes, [])


class WaitForStableTest(unittest.TestCase):
    def test_waits_out_re_enumeration(self):
        from xbox_wireless import wait_for_stable

        with tempfile.TemporaryDirectory() as root:
            a = FakeAdapter(root, name="1-4")

            async def scenario():
                async def churn():
                    await asyncio.sleep(0.1)
                    a.move_to("1-5")

                task = asyncio.create_task(churn())
                ds = await wait_for_stable(root=root, stable_for=0.3, timeout=3, poll=0.05)
                await task
                return ds

            ds = run(scenario())
            self.assertEqual([d.sysfs for d in ds], ["1-5"])


if __name__ == "__main__":
    unittest.main()
