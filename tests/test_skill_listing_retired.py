"""memory-native-authoring.md test 4b: the skill listing's retirement check.

The skill index rendered from the mirrored graph (``markdown:skills/*``
refs and their ``part-of`` links, minted by ``precis mirror-links``) must
equal the one rendered from the skill handler's file walk of the shipped
root. An empty diff is the retirement condition for the file-walk listing;
plugin skill roots (``_plugin_skill_roots``) each need their own mirror
alias before it fires for them.
"""

from __future__ import annotations

from importlib import resources
from pathlib import Path

from precis.cli.mirror_links import (
    graph_skill_parents,
    skill_parents,
    skill_tree_lines,
    sync_mirror_links,
)
from precis.dispatch import Hub
from precis.handlers._roots import MirrorRoot
from precis.handlers.markdown import MarkdownHandler
from precis.handlers.skill import _walk_skill_root
from precis.store import Store
from precis.utils.skill_common import parse_frontmatter

SKILLS_DIR = Path(__file__).resolve().parents[1] / "src" / "precis" / "data" / "skills"


def test_skill_index_from_graph_equals_file_walk(
    hub: Hub, store: Store, tmp_path: Path
) -> None:
    shipped: dict[str, str] = {}
    _walk_skill_root(resources.files("precis.data.skills"), shipped)
    walk = {slug: parse_frontmatter(text) for slug, text in shipped.items()}
    from_files = skill_tree_lines(skill_parents(walk))

    mirror = MirrorRoot(path=SKILLS_DIR, ro=True)
    handler = MarkdownHandler(hub=hub, root=tmp_path, mirrors={"skills": mirror})
    sync_mirror_links(store, handler)
    from_graph = skill_tree_lines(graph_skill_parents(store))

    assert len(from_files) > 100
    assert from_graph == from_files
