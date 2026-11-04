"""Smartdraft columns stay operable and keep room for Focus (gr553309)."""

from __future__ import annotations

import json
import shutil
import subprocess
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import pytest

pytest.importorskip("fastapi")

from fastapi.testclient import TestClient

from precis_web.app import create_app
from precis_web.config import WebConfig

from .conftest import FakeRuntime
from .test_smartdraft_reader import SmartDraftFakeStore

_STATIC = Path(__file__).resolve().parents[2] / "src/precis_web/static"
_VIEW = _STATIC.parent / "templates/smartdraft/view.html.j2"
_needs_node = pytest.mark.skipif(
    shutil.which("node") is None, reason="node is not on PATH"
)


class _LayoutMarkup(HTMLParser):
    """Collect controls together with their column ancestors."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.stack: list[tuple[str, dict[str, str | None]]] = []
        self.elements: list[tuple[str, dict[str, str | None], set[str]]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        ancestors = {
            identifier
            for _, ancestor in self.stack
            if (identifier := ancestor.get("id")) is not None
        }
        self.elements.append((tag, attributes, ancestors))
        if tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag: str) -> None:
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                return


@pytest.fixture
def layout_client(tmp_path: Path) -> TestClient:
    return TestClient(
        create_app(
            runtime=FakeRuntime(SmartDraftFakeStore()),
            web_config=WebConfig(corpus_dir=tmp_path),
        )
    )


def test_smartdraft_layout_controls(layout_client: TestClient) -> None:
    response = layout_client.get("/smartdraft/sdt")
    assert response.status_code == 200
    page = _LayoutMarkup()
    page.feed(response.text)
    grips = [a for _, a, _ in page.elements if "data-sd-resize" in a]
    assert {a["data-sd-resize"] for a in grips} == {"search", "collaborate"}
    assert len(grips) == 2
    for grip in grips:
        assert grip["role"] == "separator"
        assert grip["tabindex"] == "0"
        assert grip["aria-orientation"] == "vertical"
        assert grip["aria-controls"] == f"sd-{grip['data-sd-resize']}"
        assert grip.get("aria-label")

    hide = [
        (tag, a, parents) for tag, a, parents in page.elements if "data-sd-hide" in a
    ]
    assert {a["data-sd-hide"] for _, a, _ in hide} == {"search", "collaborate"}
    assert len(hide) == 2
    for tag, control, parents in hide:
        assert tag == "button"
        assert control["type"] == "button"
        assert f"sd-{control['data-sd-hide']}" in parents
        assert "sd-focus" not in parents

    show = [
        (tag, a, parents) for tag, a, parents in page.elements if "data-sd-show" in a
    ]
    assert {a["data-sd-show"] for _, a, _ in show} == {"search", "collaborate"}
    assert len(show) == 2
    for tag, control, parents in show:
        assert tag == "button"
        assert control["type"] == "button"
        assert "sd-focus" in parents
    assert "/static/smartdraft-layout.js" in response.text
    assert "/static/smartdraft-layout.css" in response.text


def _run_js(tmp_path: Path, code: str) -> Any:
    script = tmp_path / "smartdraft_layout.js"
    script.write_text(code, encoding="utf-8")
    result = subprocess.run(
        ["node", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
        timeout=60,
    )
    return json.loads(result.stdout)


def _restore(tmp_path: Path, stored: Any, width: int = 1000) -> dict[str, Any]:
    source = (_STATIC / "smartdraft-layout.js").read_text(encoding="utf-8")
    pure = source[
        source.index("// sd-layout-pure:begin") : source.index("// sd-layout-pure:end")
    ]
    return _run_js(
        tmp_path,
        pure
        + f"\nconsole.log(JSON.stringify(sdLayoutState({json.dumps(stored)}, {width})));",
    )


@_needs_node
@pytest.mark.parametrize("stored", [None, [], "broken", 42, True])
def test_restore_ignores_non_object_storage(tmp_path: Path, stored: Any) -> None:
    state = _restore(tmp_path, stored)
    assert state == {
        "search": 280,
        "collaborate": 280,
        "searchHidden": False,
        "collaborateHidden": False,
    }


@_needs_node
def test_restore_ignores_invalid_widths_and_hidden_flags(tmp_path: Path) -> None:
    state = _restore(
        tmp_path,
        {
            "search": -30,
            "collaborate": "900",
            "searchHidden": "false",
            "collaborateHidden": 1,
        },
    )
    assert state["search"] == state["collaborate"] == 280
    assert state["searchHidden"] is state["collaborateHidden"] is False


@_needs_node
@pytest.mark.parametrize(
    "stored",
    [
        {"search": 5000, "collaborate": 4000},
        {"search": 1e308, "collaborate": 1e308},
        {"search": 1, "collaborate": 5000},
    ],
)
def test_restore_clamps_widths_to_leave_focus_minimum(
    tmp_path: Path, stored: dict[str, float]
) -> None:
    state = _restore(tmp_path, stored)
    assert state["search"] >= 220
    assert state["collaborate"] >= 220
    assert 1000 - state["search"] - state["collaborate"] - 24 >= 360 - 1e-9


@_needs_node
def test_restore_hidden_side_gives_its_space_to_focus(tmp_path: Path) -> None:
    state = _restore(
        tmp_path,
        {"search": 300, "collaborate": 800, "searchHidden": True},
    )
    assert state["searchHidden"] is True
    assert state["collaborateHidden"] is False
    assert state["search"] == 300
    assert 220 <= state["collaborate"] <= 628


@_needs_node
def test_restore_keeps_valid_widths(tmp_path: Path) -> None:
    state = _restore(tmp_path, {"search": 240, "collaborate": 300})
    assert state["search"] == 240
    assert state["collaborate"] == 300


_BROWSER_JS = """
let saved = INITIAL_STORAGE;
const handlers = {};
const documentHandlers = {};
const windowHandlers = {};
const media = {matches: true};
let focused = null;
function element(dataset = {}) {
  return {
    dataset, hidden: false, style: {}, attributes: {}, captured: null,
    setAttribute(key, value) { this.attributes[key] = value; },
    focus() { focused = this; },
    setPointerCapture(id) { this.captured = id; },
    hasPointerCapture(id) { return this.captured === id; },
    releasePointerCapture() { this.captured = null; },
    closest(selector) {
      return {
        '[data-sd-resize]': this.dataset.sdResize,
        '[data-sd-hide]': this.dataset.sdHide,
        '[data-sd-show]': this.dataset.sdShow,
      }[selector] ? this : null;
    },
  };
}
let elements;
function replaceChildren() {
  elements = {'#sd-focus': element()};
  for (const side of ['search', 'collaborate']) {
    elements['#sd-' + side] = element();
    for (const action of ['resize', 'hide', 'show']) {
      const dataset = {};
      dataset['sd' + action[0].toUpperCase() + action.slice(1)] = side;
      elements['[data-sd-' + action + '="' + side + '"]'] = element(dataset);
    }
  }
}
replaceChildren();
const root = {
  clientWidth: 1000, style: {},
  classList: {toggle() {}, add() {}, remove() {}},
  querySelector(selector) { return elements[selector]; },
  addEventListener(kind, handler) { handlers[kind] = handler; },
};
const document = {
  getElementById(id) { return id === 'sd-content' ? root : null; },
  addEventListener(kind, handler) { documentHandlers[kind] = handler; },
};
const window = {
  matchMedia() { return media; },
  localStorage: {
    getItem(key) {
      if (key !== 'precis.smartdraft.layout.v1') throw Error('unexpected key');
      return saved;
    },
    setItem(key, value) {
      if (key !== 'precis.smartdraft.layout.v1') throw Error('unexpected key');
      saved = value;
    },
  },
  addEventListener(kind, handler) { windowHandlers[kind] = handler; },
};
new Function('window', 'document', SOURCE)(window, document);
function control(action, side) {
  return elements['[data-sd-' + action + '="' + side + '"]'];
}
function dispatch(kind, target, values = {}) {
  const event = {
    target, preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; }, ...values,
  };
  handlers[kind](event);
  if (!event.stopped && documentHandlers[kind]) documentHandlers[kind](event);
  return {prevented: !!event.prevented, stopped: !!event.stopped};
}
function snapshot() {
  const result = {columns: root.style.gridTemplateColumns, stored: saved};
  for (const side of ['search', 'collaborate']) {
    result[side] = {
      hidden: elements['#sd-' + side].hidden,
      tabHidden: control('show', side).hidden,
      gripHidden: control('resize', side).hidden,
      width: Number(control('resize', side).attributes['aria-valuenow']),
    };
  }
  return result;
}
"""


def _browser(tmp_path: Path, stored: str | None, actions: str) -> Any:
    source = (_STATIC / "smartdraft-layout.js").read_text(encoding="utf-8")
    harness = _BROWSER_JS.replace("INITIAL_STORAGE", json.dumps(stored)).replace(
        "SOURCE", json.dumps(source)
    )
    return _run_js(tmp_path, harness + "\n" + actions)


@_needs_node
@pytest.mark.parametrize("stored", [None, "{broken", "null", "[]", '"broken"'])
def test_malformed_local_storage_does_not_break_bootstrap(
    tmp_path: Path, stored: str | None
) -> None:
    result = _browser(tmp_path, stored, "console.log(JSON.stringify(snapshot()));")
    assert result["search"]["width"] == result["collaborate"]["width"] == 280
    assert result["search"]["hidden"] is result["collaborate"]["hidden"] is False


@_needs_node
def test_oversized_local_storage_is_clamped_on_bootstrap(tmp_path: Path) -> None:
    result = _browser(
        tmp_path,
        '{"search": 9000, "collaborate": 9000}',
        "console.log(JSON.stringify(snapshot()));",
    )
    assert result["search"]["width"] == result["collaborate"]["width"] == 308
    assert "minmax(360px, 1fr)" in result["columns"]


@_needs_node
def test_keyboard_and_pointer_resize_clamp_and_persist(tmp_path: Path) -> None:
    result = _browser(
        tmp_path,
        None,
        """
