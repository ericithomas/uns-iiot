"""
SENTRON PAC3220 Modbus TCP emulator with APFC closed-loop control
for the CorePac Energy Intelligence Layer.

This is a drop-in replacement for the original sentron_emulator.py built for
pymodbus==3.6.9.

It keeps the original meter register map at offsets 0 through 42 unchanged.
It adds APFC-only registers on the Incomer meter starting at offset 44.

Meters:
  unit 1  Incomer      main LV switchgear meter, now corrected by APFC
  unit 2  PackLine1    packaging line feeder
  unit 3  Utilities    compressor/chiller/VFD feeder

Original register map, 0-based offsets, float32 big-endian ABCD:
   0  v_l1          V
   2  v_l2          V
   4  v_l3          V
   6  i_l1          A
   8  i_l2          A
  10  i_l3          A
  12  p_tot         kW
  14  q_tot         kVAR
  16  s_tot         kVA
  18  pf_tot        -
  20  freq          Hz
  22  thd_v         %
  24  thd_i         %
  26  p_l1          kW
  28  p_l2          kW
  30  p_l3          kW
  32  pf_l1         -
  34  pf_l2         -
  36  pf_l3         -
  38  kwh           kWh
  40  kvarh         kVARh
  42  demand        kW

APFC extension on unit 1 only:
  44  apfc_pf_before       PF before capacitor correction
  46  apfc_pf_after        PF after capacitor correction
  48  apfc_q_cap_kvar      capacitor kVAR currently injected
  50  apfc_steps_active    number of active capacitor steps
  52  apfc_target_pf       target PF setpoint
  54  apfc_q_before_kvar   load reactive power before correction
  56  apfc_q_after_kvar    net reactive power after correction

Run:
  pip install "pymodbus==3.6.9"
  python3 sentron_emulator_apfc.py
"""

import asyncio
import math
import random
import struct
import threading
import time

from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusSlaveContext,
)
from pymodbus.server import StartAsyncTcpServer

LISTEN_HOST = "0.0.0.0"
LISTEN_PORT = 5020
UPDATE_PERIOD_S = 1.0
NOMINAL_V = 230.0      # phase-to-neutral, UAE 400 V three-phase system
NOMINAL_F = 50.0
REG_COUNT = 100

# Register offsets. Each value is float32, so each key occupies 2 holding registers.
OFF = {
    "v_l1": 0, "v_l2": 2, "v_l3": 4,
    "i_l1": 6, "i_l2": 8, "i_l3": 10,
    "p_tot": 12, "q_tot": 14, "s_tot": 16, "pf_tot": 18,
    "freq": 20, "thd_v": 22, "thd_i": 24,
    "p_l1": 26, "p_l2": 28, "p_l3": 30,
    "pf_l1": 32, "pf_l2": 34, "pf_l3": 36,
    "kwh": 38, "kvarh": 40, "demand": 42,

    # APFC closed-loop extension. These are meaningful on unit 1 only.
    "apfc_pf_before": 44,
    "apfc_pf_after": 46,
    "apfc_q_cap_kvar": 48,
    "apfc_steps_active": 50,
    "apfc_target_pf": 52,
    "apfc_q_before_kvar": 54,
    "apfc_q_after_kvar": 56,
}


def clamp(x, lo, hi):
    return max(lo, min(hi, x))


def pf_from_pq(p_kw, q_kvar):
    s = math.hypot(p_kw, q_kvar)
    return (p_kw / s) if s > 1e-6 else 1.0


