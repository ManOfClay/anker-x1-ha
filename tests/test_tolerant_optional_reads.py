"""Tests for tolerant optional register reads.

pymodbus >=3.6 RAISES `ModbusIOException` when a request exhausts its
retries instead of returning a response whose `isError()` is True, so the
`if not result.isError():` guards that were meant to make registers 10750
(serial), 10253 (pack voltage) and 10620 (external meter) optional never
ran — the exception escaped and aborted the config flow / poll instead.

`read_optional` in modbus_client.py collapses that into three
distinguishable outcomes; callers treat a clean error response (cheap,
retry next poll) differently from a transport timeout (expensive, disable
the block).

Same constraint as tests/test_meter_block.py: `homeassistant` isn't
installed, so coordinator.py / config_flow.py can't be imported.
Structural claims are verified by parsing the source with `ast`;
modbus_client.py has no HA dependency, so `read_optional` and
`decode_string_lowbyte` are exercised directly.
"""

from __future__ import annotations

import ast
import asyncio
import importlib.util
from pathlib import Path

import pytest
from pymodbus.exceptions import ConnectionException, ModbusException, ModbusIOException

REPO_ROOT = Path(__file__).resolve().parent.parent
COORDINATOR_PY = REPO_ROOT / "custom_components" / "anker_x1" / "coordinator.py"
CONFIG_FLOW_PY = REPO_ROOT / "custom_components" / "anker_x1" / "config_flow.py"
MODBUS_CLIENT_PY = REPO_ROOT / "custom_components" / "anker_x1" / "modbus_client.py"


