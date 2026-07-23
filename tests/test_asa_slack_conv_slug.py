from __future__ import annotations

from asa_slack.conv_slug import compute_slug


def test_slug_shape():
    slug = compute_slug(team_id="T1", channel_id="C2", thread_ts="171000.001")
    assert slug == "slack/T1/C2/171000.001"


def test_slug_is_stable_and_deterministic():
    kwargs = dict(team_id="T1", channel_id="C2", thread_ts="171000.001")
    assert compute_slug(**kwargs) == compute_slug(**kwargs)


def test_different_threads_get_different_slugs():
    a = compute_slug(team_id="T1", channel_id="C2", thread_ts="1.0")
    b = compute_slug(team_id="T1", channel_id="C2", thread_ts="2.0")
    assert a != b
