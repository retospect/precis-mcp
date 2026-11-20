"""Read-only publish gates, separate from candidate mint admissibility.

The shared preflight reads stored evidence, trust and timestamp records;
it does not contact the registry or timestamp calendars.
"""

from precis.nanopub.preflight import publish_preflight
from precis.response import Response
from precis.store import Store
from precis.store.types import Ref


def render_publish_preflight(store: Store, ref: Ref) -> Response:
    """Render blocking gates and advisory notes without changing state."""
    issues = publish_preflight(store, ref.id)
    lines = [f"publish-preflight fi{ref.id}"]
    if not any(issue.blocking for issue in issues):
        lines.append("clear to publish")
    lines.extend(
        f"- [{'blocking' if issue.blocking else 'note'}] "
        f"[{issue.check}] {issue.message}"
        for issue in issues
    )
    return Response(body="\n".join(lines))