const key = dispatch('keydown', control('resize', 'search'), {key: 'ArrowRight'});
const keyboard = snapshot();
dispatch('pointerdown', control('resize', 'collaborate'), {
  button: 0, isPrimary: true, pointerId: 7, clientX: 700,
});
dispatch('pointermove', root, {pointerId: 7, clientX: -2000});
dispatch('pointerup', root, {pointerId: 7});
console.log(JSON.stringify({key, keyboard, pointer: snapshot()}));
""",
    )
    assert result["key"] == {"prevented": True, "stopped": True}
    assert result["keyboard"]["search"]["width"] == 290
    assert json.loads(result["keyboard"]["stored"])["search"] == 290
    pointer = result["pointer"]
    assert pointer["search"]["width"] == 290
    assert pointer["collaborate"]["width"] == 326
    assert json.loads(pointer["stored"])["collaborate"] == 326


@_needs_node
def test_hidden_sides_survive_navigation_and_narrow_screen(tmp_path: Path) -> None:
    result = _browser(
        tmp_path,
        None,
        """
dispatch('click', control('hide', 'search'));
dispatch('click', control('hide', 'collaborate'));
const hidden = snapshot();
replaceChildren();  // The reader replaces #sd-content's children on navigation.
window.__sdLayout.refresh();
const navigated = snapshot();
media.matches = false;
root.clientWidth = 500;
windowHandlers.resize();
const narrow = snapshot();
window.__sdLayout.show('search');  // Search shortcut on a phone preserves preferences.
window.__sdLayout.show('collaborate');
const narrowOpened = snapshot();
media.matches = true;
root.clientWidth = 1000;
windowHandlers.resize();
dispatch('click', control('show', 'search'));
console.log(JSON.stringify({hidden, navigated, narrow, narrowOpened, restored: snapshot(),
  focusReturned: focused === control('hide', 'search')}));