def _import_modbus_client():
    """Import modbus_client.py directly from its file, bypassing the package
    __init__ (which pulls in `homeassistant`)."""
    spec = importlib.util.spec_from_file_location(
        "anker_x1_modbus_client_under_test_optional", MODBUS_CLIENT_PY
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# Fake pymodbus client method / response doubles
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Stand-in for a pymodbus response object."""

    def __init__(self, registers: list[int] | None = None, error: bool = False) -> None:
        self.registers = registers if registers is not None else []
        self._error = error

    def isError(self) -> bool:
        return self._error


def _fake_read(
    *, result: object = None, raises: BaseException | None = None, calls: list | None = None
):
    """Build a bound-method stand-in for `client.read_input_registers`."""

    async def read(address, count=None, **unit):
        if calls is not None:
            calls.append((address, count, unit))
        if raises is not None:
            raise raises
        return result

    return read


# ---------------------------------------------------------------------------
# Behavioural: read_optional — the three distinguishable outcomes
# ---------------------------------------------------------------------------


def test_successful_read_returns_registers_and_no_timeout():
    modbus_client = _import_modbus_client()
    read = _fake_read(result=_FakeResponse([1, 2, 3]))

    got = asyncio.run(modbus_client.read_optional(read, 10620, count=3, slave=1))

    assert got.registers == [1, 2, 3]
    assert got.timed_out is False


def test_successful_read_returns_a_plain_list_copy():
    """Callers slice and cache the result, so it must not alias the response."""
    modbus_client = _import_modbus_client()
    response = _FakeResponse([1, 2, 3])
    read = _fake_read(result=response)

    got = asyncio.run(modbus_client.read_optional(read, 10620, count=3, slave=1))

    assert got.registers == [1, 2, 3]
    assert got.registers is not response.registers


def test_read_passes_address_count_and_unit_kwargs_through():
    modbus_client = _import_modbus_client()
    calls: list = []
    read = _fake_read(result=_FakeResponse([7]), calls=calls)

    asyncio.run(modbus_client.read_optional(read, 10253, count=1, device_id=2))

    assert calls == [(10253, 1, {"device_id": 2})]


def test_clean_modbus_error_response_is_not_a_timeout():
    """The device answered (e.g. illegal data address) — cheap, retry later."""
    modbus_client = _import_modbus_client()
    read = _fake_read(result=_FakeResponse(error=True))

    got = asyncio.run(modbus_client.read_optional(read, 10750, count=8, slave=1))

    assert got.registers is None
    assert got.timed_out is False


@pytest.mark.parametrize(
    "exc",
    [
        ModbusIOException("No response received after 3 retries"),
        asyncio.TimeoutError(),
        OSError("connection reset"),
    ],
    ids=["modbus_io", "asyncio_timeout", "oserror"],
)
def test_transport_failures_report_timed_out(exc):
    """pymodbus >=3.6 raises on retry exhaustion rather than returning an
    error response; every transport failure must surface as timed_out.

    `ConnectionException` is deliberately absent here — see below.
    """
    modbus_client = _import_modbus_client()
    read = _fake_read(raises=exc)

    got = asyncio.run(modbus_client.read_optional(read, 10750, count=8, slave=1))

    assert got.registers is None
    assert got.timed_out is True


def test_connection_loss_is_not_a_timeout():
    """A dropped socket (inverter rebooting for firmware, Wi-Fi blip) says
    nothing about the register, so it must not latch the block off: the
    coordinator disables a block permanently on timed_out, which would kill
    the external-meter sensors until Home Assistant restarts."""
    modbus_client = _import_modbus_client()
    read = _fake_read(raises=ConnectionException("Connection lost"))

    got = asyncio.run(modbus_client.read_optional(read, 10620, count=47, slave=1))

    assert got.registers is None
    assert got.timed_out is False


def test_connection_and_io_exceptions_are_sibling_modbus_exceptions():
    """The split above only holds while `ConnectionException` is a distinct
    `ModbusException` subclass that `ModbusIOException` does not inherit from
    — otherwise one `except` clause silently swallows the other."""
    assert issubclass(ConnectionException, ModbusException)
    assert issubclass(ModbusIOException, ModbusException)
    assert not issubclass(ModbusIOException, ConnectionException)
    assert not issubclass(ConnectionException, ModbusIOException)


def test_unrelated_exceptions_are_not_swallowed():
    """Only transport failures are absorbed — a programming error must not be
    silently reported as a missing optional register."""
    modbus_client = _import_modbus_client()
    read = _fake_read(raises=ValueError("boom"))

    with pytest.raises(ValueError):
        asyncio.run(modbus_client.read_optional(read, 10750, count=8, slave=1))


# ---------------------------------------------------------------------------
# Behavioural: the 10100 PCS serial fallback decodes out of Block B
# ---------------------------------------------------------------------------


def _encode_string_lowbyte(text: str, registers: int) -> list[int]:
    """Encode ASCII into `registers` words, low byte first, NUL padded."""
    raw = text.encode("ascii").ljust(registers * 2, b"\x00")
    return [raw[i] | (raw[i + 1] << 8) for i in range(0, registers * 2, 2)]


def test_pcs_serial_decodes_from_block_b_offset_10_to_22():
    """PCS Serial Number is 10100 STRING x12 — already inside Block B
    (10090-10132), i.e. b[10:22]."""
    modbus_client = _import_modbus_client()
    b = [0] * 43  # synthetic Block B, base address 10090
    b[10:22] = _encode_string_lowbyte("X1H5KS2412345678", 12)

    assert modbus_client.decode_string_lowbyte(b[10:22]) == "X1H5KS2412345678"


def test_unpopulated_pcs_serial_decodes_to_empty_string():
    """A unit that leaves 10100 blank must stay 'serial unknown', not adopt ''."""
    modbus_client = _import_modbus_client()
    b = [0] * 43

    assert modbus_client.decode_string_lowbyte(b[10:22]) == ""


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _coordinator_source() -> str:
    return COORDINATOR_PY.read_text()


def _load_update_data_func() -> ast.AsyncFunctionDef:
    tree = ast.parse(_coordinator_source())
    return next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_async_update_data"
    )


TOLERANT_READERS = {"read_optional", "_read_optional_block"}


def _find_tolerant_read_calls(source: str) -> dict[int, int | None]:
    """Return {address: count} for every tolerant-read call in `source`.

    Covers `read_optional(read, address, count=...)` and the coordinator's
    `_read_optional_block(block, address, count)` wrapper around it.
    """
    tree = ast.parse(source)
    calls: dict[int, int | None] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and _called_name(node) in TOLERANT_READERS):
            continue
        address = _literal(node.args[1])
        if address is None:
            continue  # the helper's own forwarding call — address is a parameter
        count = next(
            (_literal(kw.value) for kw in node.keywords if kw.arg == "count"),
            None,
        )
        if count is None and len(node.args) > 2:
            count = _literal(node.args[2])
        calls[address] = count
    return calls


def _literal(node: ast.AST) -> int | None:
    """Return `node`'s int literal value, or None if it isn't one."""
    if isinstance(node, ast.Constant) and isinstance(node.value, int):
        return node.value
    return None


def _called_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _find_read_input_registers_calls(source: str) -> dict[int, int | None]:
    """Return {start_address: count} for every read_input_registers(...) call."""
    tree = ast.parse(source)
    calls: dict[int, int | None] = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "read_input_registers"
        ):
            addr = ast.literal_eval(node.args[0])
            count = next(
                (ast.literal_eval(kw.value) for kw in node.keywords if kw.arg == "count"),
                None,
            )
            calls[addr] = count
    return calls


def _imported_names(source: str, module: str) -> set[str]:
    """Return the names imported from `module` (relative imports included)."""
    tree = ast.parse(source)
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == module:
            names.update(alias.name for alias in node.names)
    return names


def _assigned_attributes(func: ast.AST) -> set[str]:
    """Return every assignment target in `func`, unparsed (e.g. "self.serial")."""
    return {
        ast.unparse(target)
        for node in ast.walk(func)
        for target in (
            [node.target]
            if isinstance(node, ast.AnnAssign)
            else getattr(node, "targets", [])
        )
    }


def _first_lineno_of_constant(func: ast.AST, value: object) -> int | None:
    return min(
        (
            node.lineno
            for node in ast.walk(func)
            if isinstance(node, ast.Constant) and node.value == value
        ),
        default=None,
    )


# ---------------------------------------------------------------------------
# Structural: coordinator Block D (serial, 10750) is latched after any failure
# ---------------------------------------------------------------------------


def test_coordinator_imports_read_optional():
    assert "read_optional" in _imported_names(_coordinator_source(), "modbus_client")


def test_serial_probe_latch_initialised_in_init():
    tree = ast.parse(_coordinator_source())
    init = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assigned = _assigned_attributes(init)
    assert "self._serial_probe_done" in assigned


def test_block_d_read_is_guarded_by_the_latch():
    """10750 must never be re-probed once it has failed — at a 1 s scan
    interval a 9 s timeout every cycle is catastrophic, not cosmetic."""
    func = _load_update_data_func()
    guard = next(
        node
        for node in ast.walk(func)
        if isinstance(node, ast.If)
        and any(
            isinstance(inner, ast.Constant) and inner.value == 10750
            for inner in ast.walk(node)
        )
    )
    assert "_serial_probe_done" in ast.unparse(guard.test)


def test_block_d_goes_through_read_optional():
    assert _find_tolerant_read_calls(_coordinator_source()).get(10750) == 8


def test_block_d_no_longer_calls_read_input_registers_directly():
    assert 10750 not in _find_read_input_registers_calls(_coordinator_source())


# ---------------------------------------------------------------------------
# Structural: the 10100 serial fallback, ordered after the 10750 attempt
# ---------------------------------------------------------------------------


# An unpopulated 10100 decodes to "", which must stay "serial unknown"
# rather than being adopted as the identity.
SERIAL_FALLBACK_RHS = "decode_string_lowbyte(b[10:22]) or None"


def test_serial_falls_back_to_block_b_offset_10_to_22():
    func = _load_update_data_func()
    assert any(
        ast.unparse(node.value) == SERIAL_FALLBACK_RHS
        for node in ast.walk(func)
        if isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None
    )


def test_serial_fallback_is_ordered_after_the_10750_attempt():
    """10750 must win when it works so unique_id stays stable for existing
    installs; 10100 is only a fallback."""
    func = _load_update_data_func()
    primary = _first_lineno_of_constant(func, 10750)
    fallback = next(
        node.lineno
        for node in ast.walk(func)
        if isinstance(node, (ast.Assign, ast.AnnAssign))
        and node.value is not None
        and ast.unparse(node.value) == SERIAL_FALLBACK_RHS
    )
    assert primary is not None
    assert primary < fallback


# ---------------------------------------------------------------------------
# Structural: optional blocks H (10253) and M (10620) self-disable on timeout
# ---------------------------------------------------------------------------


def test_block_h_and_m_go_through_read_optional():
    calls = _find_tolerant_read_calls(_coordinator_source())
    assert calls.get(10253) == 1
    assert calls.get(10620) == 47


def test_block_h_and_m_no_longer_call_read_input_registers_directly():
    calls = _find_read_input_registers_calls(_coordinator_source())
    assert 10253 not in calls
    assert 10620 not in calls


def test_timed_out_optional_blocks_are_disabled_for_the_coordinator_lifetime():
    source = _coordinator_source()
    tree = ast.parse(source)
    init = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    assigned = _assigned_attributes(init)
    assert "self._disabled_blocks" in assigned

    reader = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_read_optional_block"
    )
    body = ast.unparse(reader)
    assert "read_optional" in body
    assert "timed_out" in body
    assert "self._disabled_blocks.add" in body


def test_disabling_an_optional_block_warns_once_naming_block_and_address():
    source = _coordinator_source()
    assert "_LOGGER.warning" in source
    assert "did not respond" in source


def test_module_docstring_documents_the_fallback_and_self_disabling():
    docstring = ast.get_docstring(ast.parse(_coordinator_source()))
    assert docstring is not None
    assert "10100" in docstring
    assert "disab" in docstring.lower()


# ---------------------------------------------------------------------------
# Structural: config_flow no longer aborts on an unimplemented register
# ---------------------------------------------------------------------------


def _config_flow_source() -> str:
    return CONFIG_FLOW_PY.read_text()


def test_config_flow_imports_shared_string_decoder():
    names = _imported_names(_config_flow_source(), "modbus_client")
    assert "decode_string_lowbyte" in names


def test_config_flow_no_longer_defines_a_duplicate_string_decoder():
    tree = ast.parse(_config_flow_source())
    assert not any(
        isinstance(node, ast.FunctionDef)
        and node.name == "_decode_string_low_byte_first"
        for node in ast.walk(tree)
    )


def test_config_flow_model_read_spans_10090_to_10111():
    """count=22 so the same frame carries the model (10090-10099) and the
    PCS serial fallback (10100-10111) with no extra Modbus traffic."""
    assert _find_tolerant_read_calls(_config_flow_source()).get(10090) == 22


def test_config_flow_serial_read_is_optional():
    assert _find_tolerant_read_calls(_config_flow_source()).get(10750) == 8


def test_config_flow_soc_probe_stays_strict():
    """A failure at 10014 is a genuine cannot_connect, so it must not be
    routed through read_optional."""
    assert _find_read_input_registers_calls(_config_flow_source()).get(10014) == 1
    assert 10014 not in _find_tolerant_read_calls(_config_flow_source())
