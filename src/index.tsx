import {
  ButtonItem,
  Field,
  PanelSection,
  PanelSectionRow,
  SliderField,
  ToggleField,
  staticClasses,
} from "@decky/ui";
import {
  addEventListener,
  callable,
  definePlugin,
  removeEventListener,
  toaster,
} from "@decky/api";
import { useEffect, useState } from "react";
import { FaXbox } from "react-icons/fa";

type Dongle = {
  sysfs: string;
  pid: string;
  product: string;
  authorized: boolean;
  pairing_supported: boolean;
  pairing: boolean;
};

type ResetResult = {
  ok: boolean;
  count: number;
  reason: "resume" | "manual";
  message: string;
  time: number;
};

type Settings = {
  auto_reset: boolean;
  delay_seconds: number;
  extra_pids: string[];
};

type WakeLink = {
  path: string;
  kind: "adapter" | "hub" | "root hub" | "usb controller" | "other";
  label: string;
  state: string;
};

type AcpiWake = { name: string; sstate: string; enabled: boolean; pci: string };

type WakeStatus = {
  enabled: boolean;
  chain: WakeLink[];
  acpi: AcpiWake[];
  errors: string[];
  mem_sleep: string;
  mem_sleep_options: string[];
  keep_usb_powered: boolean;
  driver: string;
};

type Controller = { name: string; via: "adapter" | "bluetooth" | "usb cable" | "other" };
type UsbDevice = { sysfs: string; id: string; product: string };

type State = {
  settings: Settings;
  dongles: Dongle[];
  last_reset: ResetResult | Record<string, never>;
  wake: WakeStatus;
  adapter_status: "ok" | "reconnecting" | "missing";
  activity: "settling" | "resetting" | null;
  controllers: Controller[];
  microsoft_usb: UsbDevice[];
};

const VIA_LABEL: Record<Controller["via"], string> = {
  adapter: "via adapter",
  bluetooth: "via Bluetooth",
  "usb cable": "via USB cable",
  other: "connected",
};

function missingExplanation(s: State): string {
  const bt = s.controllers.filter((c) => c.via === "bluetooth");
  const cable = s.controllers.filter((c) => c.via === "usb cable");
  if (bt.length)
    return `Your controller is connected over Bluetooth, not through the adapter. Re-pair it with the adapter to use it.`;
  if (cable.length) return "Your controller is connected by USB cable, and no adapter is plugged in.";
  if (s.microsoft_usb.length)
    return `Found Microsoft USB device(s) not recognised as an adapter: ${s.microsoft_usb
      .map((u) => `${u.id} ${u.product}`)
      .join(", ")}. Please report this ID on GitHub.`;
  return "Plug in the Xbox Wireless Adapter. Just installed the plugin? Unplug the adapter and plug it back in once.";
}

const getState = callable<[], State>("get_state");
const setAutoReset = callable<[enabled: boolean], Settings>("set_auto_reset");
const setDelay = callable<[seconds: number], Settings>("set_delay");
const resetNow = callable<[], ResetResult>("reset_now");
const setKeepUsbPowered = callable<
  [enabled: boolean],
  State & { ok: boolean; message: string }
>("set_keep_usb_powered");
const setWake = callable<[enabled: boolean], State & { ok: boolean; message: string }>(
  "set_wake",
);

function wakeSummary(w: WakeStatus, hasAdapter: boolean): string {
  if (!w.enabled) return "Press the Xbox button to wake the PC from sleep.";
  if (!hasAdapter) return "Will apply when the adapter is connected.";
  if (w.driver && !w.driver.startsWith("xone"))
    return `The adapter uses the "${w.driver}" driver, which can't wake the PC. Wake needs xone.`;
  if (w.errors.length) return w.errors.join("; ");
  const links = w.chain.length + w.acpi.length;
  const on =
    w.chain.filter((l) => l.state === "enabled").length + w.acpi.filter((a) => a.enabled).length;
  return `Wake enabled on ${on}/${links} links.`;
}