class APFCController:
    """
    Discrete capacitor-bank APFC controller.

    This is intentionally not PID. It is a quantized hysteresis controller:
    one capacitor step can change state per decision cycle, and removed steps
    have a discharge lockout before they are allowed back in.
    """

    def __init__(
        self,
        step_kvar=None,
        target_pf=0.98,
        lower_pf=0.965,
        decision_period_s=5.0,
        discharge_lockout_s=15.0,
        min_lagging_kvar=2.0,
    ):
        self.step_kvar = step_kvar or [10.0, 10.0, 10.0, 10.0, 10.0, 10.0]
        self.health = [1.0 for _ in self.step_kvar]  # set a value below 1 later to simulate a weak capacitor
        self.active = [False for _ in self.step_kvar]
        self.blocked_until = [0.0 for _ in self.step_kvar]
        self.switch_count = [0 for _ in self.step_kvar]

        self.target_pf = target_pf
        self.lower_pf = lower_pf
        self.decision_period_s = decision_period_s
        self.discharge_lockout_s = discharge_lockout_s
        self.min_lagging_kvar = min_lagging_kvar
        self.last_decision_t = 0.0

    def actual_step_kvar(self, idx):
        return self.step_kvar[idx] * self.health[idx]

    def active_q_cap(self):
        return sum(self.actual_step_kvar(i) for i, on in enumerate(self.active) if on)

    def active_count(self):
        return sum(1 for on in self.active if on)

    def q_target_for_pf(self, p_kw):
        # Remaining lagging reactive power allowed at the target PF.
        phi = math.acos(clamp(self.target_pf, 0.01, 0.999999))
        return p_kw * math.tan(phi)

    def remove_one_step(self, now):
        # Remove the most recently available/largest active step. Equal steps make this simple.
        active_indices = [i for i, on in enumerate(self.active) if on]
        if not active_indices:
            return False
        idx = active_indices[-1]
        self.active[idx] = False
        self.blocked_until[idx] = now + self.discharge_lockout_s
        self.switch_count[idx] += 1
        return True

    def add_one_step(self, p_kw, q_load_kvar, now):
        q_cap_now = self.active_q_cap()
        available = [
            i for i, on in enumerate(self.active)
            if (not on) and now >= self.blocked_until[i]
        ]
        if not available:
            return False

        # Prefer a step that improves PF without pushing the bus into leading reactive power.
        candidates = []
        for idx in available:
            candidate_q_cap = q_cap_now + self.actual_step_kvar(idx)
            candidate_q_after = q_load_kvar - candidate_q_cap
            if candidate_q_after >= self.min_lagging_kvar:
                candidate_pf = pf_from_pq(p_kw, candidate_q_after)
                candidates.append((candidate_pf, idx))

        if not candidates:
            return False

        # Highest candidate PF while still lagging wins. One step only per decision cycle.
        _, idx = max(candidates, key=lambda item: item[0])
        self.active[idx] = True
        self.switch_count[idx] += 1
        return True

    def step(self, p_kw, q_load_kvar, now):
        pf_before = pf_from_pq(p_kw, q_load_kvar)

        if now - self.last_decision_t >= self.decision_period_s:
            self.last_decision_t = now
            q_cap_now = self.active_q_cap()
            q_after_now = q_load_kvar - q_cap_now
            pf_after_now = pf_from_pq(p_kw, q_after_now)

            if q_after_now < self.min_lagging_kvar:
                # Overcorrected or almost leading. Back off.
                self.remove_one_step(now)
            elif pf_after_now < self.lower_pf:
                # Lagging PF is still too low. Add one safe step.
                self.add_one_step(p_kw, q_load_kvar, now)

        q_cap = self.active_q_cap()
        q_after = q_load_kvar - q_cap
        pf_after = pf_from_pq(p_kw, q_after)

        return {
            "apfc_pf_before": pf_before,
            "apfc_pf_after": pf_after,
            "apfc_q_cap_kvar": q_cap,
            "apfc_steps_active": float(self.active_count()),
            "apfc_target_pf": self.target_pf,
            "apfc_q_before_kvar": q_load_kvar,
            "apfc_q_after_kvar": q_after,
        }


class Load:
    """A single electrical load on a feeder."""

    def __init__(self, name, p_kw, pf, harmonic, duty=0.9):
        self.name = name
        self.p_kw = p_kw
        self.pf = pf
        self.harmonic = harmonic
        self.duty = duty
        self.on = True

    def step(self):
        if random.random() < 0.04:
            self.on = random.random() < self.duty
        if not self.on:
            return 0.0, 0.0, 0.0
        p = self.p_kw * random.uniform(0.85, 1.0)
        phi = math.acos(self.pf)
        q = p * math.tan(phi)
        return p, q, self.harmonic * (p / self.p_kw)