""",
    )
    for stage in ("hidden", "navigated"):
        for side in ("search", "collaborate"):
            assert result[stage][side]["hidden"] is True
            assert result[stage][side]["tabHidden"] is False
            assert result[stage][side]["gripHidden"] is True
    assert result["narrow"]["columns"] == ""
    assert result["narrowOpened"] == result["narrow"]
    for side in ("search", "collaborate"):
        assert result["narrow"][side]["hidden"] is False
        assert result["narrow"][side]["tabHidden"] is True
        assert result["narrow"][side]["gripHidden"] is True
    assert result["restored"]["search"]["hidden"] is False
    assert result["restored"]["collaborate"]["hidden"] is True
    assert result["focusReturned"] is True
    saved = json.loads(result["restored"]["stored"])
    assert saved["searchHidden"] is False
    assert saved["collaborateHidden"] is True


@_needs_node
@pytest.mark.parametrize("prompt", ["Compare the two mechanisms", ""])
def test_collapsed_prompt_survives_pending_revalidation(
    tmp_path: Path, prompt: str
) -> None:
    view = _VIEW.read_text(encoding="utf-8")
    guards = view[
        view.index("  function panelBusy()") : view.index("  // Refill the ±3")
    ]
    result = _browser(
        tmp_path,
        None,
        guards
        + "\nconst prompt = "
        + json.dumps(prompt)
        + """;
