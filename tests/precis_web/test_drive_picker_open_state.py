"""gr477844: the Kinds/Filters <details> must survive filter-change navigations."""

from __future__ import annotations

from pathlib import Path

TEMPLATE = (
    Path(__file__).resolve().parents[2] / "src/precis_web/templates/drive/index.html.j2"
).read_text(encoding="utf-8")


def test_picker_persists_open_state_across_filter_submit():
    # Filter changes requestSubmit() a GET, re-rendering <details> closed; the
    # open state is saved on toggle and restored in init().
    assert "sessionStorage.getItem('drive.pickerOpen') === '1'" in TEMPLATE
    assert "sessionStorage.setItem('drive.pickerOpen'" in TEMPLATE
    assert '@toggle="savePicker()"' in TEMPLATE


def test_picker_closes_only_on_outside_click_or_escape():
    assert '@click.outside="closePicker($event)"' in TEMPLATE
    assert '@keydown.escape="$refs.picker.open = false"' in TEMPLATE
    # The "Active" summary button opens the picker and must not be treated as outside.
    assert "data-picker-opener" in TEMPLATE