const SLEEP_LABEL: Record<string, string> = {
  deep: "Deep sleep (S3)",
  s2idle: "s2idle",
  shallow: "Standby (S1)",
};
const setPairing = callable<[enabled: boolean], { ok: boolean; message: string }>(
  "set_pairing",
);

function formatTime(ts?: number): string {
  if (!ts) return "Never";
  return new Date(ts * 1000).toLocaleTimeString();
}

function Content() {
  const [state, setState] = useState<State | null>(null);
  const [busy, setBusy] = useState(false);

  const refresh = async () => setState(await getState());

  useEffect(() => {
    refresh();
    // keep the panel live while it's open: the adapter can disappear and come
    // back during wake / resets, and a one-off snapshot would go stale
    const id = setInterval(refresh, 2500);
    const listener = addEventListener<[ResetResult]>("dongle_reset", () => refresh());
    return () => {
      clearInterval(id);
      removeEventListener("dongle_reset", listener);
    };
  }, []);

  const pairing = state?.dongles.some((d) => d.pairing) ?? false;
  const pairingSupported = state?.dongles.some((d) => d.pairing_supported) ?? false;

  const onPair = async () => {
    setBusy(true);
    try {
      const r = await setPairing(!pairing);
      toaster.toast({ title: "Xbox Wireless Helper", body: r.message });
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const onReset = async () => {
    setBusy(true);
    try {
      const r = await resetNow();
      toaster.toast({
        title: "Xbox Wireless Helper",
        body: r.ok ? `Reset ${r.count} adapter(s)` : r.message,
      });
    } finally {
      setBusy(false);
      refresh();
    }
  };

  if (!state) {
    return (
      <PanelSection>
        <PanelSectionRow>
          <Field label="Loading…" />
        </PanelSectionRow>
      </PanelSection>
    );
  }

  const last = state.last_reset as ResetResult;

  return (
    <>
      <PanelSection title="Adapter">
        {state.dongles.length === 0 ? (
          <PanelSectionRow>
            {state.adapter_status === "reconnecting" ? (
              <Field
                label="Adapter reconnecting…"
                description={
                  state.activity === "resetting"
                    ? "Resetting the adapter."
                    : "Waiting for the adapter to come back after wake."
                }
              />
            ) : (
              <Field label="No adapter detected" description={missingExplanation(state)} />
            )}
          </PanelSectionRow>
        ) : (
          state.dongles.map((d) => (
            <PanelSectionRow key={d.sysfs}>
              <Field
                label={d.product}
                description={`045e:${d.pid} · port ${d.sysfs}`}
              />
            </PanelSectionRow>
          ))
        )}
        {state.controllers.map((c) => (
          <PanelSectionRow key={`${c.name}-${c.via}`}>
            <Field label={c.name} description={VIA_LABEL[c.via]} />
          </PanelSectionRow>
        ))}
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || state.dongles.length === 0}
            onClick={onReset}
          >
            {busy ? "Resetting…" : "Reset adapter now"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem
            layout="below"
            disabled={busy || !pairingSupported}
            onClick={onPair}
            description={
              state.dongles.length > 0 && !pairingSupported
                ? "Needs the xone driver - use the button on the adapter instead."
                : pairing
                  ? "Hold the pair button on the controller until the Xbox logo flashes fast."
                  : undefined
            }
          >
            {pairing ? "Stop pairing" : "Start pairing"}
          </ButtonItem>
        </PanelSectionRow>
        <PanelSectionRow>
          <ButtonItem layout="below" onClick={refresh}>
            Refresh
          </ButtonItem>
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="After sleep">
        <PanelSectionRow>
          <ToggleField
            label="Auto-reset on resume"
            description="Soft-replug the adapter every time the system wakes."
            checked={state.settings.auto_reset}
            onChange={async (v) => {
              const settings = await setAutoReset(v);
              setState({ ...state, settings });
            }}
          />
        </PanelSectionRow>
        <PanelSectionRow>
          <SliderField
            label="Delay after wake"
            description="Seconds to wait before resetting. Increase if it doesn't stick."
            value={state.settings.delay_seconds}
            min={0}
            max={15}
            step={1}
            showValue
            valueSuffix="s"
            onChange={async (v) => {
              const settings = await setDelay(v);
              setState({ ...state, settings });
            }}
          />
        </PanelSectionRow>
      </PanelSection>

      <PanelSection title="Wake from sleep">
        <PanelSectionRow>
          <ToggleField
            label="Wake PC with controller"
            description={wakeSummary(state.wake, state.dongles.length > 0)}
            checked={state.wake.enabled}
            disabled={busy}
            onChange={async (v) => {
              setBusy(true);
              try {
                const r = await setWake(v);
                setState(r);
                if (!r.ok) {
                  toaster.toast({ title: "Xbox Wireless Helper", body: r.message });
                }
              } finally {
                setBusy(false);
              }
            }}
          />
        </PanelSectionRow>
        {state.wake.enabled &&
          state.wake.chain.map((l) => (
            <PanelSectionRow key={l.path}>
              <Field
                label={l.label}
                description={l.state === "enabled" ? "Wake allowed" : "Wake blocked"}
              />
            </PanelSectionRow>
          ))}
        {state.wake.enabled &&
          state.wake.acpi.map((a) => (
            <PanelSectionRow key={a.name}>
              <Field
                label={`Firmware wake: ${a.name}`}
                description={`${a.enabled ? "Wake allowed" : "Wake blocked"} · up to ${a.sstate}`}
              />
            </PanelSectionRow>
          ))}
        {state.wake.mem_sleep_options.includes("s2idle") && (
          <PanelSectionRow>
            <ToggleField
              label="Keep USB powered in sleep"
              description={`⚠️ May keep your fans running while asleep. Last resort: try the BIOS settings below first. Switches sleep from deep sleep (S3) to s2idle so the adapter stays powered. Current: ${
                SLEEP_LABEL[state.wake.mem_sleep] ?? state.wake.mem_sleep
              }.`}
              checked={state.wake.keep_usb_powered}
              disabled={busy}
              onChange={async (v) => {
                setBusy(true);
                try {
                  const r = await setKeepUsbPowered(v);
                  setState(r);
                  if (!r.ok) toaster.toast({ title: "Xbox Wireless Helper", body: r.message });
                } finally {
                  setBusy(false);
                }
              }}
            />
          </PanelSectionRow>
        )}
        {state.wake.enabled && (
          <PanelSectionRow>
            <Field
              label="Still not waking?"
              description='Check your BIOS: enable "Wake from USB", set "ErP Ready" to "Enabled (S4+S5)", and disable "Deep Sleep" if present. Linux cannot change these.'
            />
          </PanelSectionRow>
        )}
      </PanelSection>

      <PanelSection title="Last reset">
        <PanelSectionRow>
          <Field
            label={formatTime(last?.time)}
            description={
              last?.time
                ? `${last.reason === "resume" ? "Automatic" : "Manual"} · ${last.message}`
                : "No reset yet this session"
            }
          />
        </PanelSectionRow>
      </PanelSection>
    </>
  );
}

export default definePlugin(() => {
  // toast on automatic resets, even when the panel is closed
  const listener = addEventListener<[ResetResult]>("dongle_reset", (r) => {
    toaster.toast({
      title: "Xbox Wireless Helper",
      body: r.ok ? "Adapter reset after wake" : `Reset failed: ${r.message}`,
    });
  });

  return {
    name: "Xbox Wireless Helper",
    titleView: <div className={staticClasses.Title}>Xbox Wireless Helper</div>,
    content: <Content />,
    icon: <FaXbox />,
    onDismount() {
      removeEventListener("dongle_reset", listener);
    },
  };
});
