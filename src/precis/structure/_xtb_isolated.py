"""GFN2-xTB in a child process — :class:`IsolatedTBLite`, the calculator
:func:`precis.structure.relax._xtb_calculator` returns where tblite cannot
share a process with torch (:func:`precis.structure.relax._xtb_needs_isolation`,
gr495146). Kept out of ``relax`` so ASE's calculator base is only imported
when the isolated path is taken.
"""

from __future__ import annotations

import multiprocessing

from ase.calculators.calculator import Calculator, all_changes

from .relax import RelaxUnsupported, _native_tblite


def serve(
    conn, charge: int, multiplicity: int | None, spin_polarization: bool = False
) -> None:
    """Child-process loop behind :class:`IsolatedTBLite`: build the native
    calculator once, then answer ``(atoms, properties)`` requests with the
    calculator's results dict until the pipe closes. Torch is never imported
    here — that is the point of the process."""
    try:
        calc = _native_tblite(
            charge=charge,
            multiplicity=multiplicity,
            spin_polarization=spin_polarization,
        )
    except RelaxUnsupported as exc:
        conn.send(("unsupported", str(exc)))
        return
    except Exception as exc:  # relayed to the parent verbatim
        conn.send(("error", f"{type(exc).__name__}: {exc}"))
        return
    conn.send(("ready", None))
    while True:
        try:
            msg = conn.recv()
        except EOFError:
            return
        if msg is None:
            return
        atoms, properties = msg
        try:
            calc.calculate(atoms, properties, all_changes)
            conn.send(("ok", dict(calc.results)))
        except Exception as exc:  # relayed to the parent verbatim
            conn.send(("error", f"{type(exc).__name__}: {exc}"))


class IsolatedTBLite(Calculator):
    """An ASE calculator that evaluates GFN2-xTB in a spawned child process
    (:func:`serve`), for hosts where tblite cannot share a process with
    torch (:func:`_xtb_needs_isolation`). Same results as the native TBLite,
    one pipe round trip per evaluation; a missing tblite raises
    :class:`RelaxUnsupported` at construction, like the in-process path, and a
    dead child raises ``RuntimeError`` with its exit code — the parent
    survives either way."""

    implemented_properties = [
        "energy",
        "free_energy",
        "energies",
        "forces",
        "charges",
        "dipole",
        "stress",
    ]

    def __init__(
        self,
        *,
        charge: int,
        multiplicity: int | None,
        spin_polarization: bool = False,
        **kwargs,
    ) -> None:
        super().__init__(**kwargs)
        ctx = multiprocessing.get_context("spawn")
        self._conn, child_conn = ctx.Pipe()
        self._proc = ctx.Process(
            target=serve,
            args=(child_conn, charge, multiplicity, spin_polarization),
            daemon=True,
        )
        self._proc.start()
        child_conn.close()
        kind, payload = self._recv()
        if kind == "unsupported":
            self.close()
            raise RelaxUnsupported(payload)
        if kind != "ready":
            self.close()
            raise RuntimeError(f"xtb child failed to start: {payload}")

    def _recv(self):
        try:
            return self._conn.recv()
        except (EOFError, OSError) as exc:
            self._proc.join(timeout=5)
            raise RuntimeError(
                f"xtb child process died (exit code {self._proc.exitcode}) "
                "before answering"
            ) from exc

    def calculate(self, atoms=None, properties=("energy",), system_changes=all_changes):
        super().calculate(atoms, properties, system_changes)
        assert self.atoms is not None
        self._conn.send((self.atoms.copy(), list(properties)))
        kind, payload = self._recv()
        if kind != "ok":
            raise RuntimeError(f"xtb child failed: {payload}")
        self.results = payload

    def close(self) -> None:
        """Stop the child (idempotent)."""
        try:
            self._conn.send(None)
        except (OSError, ValueError):
            pass
        self._conn.close()
        self._proc.join(timeout=5)
        if self._proc.is_alive():
            self._proc.kill()

    def __del__(self) -> None:
        try:
            self.close()
        except Exception:  # interpreter teardown
            pass