class Meter:
    """A metering point: aggregates its loads into electrical quantities."""

    def __init__(self, name, loads):
        self.name = name
        self.loads = loads
        self.kwh = 0.0
        self.kvarh = 0.0
        self.demand = 0.0

    def step(self, dt_s):
        p = q = 0.0
        harm_weight = 0.0
        for load in self.loads:
            lp, lq, lh = load.step()
            p += lp
            q += lq
            harm_weight += lh

        s = math.hypot(p, q)
        pf = (p / s) if s > 1e-6 else 1.0

        bal = [1 / 3 + random.uniform(-0.02, 0.02) for _ in range(3)]
        bal = [b / sum(bal) for b in bal]
        p_ph = [p * b for b in bal]
        pf_ph = [clamp(pf + random.uniform(-0.02, 0.02), 0.0, 1.0) for _ in range(3)]

        v_ph = [NOMINAL_V * random.uniform(0.985, 1.005) for _ in range(3)]

        i_ph = []
        for k in range(3):
            s_ph = p_ph[k] / pf_ph[k] if pf_ph[k] > 1e-3 else 0.0
            i_ph.append((s_ph * 1000.0) / v_ph[k] if v_ph[k] > 1 else 0.0)

        freq = NOMINAL_F + random.uniform(-0.05, 0.05)
        thd_i = 2.0 + 25.0 * min(1.0, harm_weight)
        thd_v = 0.8 + 0.15 * thd_i

        self.kwh += p * (dt_s / 3600.0)
        self.kvarh += q * (dt_s / 3600.0)
        self.demand = max(self.demand * 0.999, p)

        return {
            "v_l1": v_ph[0], "v_l2": v_ph[1], "v_l3": v_ph[2],
            "i_l1": i_ph[0], "i_l2": i_ph[1], "i_l3": i_ph[2],
            "p_tot": p, "q_tot": q, "s_tot": s, "pf_tot": pf,
            "freq": freq, "thd_v": thd_v, "thd_i": thd_i,
            "p_l1": p_ph[0], "p_l2": p_ph[1], "p_l3": p_ph[2],
            "pf_l1": pf_ph[0], "pf_l2": pf_ph[1], "pf_l3": pf_ph[2],
            "kwh": self.kwh, "kvarh": self.kvarh, "demand": self.demand,
        }


def f32_to_regs(value):
    hi, lo = struct.unpack(">HH", struct.pack(">f", float(value)))
    return [hi, lo]


def build_meters():
    pack = Meter("PackLine1", [
        Load("drive_motor", 18.0, 0.84, harmonic=0.05, duty=0.95),
        Load("conveyor", 7.5, 0.80, harmonic=0.04, duty=0.85),
        Load("heater_seal", 6.0, 0.99, harmonic=0.0, duty=0.6),
    ])
    util = Meter("Utilities", [
        Load("compressor", 30.0, 0.82, harmonic=0.08, duty=0.8),
        Load("hvac_chiller", 45.0, 0.86, harmonic=0.06, duty=0.7),
        Load("vfd_pump", 22.0, 0.95, harmonic=0.45, duty=0.75),
    ])
    incomer = Meter("Incomer", [
        Load("unmetered_misc", 12.0, 0.90, harmonic=0.05, duty=0.9),
    ])
    return incomer, pack, util


def make_context():
    slaves = {
        uid: ModbusSlaveContext(
            hr=ModbusSequentialDataBlock(0, [0] * REG_COUNT),
            zero_mode=True,
        )
        for uid in (1, 2, 3)
    }
    return ModbusServerContext(slaves=slaves, single=False)


