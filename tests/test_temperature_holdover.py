"""Tests for the inverter-temperature hold-over (register 10156).

The firmware intermittently reports 0x0000 for 10156 while every other
register in the same Modbus response stays valid — ground-truthed on
fw 1.0.16.1, 2026-08-14: 9 of 29 reads returned 0 with PV voltage and PV
power plausible in the very same response. The coordinator therefore holds
the last good value instead of publishing a 0 °C spike.

Same constraint as the other test modules: `homeassistant` isn't installed,
so coordinator.py can't be imported. Structural claims are verified by
parsing the source with `ast`; the hold-over behaviour itself is replayed
against the decode helper from modbus_client.py, which is HA-agnostic.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "coordinator.py"
MODBUS_CLIENT_PY = REPO_ROOT / "custom_components" / "anker_x1" / "modbus_client.py"


def _load_coordinator_source() -> str:
    return COORDINATOR_PY.read_text()


def _import_modbus_client():
    spec = importlib.util.spec_from_file_location("modbus_client", MODBUS_CLIENT_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Structure: the cache field and the guarded assignment
# ---------------------------------------------------------------------------

def test_last_temperature_cache_initialised_to_none():
    source = _load_coordinator_source()
    assert "self._last_temperature: float | None = None" in source


def test_temperature_only_cached_when_register_is_nonzero():
    """The write into the cache must sit behind a `!= 0` guard."""
    source = _load_coordinator_source()
    assert "raw_temperature = decode_i16(c[0])" in source
    assert "if raw_temperature != 0:" in source
    assert "self._last_temperature = raw_temperature / 10.0" in source


def test_published_temperature_reads_from_the_cache():
    """The value handed to the sensor is the cache, never the raw read."""
    source = _load_coordinator_source()
    assert "inverter_temperature: float | None = self._last_temperature" in source


def test_temperature_still_decoded_from_register_10156():
    """c[0] is base 10156, so the offset must stay 0."""
    tree = ast.parse(_load_coordinator_source())
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "decode_i16"
        and ast.unparse(node) == "decode_i16(c[0])"
    ]
    assert len(calls) == 1


def test_inverter_temperature_still_returned_by_the_coordinator():
    source = _load_coordinator_source()
    assert '"inverter_temperature": inverter_temperature,' in source


# ---------------------------------------------------------------------------
# Behaviour: replay the observed 0x0000 / 0x01da flapping
# ---------------------------------------------------------------------------

def _hold_over(raw_words: list[int]) -> list[float | None]:
    """Mirror of the coordinator's hold-over logic, driven by real decodes."""
    modbus_client = _import_modbus_client()
    last: float | None = None
    published: list[float | None] = []
    for word in raw_words:
        raw = modbus_client.decode_i16(word)
        if raw != 0:
            last = raw / 10.0
        published.append(last)
    return published


def test_zero_reads_hold_the_previous_value():
    # The exact pattern logged against the unit: 0x01da == 47.4 degC.
    published = _hold_over([0x01DA, 0x0000, 0x01DA, 0x0000, 0x0000, 0x01DA])
    assert published == [47.4, 47.4, 47.4, 47.4, 47.4, 47.4]


def test_no_value_before_the_first_good_read():
    """A cold start that begins on a zero read must publish None, not 0.0."""
    published = _hold_over([0x0000, 0x0000, 0x01DA])
    assert published == [None, None, 47.4]


def test_real_changes_still_propagate():
    """Hold-over must not freeze the sensor once it has a value."""
    published = _hold_over([0x01DA, 0x0000, 0x01F4])  # 47.4 -> hold -> 50.0
    assert published == [47.4, 47.4, 50.0]


def test_negative_temperatures_are_not_swallowed():
    """Only exactly 0 is treated as the glitch; sub-zero readings are real."""
    published = _hold_over([0xFFF6, 0x0000, 0xFF9C])  # -1.0 -> hold -> -10.0
    assert published == [-1.0, -1.0, -10.0]
