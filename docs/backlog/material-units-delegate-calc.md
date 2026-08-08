# material/component units: delegate to calc, never a second engine

Decided (Reto, 2026-07-29): `calc` already does pint-backed unit conversion,
so material/component stay canonical-units-only on write (a non-canonical
`unit=` is rejected, naming the canonical one); callers convert via calc.
This retires the utils/units.py convert-on-write plan. If a read-side
`units=` convenience is ever wanted it must delegate to calc's pint. Nothing
to build until a concrete consumer needs it.
