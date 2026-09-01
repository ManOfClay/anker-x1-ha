"""Tests for the native daily-energy registers 10020, 10024 and 10260.

The firmware DOES reset its daily registers, contrary to the premise the
removed AnkerX1DailyEnergySensor was built on. Ground-truthed on fw 1.0.16.1
across the 2026-09-02 00:00:48 local rollover, reading block A and block F
directly off the unit:

    10016 Daily PV          97.02 -> 0.00      10018 Total PV      3624.20 held
    10020 Daily BattChg      6.35 -> 0.00      10022 Total BattChg  259.68 held
    10024 Daily Load        28.13 -> 0.00      10026 "Total" Load    28.13 -> 0.00
    10028 Daily Purchased    0.30 -> 0.00      10030 "Total" Purch    0.30 -> 0.00
    10032 Daily Feed-in     69.67 -> 0.00      10034 "Total" Feed    69.67 -> 0.00

Only 10018 and 10022 are genuine lifetime counters. The spec's "Total" label
on 10026/10030/10034 does not describe this firmware, and 10028/10032 carry
values bit-identical to 10030/10034, so decoding them would only duplicate
sensors that already exist.

Same constraint as the other test modules: `homeassistant` isn't installed,
so sensor.py / coordinator.py are inspected with `ast`, while the decode
helpers in modbus_client.py run for real.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENSOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "sensor.py"
COORDINATOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "coordinator.py"
MODBUS_CLIENT_PY = REPO_ROOT / "custom_components" / "anker_x1" / "modbus_client.py"

NEW_KEYS = {"battery_charge_energy", "battery_discharge_energy", "load_energy_today"}

# Decoding these would duplicate grid_bought_total / grid_fed_in_total, which
# carry the same bits and reset with them.
MIRRORED_ADDRESSES = ("10028", "10032", "10258", "10262")


def _load_modbus_client():
    spec = importlib.util.spec_from_file_location(
        "anker_x1_modbus_client_native_daily", MODBUS_CLIENT_PY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_descriptions() -> dict[str, dict[str, object]]:
    tree = ast.parse(SENSOR_PY.read_text())
    assign = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "NUMERIC_SENSOR_DESCRIPTIONS"
    )
    assert isinstance(assign.value, ast.Tuple)
    out: dict[str, dict[str, object]] = {}
    for call in assign.value.elts:
        assert isinstance(call, ast.Call)
        kwargs: dict[str, object] = {}
        for kw in call.keywords:
            if kw.arg is None:
                continue
            try:
                kwargs[kw.arg] = ast.literal_eval(kw.value)
            except (ValueError, TypeError):
                kwargs[kw.arg] = ast.unparse(kw.value)
        key = kwargs["key"]
        assert isinstance(key, str)
        out[key] = kwargs
    return out


def _coordinator_return_keys() -> set[str]:
    tree = ast.parse(COORDINATOR_PY.read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_update_data"
    )
    ret = next(
        node
        for node in ast.walk(func)
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict)
    )
    assert isinstance(ret.value, ast.Dict)
    return {
        k.value
        for k in ret.value.keys
        if isinstance(k, ast.Constant) and isinstance(k.value, str)
    }


# ---------------------------------------------------------------------------
# Decode behaviour — the raws observed on the unit
# ---------------------------------------------------------------------------

def test_block_a_daily_offsets_decode_observed_values() -> None:
    """a[20:22] and a[24:26] carry daily charge and daily load consumption."""
    mc = _load_modbus_client()
    a = [0] * 40
    a[20], a[21] = 635, 0      # 10020 -> 6.35 kWh
    a[22], a[23] = 25968, 0    # 10022 -> 259.68 kWh (lifetime, for contrast)
    a[24], a[25] = 2813, 0     # 10024 -> 28.13 kWh
    assert mc.decode_u32_le(a[20:22]) / 100.0 == 6.35
    assert mc.decode_u32_le(a[22:24]) / 100.0 == 259.68
    assert mc.decode_u32_le(a[24:26]) / 100.0 == 28.13


def test_block_f_daily_discharge_offset() -> None:
    """f[2:4] is the daily discharge; f[6:8] stays the lifetime total."""
    mc = _load_modbus_client()
    f = [0] * 8
    f[2], f[3] = 687, 0        # 10260 -> 6.87 kWh
    f[6], f[7] = 24823, 0      # 10264 -> 248.23 kWh
    assert mc.decode_u32_le(f[2:4]) / 100.0 == 6.87
    assert mc.decode_u32_le(f[6:8]) / 100.0 == 248.23


def test_daily_registers_survive_the_midnight_zero() -> None:
    """A reset reads as a clean 0, not as a wrapped or negative value."""
    mc = _load_modbus_client()
    assert mc.decode_u32_le([0, 0]) == 0


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_coordinator_exposes_the_native_daily_keys() -> None:
    assert NEW_KEYS <= _coordinator_return_keys()


def test_sensors_exist_for_the_native_daily_keys() -> None:
    descriptions = _load_descriptions()
    missing = NEW_KEYS - descriptions.keys()
    assert not missing, f"no sensor description for {sorted(missing)}"
    for key in sorted(NEW_KEYS):
        kwargs = descriptions[key]
        assert kwargs["device_class"] == "SensorDeviceClass.ENERGY", key
        assert kwargs["state_class"] == "SensorStateClass.TOTAL_INCREASING", key
        assert kwargs["native_unit_of_measurement"] == "UnitOfEnergy.KILO_WATT_HOUR", key


# ---------------------------------------------------------------------------
# Lock-ins
# ---------------------------------------------------------------------------

def test_derived_daily_energy_machinery_is_gone() -> None:
    """The baseline-subtraction sensor rested on a premise the unit disproves."""
    source = SENSOR_PY.read_text()
    for name in ("AnkerX1DailyEnergySensor", "_RestoredBaseline", "ExtraStoredData"):
        assert name not in source, f"{name} should have been removed"


def test_mirrored_registers_stay_undecoded() -> None:
    """10028/10032 mirror 10030/10034, 10258/10262 mirror 10020/10022.

    They may be named in comments -- the reasoning is worth keeping -- but must
    not be decoded into their own values.
    """
    tree = ast.parse(COORDINATOR_PY.read_text())
    func = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_update_data"
    )
    # The offsets those addresses would occupy, per block base.
    forbidden = {"a[28:30]", "a[32:34]", "f[0:2]", "f[4:6]"}
    sliced = {
        ast.unparse(node)
        for node in ast.walk(func)
        if isinstance(node, ast.Subscript)
    }
    assert not (forbidden & sliced), f"mirrored register decoded: {forbidden & sliced}"
