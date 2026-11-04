"""Slider links discard stale coordinate aliases before a reload."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

import pytest

from precis_pathway.environment import Environment
from tests._node import needs_node

_TEMPLATE = (
    Path(__file__).resolve().parents[2]
    / "src/precis_web/templates/pourbaix/detail.html.j2"
)

_HARNESS = """
const linkedEnvironment = LINKED_ENVIRONMENT;
const environmentUDirty = true, environmentPhDirty = true;
const uEl = {value: '-0.6', dataset: {default: '0'}};
const phEl = {value: '8', dataset: {default: '7'}};
const resetT = {checked: false};
const near = (a, b) => Math.abs(a - b) < 1e-9;
const oldQuery = '?U=-0.1&pH=2&u=-0.2&ph=3&keep=yes';
const window = {location: {href: 'https://example.com/pourbaix/st1' + oldQuery}};
const anchor = (path) => ({
  dataset: {}, href: path + oldQuery + '#point',
  getAttribute() { return this.href; },
  setAttribute(name, value) { this.href = value; },
});
const carry = anchor('/pourbaix/st2');
const pathway = anchor('/refs/pathway/pw1');
const document = {
  querySelectorAll(selector) {
    return selector === 'a.pbx-carry' ? [carry] : [pathway];
  },
  getElementById() { return null; },
};
let address;
const history = {replaceState(state, title, url) { address = url; }};
PAGE_FUNCTIONS
syncLinks(true);
console.log(JSON.stringify([address, carry.href, pathway.href]));
"""


@needs_node
@pytest.mark.parametrize("canonical", [False, True])
def test_slider_links_remove_all_old_coordinate_aliases(
    tmp_path: Path, canonical: bool
) -> None:
    template = _TEMPLATE.read_text(encoding="utf-8")
    start = template.index("  function bankParams()")
    end = template.index("  // ── slice 4", start)
    linked = (
        {"T": 350, "electrochemical": {"U_vs_RHE": -0.1, "pH": 2}}
        if canonical
        else None
    )
    script = tmp_path / "pourbaix_links.js"
    script.write_text(
        _HARNESS.replace("LINKED_ENVIRONMENT", json.dumps(linked)).replace(
            "PAGE_FUNCTIONS", template[start:end]
        ),
        encoding="utf-8",
    )
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    address, carry, pathway = json.loads(result.stdout)
    for url in (address, carry, pathway):
        params = dict(parse_qsl(urlsplit(url).query))
        assert "U" not in params
        assert "pH" not in params
        assert params["keep"] == "yes"
        if canonical:
            assert "u" not in params
            assert "ph" not in params
        restored = Environment.from_url(params)
        assert restored.electrochemical == {"U_vs_RHE": -0.6, "pH": 8.0}
    assert urlsplit(carry).fragment == "point"
    assert urlsplit(pathway).fragment == "point"
    assert dict(parse_qsl(urlsplit(address).query))["reset"] == "0"
    assert "reset" not in dict(parse_qsl(urlsplit(pathway).query))
