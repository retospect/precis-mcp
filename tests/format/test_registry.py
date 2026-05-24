"""Unit tests for the :mod:`precis.format` serializer registry.

The registry is the single point of dispatch for ``--format`` and
for any in-process caller that wants to swap between TOON, JSON,
and ASCII-table output. Tests here pin the contract of the public
surface: ``SERIALIZERS``, ``serialize``, ``register``.
"""

from __future__ import annotations

import json

import pytest

from precis import format as fmt


class TestRegistryContents:
    def test_toon_registered(self):
        assert "toon" in fmt.SERIALIZERS

    def test_json_registered(self):
        assert "json" in fmt.SERIALIZERS

    def test_table_registered(self):
        assert "table" in fmt.SERIALIZERS

    def test_entries_are_callables(self):
        for name, f in fmt.SERIALIZERS.items():
            assert callable(f), f"SERIALIZERS[{name!r}] must be callable"


class TestSerialize:
    def test_toon_dispatches_to_toon_dump(self):
        rows = [{"a": "1", "b": "2"}]
        out = fmt.serialize(rows, format="toon")
        assert out == "a\tb\n1\t2"

    def test_json_dispatches_to_json_dumps(self):
        rows = [{"a": "1", "b": "2"}]
        out = fmt.serialize(rows, format="json")
        # Parseable, structure-preserving.
        assert json.loads(out) == rows

    def test_table_dispatches_to_table_render(self):
        rows = [{"a": "1"}]
        out = fmt.serialize(rows, format="table")
        # Coarse shape check — full table contract lives in
        # test_table.py. Here we just confirm the dispatch lands.
        assert "a" in out

    def test_default_format_is_toon(self):
        rows = [{"a": "1", "b": "2"}]
        out = fmt.serialize(rows)
        assert out == fmt.serialize(rows, format="toon")

    def test_unknown_format_raises_value_error(self):
        with pytest.raises(ValueError) as exc_info:
            fmt.serialize([{"a": "1"}], format="yaml")
        # Error message must list the known names so the operator
        # can correct their flag immediately.
        msg = str(exc_info.value)
        assert "yaml" in msg
        assert "toon" in msg
        assert "json" in msg

    def test_kwargs_passed_through(self):
        # `serialize(..., sep=",")` forwards to the toon serializer.
        rows = [{"a": "1", "b": "2"}]
        out = fmt.serialize(rows, format="toon", sep=",")
        assert out == "a,b\n1,2"


class TestRegister:
    def teardown_method(self):
        # Each test that mutates the registry restores it via this
        # hook so the module-level dict stays clean across tests.
        fmt.SERIALIZERS.pop("xtest", None)

    def test_register_adds_a_new_format(self):
        def my_fmt(data, **_kw):
            return f"X{data!r}"

        fmt.register("xtest", my_fmt)
        assert "xtest" in fmt.SERIALIZERS

    def test_registered_format_is_dispatchable(self):
        def my_fmt(data, **_kw):
            return f"X{len(data)}"

        fmt.register("xtest", my_fmt)
        out = fmt.serialize([{"a": "1"}], format="xtest")
        assert out == "X1"

    def test_register_overwrites_existing(self):
        # Overwriting an existing name is intentional — handy for
        # tests, and operators can swap a registered renderer at
        # runtime if they want a custom shape.
        def my_toon(data, **_kw):
            return "CUSTOM"

        fmt.register("xtest", my_toon)
        assert fmt.SERIALIZERS["xtest"] is my_toon
