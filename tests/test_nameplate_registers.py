"""Tests for the Block B nameplate registers 10124 and 10126.

Both sit inside `read_input_registers(10090, count=43)`, which the integration
already issued, so decoding them adds no traffic. Values read off the unit
(X1-H12K-T, fw 1.0.16.1):

    10124 Rated Power (Pn)    12000 W
    10126 Max Active Power    12000 W

Same constraint as the other test modules: `homeassistant` isn't installed, so
sensor.py / coordinator.py are inspected with `ast`, while the decode helpers
in modbus_client.py run for real.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SENSOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "sensor.py"
COORDINATOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "coordinator.py"
MODBUS_CLIENT_PY = REPO_ROOT / "custom_components" / "anker_x1" / "modbus_client.py"

NAMEPLATE_KEYS = {"rated_power", "max_active_power"}


def _load_modbus_client():
    spec = importlib.util.spec_from_file_location(
        "anker_x1_modbus_client_nameplate", MODBUS_CLIENT_PY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _block_b_fixture() -> list[int]:
    """A Block B response carrying the values observed on the unit."""
    b = [0] * 43
    b[34], b[35] = 12000, 0    # 10124 rated power
    b[36], b[37] = 12000, 0    # 10126 max active power
    return b


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


def _numeric_descriptions() -> dict[str, dict[str, object]]:
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


# ---------------------------------------------------------------------------
# Decode behaviour
# ---------------------------------------------------------------------------

def test_nameplate_offsets_decode_observed_values() -> None:
    mc = _load_modbus_client()
    b = _block_b_fixture()
    assert mc.decode_i32_le(b[34:36]) == 12000
    assert mc.decode_i32_le(b[36:38]) == 12000


def test_nameplate_is_signed_so_a_high_word_cannot_wrap() -> None:
    """i32, per the spec's INT32 -- a 12 kW nameplate must not read negative."""
    mc = _load_modbus_client()
    assert mc.decode_i32_le([12000, 0]) == 12000
    assert mc.decode_i32_le([0xFFFF, 0xFFFF]) == -1


# ---------------------------------------------------------------------------
# Wiring
# ---------------------------------------------------------------------------

def test_coordinator_exposes_the_nameplate_keys() -> None:
    assert NAMEPLATE_KEYS <= _coordinator_return_keys()


def test_nameplate_sensors_are_diagnostic_power() -> None:
    descriptions = _numeric_descriptions()
    missing = NAMEPLATE_KEYS - descriptions.keys()
    assert not missing, f"no sensor description for {sorted(missing)}"
    for key in sorted(NAMEPLATE_KEYS):
        kwargs = descriptions[key]
        assert kwargs["entity_category"] == "EntityCategory.DIAGNOSTIC", key
        assert kwargs["device_class"] == "SensorDeviceClass.POWER", key
        assert kwargs["native_unit_of_measurement"] == "UnitOfPower.WATT", key


def test_nameplate_sensors_carry_no_state_class() -> None:
    """They are constants, so statistics would be noise, not history."""
    descriptions = _numeric_descriptions()
    for key in sorted(NAMEPLATE_KEYS):
        assert "state_class" not in descriptions[key], key
