"""
Hot water unit heater with a 2-position valve and an aquastat fan interlock.

Physical arrangement being simulated
------------------------------------

      HW supply ──[branch piping]──[2-pos valve]──●aquastat──[coil]── HW return
                                                     │
                                         (N.O., closes on temp rise)
                                                     │
            L1 ──[disconnect]──[aquastat contact]──[thermal protector]──(fan motor)── N
                                         ▲
                                    CT on motor lead → 0-5 V → ALC AI (0-10 A)

Sequence: the controller opens the valve on a call for heat; hot water purges
the cold branch piping and heats the coil and supply pipe; when the strap-on
aquastat reaches its close setpoint (90 F) the fan runs.  When the valve
closes the coil and pipe cool, and the fan keeps running until the aquastat
opens (80 F).  The controller has no fan command (unless the optional
`fan_control: controller_and_aquastat` change order is enabled).

Thermal model (lumped, energy-balance based; units BTU, F, hours):

  branch water   T_br   : purged toward HW supply temp when flowing,
                          cools toward the space when stagnant
  coil (water+metal) T_c: C_c dTc/dt = m_cp*(T_br - T_c) - UA_coil*(T_c - T_sp)
  supply pipe at aquastat T_p: follows T_br when flowing, otherwise
                          conducts toward the coil and the space
  space          T_sp   : C_sp dTsp/dt = Q_coil + Q_int - UA_sp*(T_sp - OAT)

UA_coil blends between natural convection (fan off) and forced convection
(fan at speed).
"""

from __future__ import annotations

import math

from .base import EquipmentModel, FaultDef, ParamInfo, SignalDef


def _approach(value: float, target: float, h: float, tau: float) -> float:
    """Exact first-order lag step."""
    if tau <= 0:
        return target
    return target + (value - target) * math.exp(-h / tau)


