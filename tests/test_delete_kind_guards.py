"""delete guards (gr494831): kind-scoped numeric delete, live-job refusal."""

import importlib
import pkgutil

import pytest

import precis.handlers
from precis.dispatch import Hub
from precis.errors import BadInput, NotFound
from precis.handlers._numeric_ref import NumericRefHandler
from precis.handlers.job import JobHandler
from precis.store import Tag


def _import_every_handler() -> None:
    # handlers register lazily; import each module so __subclasses__ is complete
    for m in pkgutil.iter_modules(precis.handlers.__path__):
        try:
            importlib.import_module(f"precis.handlers.{m.name}")
        except ImportError:  # optional-extra handlers
            continue


def _all_subclasses(
    cls: type[NumericRefHandler],
) -> list[type[NumericRefHandler]]:
    out: list[type[NumericRefHandler]] = []
    for sub in cls.__subclasses__():
        out.append(sub)
        out.extend(_all_subclasses(sub))
    return out


def _base_delete_handlers() -> list[type[NumericRefHandler]]:
    """Every concrete numeric kind that serves delete through the base."""
    found: dict[str, type[NumericRefHandler]] = {}
    for cls in _all_subclasses(NumericRefHandler):
        spec = getattr(cls, "spec", None)
        kind = getattr(spec, "kind", None)
        if not kind or kind in found or not getattr(spec, "supports_delete", False):
            continue
        if cls.delete is not NumericRefHandler.delete:
            continue
        found[kind] = cls
    return [found[k] for k in sorted(found)]


_import_every_handler()
_HANDLERS = _base_delete_handlers()


def test_parametrisation_is_not_vacuous() -> None:
    kinds = {c.spec.kind for c in _HANDLERS}
    assert {"memory", "job", "finding"} <= kinds


@pytest.mark.parametrize("cls", _HANDLERS, ids=lambda c: c.spec.kind)
def test_delete_refuses_ref_of_another_kind(
    hub: Hub, cls: type[NumericRefHandler]
) -> None:
    other = hub.live_store.insert_ref(kind="todo", slug=None, title="Other-kind ref")
    h = cls(hub=hub)
    with pytest.raises(NotFound) as ei:
        h.delete(id=other.id)
    assert "'todo'" in str(ei.value)
    assert "get(kind='todo'" in str(ei.value.next)
    assert hub.live_store.get_ref(kind="todo", id=other.id) is not None


def test_job_delete_refuses_non_terminal(hub: Hub) -> None:
    store = hub.live_store
    job = store.insert_ref(kind="job", slug=None, title="running job")
    store.add_tag(job.id, Tag.parse_strict("STATUS:running", kind="job"))
    h = JobHandler(hub=hub)
    with pytest.raises(BadInput) as ei:
        h.delete(id=job.id)
    assert "STATUS:cancel_requested" in str(ei.value.next)
    assert store.get_ref(kind="job", id=job.id) is not None

    store.add_tag(
        job.id,
        Tag.parse_strict("STATUS:queued", kind="job"),
        replace_prefix=True,
    )
    with pytest.raises(BadInput) as qi:
        h.delete(id=job.id)
    assert "add=['STATUS:cancelled']" in str(qi.value.next)

    store.add_tag(
        job.id,
        Tag.parse_strict("STATUS:cancelled", kind="job"),
        replace_prefix=True,
    )
    assert "deleted" in h.delete(id=job.id).body