const textarea = {
  tagName: 'TEXTAREA', value: '',
  get offsetParent() { return elements['#sd-collaborate'].hidden ? null : root; },
};
root.querySelectorAll = () => [textarea];
let finishFetch;
let stale = false;
let swaps = 0;
let ST = {v: 'cached'};
const cache = {
  get() { return {v: 'cached'}; },
  track() { return new Promise(resolve => { finishFetch = resolve; }); },
  markStale() { stale = true; },
};
function rcache() { return cache; }
function fetchPanel() {}
function isCurrent() { return true; }
function applyPanel() { swaps++; textarea.value = ''; return true; }
function readState() { return {v: 'changed'}; }
function afterSwap() {}
(async () => {
  const pending = revalidate('/smartdraft/test?focus=heading');
  textarea.value = prompt;
  dispatch('click', control('hide', 'collaborate'));
  const hidden = elements['#sd-collaborate'].hidden;
  finishFetch({status: 'ok', entry: {v: 'changed', html: '<fresh panel>'}});
  await pending;
  dispatch('click', control('show', 'collaborate'));
  console.log(JSON.stringify({hidden, value: textarea.value, stale, swaps}));
})();
""",
    )
    assert result == {
        "hidden": True,
        "value": prompt,
        "stale": bool(prompt),
        "swaps": 0 if prompt else 1,
    }


@_needs_node
@pytest.mark.parametrize("action", ["hide", "show", "resize"])
@pytest.mark.parametrize("side", ["search", "collaborate"])
def test_layout_controls_keep_tab_out_of_reader_navigation(
    tmp_path: Path, action: str, side: str
) -> None:
    view = _VIEW.read_text(encoding="utf-8")
    start = view.index("  document.addEventListener('keydown', (e) => {")
    listener = view[start : view.index("\n})();", start)]
    result = _browser(
        tmp_path,
        None,
        """
const ST = {order: [['heading', 0], ['paragraph', 1]], focus: 'heading'};
const RSVP = {on: false};
const jumps = [];
function jump(focus) { jumps.push(focus); }
"""
        + listener
        + f"\nconst target = control({json.dumps(action)}, {json.dumps(side)});"
        + """
document.activeElement = target;
const tab = dispatch('keydown', target, {key: 'Tab'});
const controlJumps = jumps.slice();
document.activeElement = root;
const readerTab = dispatch('keydown', element(), {key: 'Tab'});
console.log(JSON.stringify({tab, controlJumps, readerTab, jumps}));
""",
    )
    assert result["tab"] == {"prevented": False, "stopped": True}
    assert result["controlJumps"] == []
    assert result["readerTab"] == {"prevented": True, "stopped": False}
    assert result["jumps"] == ["paragraph"]