class HotWaterUnitHeater(EquipmentModel):
    TYPE = "hw_unit_heater"
    DESCRIPTION = "Hot water unit heater, 2-position valve, aquastat fan interlock"

    DEFAULTS = {
        # --- hot water / coil --------------------------------------------
        "hw_supply_temp": 180.0,        # F, plant loop supply temperature
        "hw_flow_gpm": 3.0,             # gpm through a fully open valve
        "coil_ua_fan": 250.0,           # BTU/h-F, coil UA with fan at speed
        "coil_ua_natural": 20.0,        # BTU/h-F, coil UA with fan off
        "coil_capacitance": 10.0,       # BTU/F, coil water + copper/aluminum
        "branch_purge_tau": 20.0,       # s, time to purge cold branch water at full flow
        "branch_cool_tau": 1200.0,      # s, stagnant branch cooling to space
        # --- supply pipe at the aquastat ---------------------------------
        "pipe_flow_tau": 10.0,          # s, pipe wall + aquastat bulb lag with flow
        "pipe_to_coil_tau": 240.0,      # s, conduction toward coil when stagnant
        "pipe_to_space_tau": 900.0,     # s, loss to space when stagnant
        # --- valve -------------------------------------------------------
        "valve_open_time": 20.0,        # s, actuator drive-open stroke time
        "valve_close_time": 12.0,       # s, spring-return close time
        # --- aquastat ----------------------------------------------------
        "aquastat_close_temp": 90.0,    # F, contact closes on rise
        "aquastat_open_temp": 80.0,     # F, contact opens on fall
        "fan_control": "aquastat",      # "aquastat" or "controller_and_aquastat"
        # --- motor / CT --------------------------------------------------
        "motor_run_amps": 2.0,          # A, normal running current
        "motor_lra": 6.0,               # A, locked rotor amps
        "motor_spinup_time": 2.0,       # s, start to full speed
        "motor_coast_time": 6.0,        # s, coast to stop
        "protector_trip_locked": 12.0,  # s at LRA before thermal protector opens
        "protector_trip_overload": 150.0,  # s running overloaded before trip
        "protector_reset_time": 180.0,  # s for auto-reset protector to cool
        "ct_range_amps": 10.0,          # A, transducer full scale (5 V)
        "ct_tau": 0.6,                  # s, transducer averaging response
        "ct_noise": 0.02,               # A, reading noise
        # --- space -------------------------------------------------------
        "oat": 30.0,                    # F, outdoor air temp (per unit for now)
        "space_ua": 300.0,              # BTU/h-F, envelope + infiltration
        "space_capacitance": 1200.0,    # BTU/F, air + furnishings
        "internal_gain": 500.0,         # BTU/h, lights, people, equipment
        "initial_space_temp": 64.0,     # F
        "sensor_noise": 0.03,           # F
    }

    CONDITIONS = ["oat", "hw_supply_temp"]
    WEATHER_PARAMS = {"oat": "oa_temp_actual"}   # follows the host weather station when there is one
    PARAM_INFO = {
        "oat": ParamInfo("", "OA temp", units="°F", minimum=-40, maximum=120),
        "hw_supply_temp": ParamInfo("", "HW supply", units="°F", minimum=40, maximum=250),
    }

    INPUTS = [
        SignalDef("valve_cmd", "binary", description="HW valve command (relay)", default=False),
        SignalDef("fan_cmd", "binary",
                  description="Fan command (only used when fan_control = controller_and_aquastat)",
                  default=False),
    ]

    SIGNALS = [
        SignalDef("space_temp", "analog", "degreesFahrenheit", "Space temperature (sensor reading)"),
        SignalDef("space_temp_actual", "analog", "degreesFahrenheit", "True space temperature"),
        SignalDef("fan_amps", "analog", "amperes", "CT transducer reading, scaled 0-10 A"),
        SignalDef("motor_amps", "analog", "amperes", "True motor current"),
        SignalDef("valve_position", "analog", "percent", "Valve position"),
        SignalDef("hw_branch_temp", "analog", "degreesFahrenheit", "Water temp entering the valve"),
        SignalDef("pipe_temp", "analog", "degreesFahrenheit", "HW supply pipe temp at the aquastat"),
        SignalDef("coil_temp", "analog", "degreesFahrenheit", "Average coil temperature"),
        SignalDef("heat_output", "analog", "btusPerHour", "Heat delivered to the space"),
        SignalDef("fan_speed", "analog", "percent", "Fan wheel speed"),
        SignalDef("oat", "analog", "degreesFahrenheit", "Outdoor air temperature"),
        SignalDef("aquastat_closed", "binary", description="Aquastat contact closed", default=False),
        SignalDef("motor_energized", "binary", description="Voltage applied to the motor", default=False),
        SignalDef("fan_running", "binary", description="Fan wheel turning", default=False),
        SignalDef("protector_tripped", "binary", description="Motor thermal protector open", default=False),
    ]

    FAULTS = [
        FaultDef("valve_stuck_closed", "Valve stuck closed / actuator failed", "Valve"),
        FaultDef("valve_stuck_open", "Valve stuck open", "Valve"),
        FaultDef("valve_leak", "Valve passing (leak-by)", "Valve", "number", 0.15, "fraction",
                 "Flow fraction that leaks through the closed valve"),
        FaultDef("no_hot_water", "No hot water from plant (pumps off)", "Hot water"),
        FaultDef("hw_supply_low", "Low HW supply temperature", "Hot water", "number", 85.0, "F",
                 "Plant supply temperature override"),
        FaultDef("aquastat_failed_open", "Aquastat failed open", "Aquastat"),
        FaultDef("aquastat_failed_closed", "Aquastat failed closed", "Aquastat"),
        FaultDef("aquastat_drift", "Aquastat setpoint drifted", "Aquastat", "number", 40.0, "F",
                 "Offset added to the close and open setpoints"),
        FaultDef("disconnect_off", "Disconnect switch off", "Motor"),
        FaultDef("motor_open_winding", "Motor open winding", "Motor"),
        FaultDef("motor_locked_rotor", "Motor seized (locked rotor)", "Motor"),
        FaultDef("motor_overloaded", "Motor overloaded (cycles on protector)", "Motor"),
        FaultDef("bearing_worn", "Worn motor bearings (high amps)", "Motor"),
        FaultDef("ct_failed_zero", "CT/transducer failed (reads 0 A)", "Current transducer"),
        FaultDef("ct_failed_high", "CT/transducer failed (reads full scale)", "Current transducer"),
        FaultDef("space_sensor_offset", "Space sensor out of calibration", "Space sensor", "number",
                 5.0, "F", "Offset added to the sensor reading"),
        FaultDef("space_sensor_open", "Space sensor open circuit", "Space sensor", "number",
                 -40.0, "F", "Value the controller reads with the sensor open"),
    ]

    # ------------------------------------------------------------------

    def reset(self) -> None:
        t0 = self.p["initial_space_temp"]
        self.t_space = t0
        self.t_branch = t0
        self.t_coil = t0
        self.t_pipe = t0
        self.valve_pos = 0.0
        self.aquastat = False
        self.motor_speed = 0.0
        self.motor_amps = 0.0
        self.motor_energized = False
        self.protector_tripped = False
        self.protector_heat = 0.0       # seconds of stress accumulated
        self.protector_cool = 0.0       # seconds since trip
        self.ct_reading = 0.0
        self.q_coil = 0.0

    # ------------------------------------------------------------------

    def _valve_step(self, h: float) -> None:
        target = 1.0 if self.inputs["valve_cmd"] else 0.0
        if self.fault("valve_stuck_closed"):
            target = 0.0  # actuator dead: spring returns the valve closed
        elif self.fault("valve_stuck_open"):
            target = 1.0  # stem bound in the open position
        if target > self.valve_pos:
            self.valve_pos = min(target, self.valve_pos + h / self.p["valve_open_time"])
        elif target < self.valve_pos:
            self.valve_pos = max(target, self.valve_pos - h / self.p["valve_close_time"])

    def _supply_temp(self) -> float:
        if self.fault("no_hot_water"):
            # loop water drifts toward mechanical-room temperature
            return 70.0
        if self.fault("hw_supply_low"):
            return self.fault_value("hw_supply_low")
        return self.p["hw_supply_temp"]

    def _aquastat_step(self) -> None:
        if self.fault("aquastat_failed_open"):
            self.aquastat = False
            return
        if self.fault("aquastat_failed_closed"):
            self.aquastat = True
            return
        offset = self.fault_value("aquastat_drift") if self.fault("aquastat_drift") else 0.0
        close_t = self.p["aquastat_close_temp"] + offset
        open_t = self.p["aquastat_open_temp"] + offset
        if not self.aquastat and self.t_pipe >= close_t:
            self.aquastat = True
        elif self.aquastat and self.t_pipe <= open_t:
            self.aquastat = False

    def _motor_step(self, h: float) -> None:
        p = self.p
        powered = self.aquastat and not self.fault("disconnect_off")
        if p["fan_control"] == "controller_and_aquastat":
            powered = powered and bool(self.inputs["fan_cmd"])

        # thermal protector (auto reset)
        if self.protector_tripped:
            self.protector_cool += h
            if self.protector_cool >= p["protector_reset_time"]:
                self.protector_tripped = False
                self.protector_heat = 0.0

        self.motor_energized = powered and not self.protector_tripped
        conducting = self.motor_energized and not self.fault("motor_open_winding")
        locked = self.fault("motor_locked_rotor")

        if conducting and not locked:
            self.motor_speed = min(1.0, self.motor_speed + h / p["motor_spinup_time"])
        else:
            self.motor_speed = max(0.0, self.motor_speed - h / p["motor_coast_time"])
            if locked:
                self.motor_speed = 0.0

        if not conducting:
            self.motor_amps = 0.0
            self.protector_heat = max(0.0, self.protector_heat - h)
            return

        run = p["motor_run_amps"]
        if self.fault("bearing_worn"):
            run *= 1.35
        if self.fault("motor_overloaded"):
            run *= 1.4
        if locked:
            amps = p["motor_lra"]
        else:
            # current falls from LRA to running amps as the motor comes up to speed
            amps = p["motor_lra"] - (p["motor_lra"] - run) * self.motor_speed
        self.motor_amps = amps

        # protector stress accumulation
        if locked:
            self.protector_heat += h * p["protector_trip_overload"] / p["protector_trip_locked"]
        elif self.fault("motor_overloaded"):
            self.protector_heat += h
        else:
            self.protector_heat = max(0.0, self.protector_heat - h)
        if self.protector_heat >= p["protector_trip_overload"]:
            self.protector_tripped = True
            self.protector_cool = 0.0
            self.motor_energized = False
            self.motor_amps = 0.0

    def _step(self, h: float) -> None:
        p = self.p
        hours = h / 3600.0

        self._valve_step(h)
        leak = self.fault_value("valve_leak") if self.fault("valve_leak") else 0.0
        flow_frac = max(self.valve_pos, leak)
        if self.fault("no_hot_water"):
            flow_frac *= 0.05  # pumps off: only thermosiphon
        m_cp = 500.0 * p["hw_flow_gpm"] * flow_frac  # BTU/h-F

        # branch piping upstream of the valve
        t_sup = self._supply_temp()
        if flow_frac > 0.001:
            self.t_branch = _approach(self.t_branch, t_sup, h, p["branch_purge_tau"] / flow_frac)
        else:
            self.t_branch = _approach(self.t_branch, self.t_space, h, p["branch_cool_tau"])

        # coil energy balance
        ua = p["coil_ua_natural"] + (p["coil_ua_fan"] - p["coil_ua_natural"]) * self.motor_speed
        self.q_coil = ua * (self.t_coil - self.t_space)
        dtc = (m_cp * (self.t_branch - self.t_coil) - self.q_coil) / p["coil_capacitance"]
        self.t_coil += dtc * hours

        # supply pipe / aquastat bulb
        if flow_frac > 0.001:
            self.t_pipe = _approach(self.t_pipe, self.t_branch, h, p["pipe_flow_tau"] / max(flow_frac, 0.05))
        else:
            rate = ((self.t_coil - self.t_pipe) / p["pipe_to_coil_tau"]
                    + (self.t_space - self.t_pipe) / p["pipe_to_space_tau"])
            self.t_pipe += rate * h

        self._aquastat_step()
        self._motor_step(h)

        # space
        q_loss = p["space_ua"] * (self.t_space - p["oat"])
        self.t_space += (self.q_coil + p["internal_gain"] - q_loss) / p["space_capacitance"] * hours

        # current transducer
        self.ct_reading = _approach(self.ct_reading, self.motor_amps, h, p["ct_tau"])

    # ------------------------------------------------------------------

    def _publish(self) -> None:
        p = self.p
        s = self.signals

        # space sensor reading
        if self.fault("space_sensor_open"):
            reading = self.fault_value("space_sensor_open")
        else:
            reading = self.t_space + self.rng.gauss(0.0, p["sensor_noise"])
            if self.fault("space_sensor_offset"):
                reading += self.fault_value("space_sensor_offset")

        # CT reading
        if self.fault("ct_failed_zero"):
            ct = 0.0
        elif self.fault("ct_failed_high"):
            ct = p["ct_range_amps"]
        else:
            ct = self.ct_reading
            ct += abs(self.rng.gauss(0.0, p["ct_noise"])) if ct < 0.05 else self.rng.gauss(0.0, p["ct_noise"])
            ct = min(max(ct, 0.0), p["ct_range_amps"])

        s["space_temp"] = round(reading, 2)
        s["space_temp_actual"] = round(self.t_space, 2)
        s["fan_amps"] = round(ct, 2)
        s["motor_amps"] = round(self.motor_amps, 2)
        s["valve_position"] = round(self.valve_pos * 100.0, 1)
        s["hw_branch_temp"] = round(self.t_branch, 1)
        s["pipe_temp"] = round(self.t_pipe, 1)
        s["coil_temp"] = round(self.t_coil, 1)
        s["heat_output"] = round(self.q_coil, 0)
        s["fan_speed"] = round(self.motor_speed * 100.0, 1)
        s["oat"] = p["oat"]
        s["aquastat_closed"] = self.aquastat
        s["motor_energized"] = self.motor_energized
        s["fan_running"] = self.motor_speed > 0.5
        s["protector_tripped"] = self.protector_tripped
