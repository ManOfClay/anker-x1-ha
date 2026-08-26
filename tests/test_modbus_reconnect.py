"""Tests for Modbus link self-healing.

A live site lost its meter sensors for nine hours on 2026-08-26. The chain:

1. one request exhausted its retries — pymodbus raised `ModbusIOException`
   ("No response received after 3 retries, continue with next request") and
   left the socket OPEN;
2. the late reply then landed against the NEXT request's transaction id, so
   every following read logged "request ask for transaction_id=1 but got
   id=60794, Skipping" — the stream never resynchronised;
3. `_ensure_connected` saw `client.connected is True` and never reconnected,
   so only reloading the config entry recovered it;
4. meanwhile every optional block timed out, and the first timeout retired
   the external meter block for the life of the coordinator.

`guarded_call` closes the loop on (1)-(3) by handing the caller a chance to
drop the socket before the next request goes out. `OptionalBlocks` closes
(4): a block is retired only after repeated CONSECUTIVE timeouts, and a
retired block is re-probed later so hardware that comes back recovers on its
own.

Same constraint as the other test modules: `homeassistant` isn't installed,
so coordinator.py can't be imported. Behaviour lives in modbus_client.py
(HA-free) and is exercised directly; coordinator.py's use of it is checked
by parsing the source with `ast`.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
import re
from pathlib import Path

import pytest
from pymodbus.exceptions import ConnectionException, ModbusIOException

REPO_ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "coordinator.py"
MODBUS_CLIENT_PY = REPO_ROOT / "custom_components" / "anker_x1" / "modbus_client.py"


def _import_modbus_client():
    """Import modbus_client.py directly, bypassing the HA-importing package."""
    spec = importlib.util.spec_from_file_location(
        "anker_x1_modbus_client_under_test_reconnect", MODBUS_CLIENT_PY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mc = _import_modbus_client()


class _Clock:
    """Deterministic monotonic clock."""

    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# ---------------------------------------------------------------------------
# guarded_call — drop the socket on a transport failure
# ---------------------------------------------------------------------------


class TestGuardedCall:
    def test_returns_result_and_leaves_connection_alone_on_success(self):
        dropped: list[int] = []

        async def call(address, count=None, **unit):
            return ("ok", address, count, unit)

        result = asyncio.run(
            mc.guarded_call(
                call, 10000, count=40, slave=1, on_transport_error=lambda: dropped.append(1)
            )
        )
        assert result == ("ok", 10000, 40, {"slave": 1})
        assert dropped == [], "a successful call must not drop the connection"

    @pytest.mark.parametrize(
        "error",
        [
            ModbusIOException("No response received after 3 retries"),
            ConnectionException("socket down"),
            asyncio.TimeoutError(),
            OSError("broken pipe"),
        ],
        ids=["io", "connection", "timeout", "oserror"],
    )
    def test_drops_connection_then_reraises(self, error):
        dropped: list[int] = []

        async def call(*args, **kwargs):
            raise error

        with pytest.raises(type(error)):
            asyncio.run(
                mc.guarded_call(call, 10000, on_transport_error=lambda: dropped.append(1))
            )
        assert dropped == [1], (
            "a transport failure leaves the pymodbus transaction stream skewed; "
            "the socket must be dropped before the next request"
        )

    def test_non_transport_error_passes_through_untouched(self):
        dropped: list[int] = []

        async def call(*args, **kwargs):
            raise ValueError("decode bug")

        with pytest.raises(ValueError):
            asyncio.run(
                mc.guarded_call(call, 10000, on_transport_error=lambda: dropped.append(1))
            )
        assert dropped == [], "a programming error says nothing about the link"


# ---------------------------------------------------------------------------
# OptionalBlocks — retire only on repeated timeouts, and re-probe later
# ---------------------------------------------------------------------------


class TestOptionalBlocks:
    def test_fresh_block_is_read(self):
        blocks = mc.OptionalBlocks(monotonic=_Clock())
        assert blocks.should_read("M") is True

    def test_single_timeout_does_not_retire(self):
        blocks = mc.OptionalBlocks(threshold=3, monotonic=_Clock())
        assert blocks.record_timeout("M") is False
        assert blocks.should_read("M") is True, (
            "one glitch — e.g. a skewed stream after an IO error — must not "
            "retire a register the unit really does implement"
        )

    def test_consecutive_timeouts_reaching_threshold_retire(self):
        blocks = mc.OptionalBlocks(threshold=3, monotonic=_Clock())
        assert [blocks.record_timeout("M") for _ in range(3)] == [False, False, True]
        assert blocks.should_read("M") is False

    def test_retirement_is_reported_once(self):
        blocks = mc.OptionalBlocks(threshold=2, monotonic=_Clock())
        blocks.record_timeout("M")
        assert blocks.record_timeout("M") is True
        assert blocks.record_timeout("M") is False, "only the transition is reported"

    def test_success_resets_the_streak(self):
        blocks = mc.OptionalBlocks(threshold=3, monotonic=_Clock())
        blocks.record_timeout("M")
        blocks.record_timeout("M")
        blocks.record_success("M")
        assert [blocks.record_timeout("M") for _ in range(2)] == [False, False], (
            "the threshold counts CONSECUTIVE timeouts"
        )

    def test_blocks_are_tracked_independently(self):
        blocks = mc.OptionalBlocks(threshold=2, monotonic=_Clock())
        blocks.record_timeout("H")
        blocks.record_timeout("H")
        assert blocks.should_read("H") is False
        assert blocks.should_read("M") is True

    def test_retired_block_is_reprobed_after_the_interval(self):
        clock = _Clock()
        blocks = mc.OptionalBlocks(threshold=1, retry_after_s=900.0, monotonic=clock)
        blocks.record_timeout("M")
        assert blocks.should_read("M") is False
        clock.advance(899.0)
        assert blocks.should_read("M") is False
        clock.advance(2.0)
        assert blocks.should_read("M") is True, (
            "a meter that comes back must recover without reloading the entry"
        )

    def test_reprobe_that_succeeds_clears_the_retirement(self):
        clock = _Clock()
        blocks = mc.OptionalBlocks(threshold=1, retry_after_s=10.0, monotonic=clock)
        blocks.record_timeout("M")
        clock.advance(11.0)
        assert blocks.should_read("M") is True
        blocks.record_success("M")
        assert blocks.should_read("M") is True
        clock.advance(1.0)
        assert blocks.should_read("M") is True

    def test_reprobe_that_times_out_needs_the_full_threshold_again(self):
        clock = _Clock()
        blocks = mc.OptionalBlocks(threshold=2, retry_after_s=10.0, monotonic=clock)
        blocks.record_timeout("M")
        blocks.record_timeout("M")
        clock.advance(11.0)
        assert blocks.should_read("M") is True
        assert blocks.record_timeout("M") is False
        assert blocks.should_read("M") is True
        assert blocks.record_timeout("M") is True
        assert blocks.should_read("M") is False

    @pytest.mark.parametrize("kwargs", [{"threshold": 0}, {"retry_after_s": 0}])
    def test_rejects_nonsense_configuration(self, kwargs):
        with pytest.raises(ValueError):
            mc.OptionalBlocks(**kwargs)


# ---------------------------------------------------------------------------
# Structural: the coordinator actually wires the recovery in
# ---------------------------------------------------------------------------


def _coordinator_tree() -> ast.Module:
    return ast.parse(COORDINATOR_PY.read_text())


def _method(tree: ast.Module, name: str) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{name} not found in coordinator.py")


class TestCoordinatorWiring:
    def test_drop_connection_closes_the_client(self):
        src = ast.unparse(_method(_coordinator_tree(), "_drop_connection"))
        assert "self._client.close()" in src, (
            "recovery requires closing the socket — a fresh connection is what "
            "restarts the pymodbus transaction counter"
        )

    def test_update_data_runs_under_the_reconnect_guard(self):
        src = ast.unparse(_method(_coordinator_tree(), "_async_update_data"))
        assert "_reconnect_on_transport_error" in src, (
            "a poll that dies on a transport error must drop the socket, or "
            "_ensure_connected sees connected=True forever"
        )

    def test_reconnect_guard_drops_on_transport_errors_only(self):
        src = ast.unparse(_method(_coordinator_tree(), "_reconnect_on_transport_error"))
        assert "TRANSPORT_ERRORS" in src
        assert "_drop_connection" in src
        assert "UpdateFailed" in src, (
            "raising the raw ModbusIOException logs a traceback on every poll"
        )

    def test_every_client_write_is_guarded(self):
        tree = _coordinator_tree()
        unguarded: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            if not node.name.startswith("async_set") and node.name not in {"async_restore"}:
                continue
            src = ast.unparse(node)
            if re.search(r"self\._client\.write_registers?\(", src):
                unguarded.append(node.name)
        assert unguarded == [], (
            f"writes must go through the guarded helper so a failed write also "
            f"drops the socket; unguarded: {unguarded}"
        )

    def test_optional_blocks_helper_replaces_the_one_shot_skip_set(self):
        src = COORDINATOR_PY.read_text()
        assert "OptionalBlocks" in src
        assert "_disabled_blocks" not in src, (
            "the one-shot skip set retired a block on a single timeout"
        )