def start_updater(context, incomer, pack, util):
    apfc = APFCController()
    inc_energy = {"kwh": 0.0, "kvarh": 0.0, "demand": 0.0}

    def write_meter(unit_id, values):
        for key, off in OFF.items():
            if key in values:
                context[unit_id].setValues(3, off, f32_to_regs(values[key]))

    def updater():
        last = time.time()
        while True:
            now = time.time()
            dt = now - last
            last = now

            pack_vals = pack.step(dt)
            util_vals = util.step(dt)
            misc_vals = incomer.step(dt)

            # Raw bus load before correction.
            p = pack_vals["p_tot"] + util_vals["p_tot"] + misc_vals["p_tot"]
            q_before = pack_vals["q_tot"] + util_vals["q_tot"] + misc_vals["q_tot"]

            # APFC decision and corrected bus Q.
            apfc_vals = apfc.step(p, q_before, now)
            q_after = apfc_vals["apfc_q_after_kvar"]
            pf_before = apfc_vals["apfc_pf_before"]
            pf_after = apfc_vals["apfc_pf_after"]
            s_after = math.hypot(p, q_after)

            v_ph = [NOMINAL_V * random.uniform(0.985, 1.005) for _ in range(3)]
            i_ph = [
                (p / 3 / pf_after * 1000.0 / v) if (pf_after > 1e-3 and v > 1) else 0.0
                for v in v_ph
            ]

            inc_energy["kwh"] += p * (dt / 3600.0)
            inc_energy["kvarh"] += q_after * (dt / 3600.0)
            inc_energy["demand"] = max(inc_energy["demand"] * 0.999, p)

            inc_vals = {
                "v_l1": v_ph[0], "v_l2": v_ph[1], "v_l3": v_ph[2],
                "i_l1": i_ph[0], "i_l2": i_ph[1], "i_l3": i_ph[2],
                "p_tot": p, "q_tot": q_after, "s_tot": s_after, "pf_tot": pf_after,
                "freq": NOMINAL_F + random.uniform(-0.05, 0.05),
                "thd_v": max(pack_vals["thd_v"], util_vals["thd_v"]),
                "thd_i": (pack_vals["thd_i"] + util_vals["thd_i"]) / 2.0,
                "p_l1": p / 3, "p_l2": p / 3, "p_l3": p / 3,
                "pf_l1": pf_after, "pf_l2": pf_after, "pf_l3": pf_after,
                "kwh": inc_energy["kwh"],
                "kvarh": inc_energy["kvarh"],
                "demand": inc_energy["demand"],
            }
            inc_vals.update(apfc_vals)

            write_meter(1, inc_vals)
            write_meter(2, pack_vals)
            write_meter(3, util_vals)

            print(
                f"[{time.strftime('%H:%M:%S')}] "
                f"Incomer {p:6.1f} kW PF {pf_before:0.3f}->{pf_after:0.3f} "
                f"APFC {int(apfc_vals['apfc_steps_active'])} steps "
                f"{apfc_vals['apfc_q_cap_kvar']:4.1f} kVAR | "
                f"PackLine1 {pack_vals['p_tot']:5.1f} kW PF {pack_vals['pf_tot']:0.3f} | "
                f"Utilities {util_vals['p_tot']:5.1f} kW PF {util_vals['pf_tot']:0.3f} "
                f"THDi {util_vals['thd_i']:4.1f}%"
            )
            time.sleep(max(0.0, UPDATE_PERIOD_S - (time.time() - now)))

    thread = threading.Thread(target=updater, daemon=True)
    thread.start()
    return thread


async def main():
    incomer, pack, util = build_meters()
    context = make_context()
    start_updater(context, incomer, pack, util)

    print(f"SENTRON PAC3220 emulator with APFC listening on {LISTEN_HOST}:{LISTEN_PORT}")
    print("Meters: unit 1 = Incomer + APFC, unit 2 = PackLine1, unit 3 = Utilities")
    print("APFC registers on unit 1: 44 through 56")

    await StartAsyncTcpServer(context=context, address=(LISTEN_HOST, LISTEN_PORT))


if __name__ == "__main__":
    asyncio.run(main())
