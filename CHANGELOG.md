# Changelog

## 1.2.1

- "Keep USB powered in sleep" now warns that it may keep the fans running
  (s2idle keeps desktop boards in their "on" power state) and is presented
  as a last resort
- BIOS hint corrected: set "ErP Ready" to "Enabled (S4+S5)" (this is what made
  wake from deep sleep work on the author's board) instead of "disable ErP"
- "No adapter detected" hint: replug the adapter once after first install
- README: new step-by-step setup guide, including why the Xbox adapter needs
  stricter BIOS power settings than a USB keyboard to wake the PC

## 1.2.0

- Wake from controller now also enables the firmware (ACPI) wake entries in
  `/proc/acpi/wakeup` for the adapter's USB controller and the PCIe bridge(s)
  above it (e.g. `XHC0`, `GP17`); needed on many AMD boards. Restored when
  turned off / uninstalled
- New **Keep USB powered in sleep** toggle: switches suspend from deep sleep
  (S3) to s2idle, for boards that cut USB power in S3 (adapter goes dark and
  can't hear the Xbox button). Re-applied on every boot, original mode restored
  when turned off / uninstalled
- Panel shows the current sleep mode, firmware wake entries, a warning if the
  adapter isn't on the xone driver (needed for wake-on-wireless), and the BIOS
  settings to check

## 1.1.1

- Fix "No adapter detected" showing while the adapter is connected:
  - the panel now refreshes live (every 2.5 s) instead of keeping the
    snapshot taken when it was opened
  - while the adapter is briefly gone (re-enumeration after wake, or our own
    port power-cycle) it shows "Adapter reconnecting…" instead
- Detect adapter revisions with unknown product IDs via the xone driver binding
- Show connected controllers and how they're connected (adapter / Bluetooth /
  USB cable); if no adapter is found, explain why (e.g. controller is on
  Bluetooth) and list unrecognised Microsoft USB IDs to report

## 1.1.0

- New **Wake PC with controller** toggle: enables USB wakeup along the
  adapter's own path (adapter -> hubs -> root hub -> USB controller), so the
  Xbox button can wake the PC from sleep
- Re-applied automatically after every reset, re-enumeration and hot-plug
  (the adapter comes back with wakeup disabled each time)
- Only the adapter's path is touched, never whole buses, so other USB devices
  don't start waking the PC
- Original hub/controller settings are restored when the toggle is turned off
  or the plugin is uninstalled

## 1.0.2

- Fix `[Errno 71] Protocol error` (EPROTO) when resetting right after resume
- Fix `[Errno 2] No such file or directory: .../authorized` after resume: the
  kernel sometimes drops and re-enumerates the adapter on wake, so the plugin
  now waits for USB to settle and re-locates the adapter before every step
- Reset now escalates: re-authorize -> hub port power-cycle -> USB reset ioctl,
  retrying transient errors (EPROTO/EIO/ETIMEDOUT/...) with backoff at each step
- A reset only counts as successful once the driver has re-bound to the adapter
- On resume, a failed reset is retried once more after 5 s
- The adapter is never left de-authorised if a reset fails

## 1.0.1

- Fix: backend now actually runs as root (`plugin.json` flag was `_root`, which Decky ignores), so Reset and Pairing no longer fail with "Permission denied"
- Clearer error message and log entry when the backend isn't running as root
- Test that guards the root flag

## 1.0.0

- Automatic soft-replug of the Xbox Wireless Adapter after resume from sleep
- Manual "Reset adapter now" button
- Pairing mode toggle (xone driver)
- Configurable post-wake delay
