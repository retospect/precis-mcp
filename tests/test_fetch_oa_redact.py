"""fetch_oa credential redaction (gr502250).

A fetch leg's recorded payload can carry a keyed or presigned URL — the
OpenAlex content key rides in ``?api_key=``, presigned publisher copies in
``sig=``/``token=`` — and a failed download's httpx exception text folds the
full request URL in with it. ``_timed_event`` is the one seam every
``ref_events`` write funnels through, so it is where the payload is masked;
the assertions here pin that invariant without a real store.
"""

from __future__ import annotations

from unittest.mock import MagicMock


def test_timed_event_masks_credential_urls_in_payload() -> None:
    from precis.workers.fetch_oa import _timed_event

    key = "FAKE" + "KEY11"
    store = MagicMock()
    _timed_event(
        store,
        42,
        source="fetcher:test",
        event="fetch_failed",
        payload={
            "doi": "10.1/x",
            "url": f"https://content.openalex.org/w.pdf?api_key={key}&f=1",
            "urls": [
                "https://pub.example/a.pdf",
                f"https://cdn.example/b.pdf?sig={key}&expires=9",
            ],
            "error": f"GET https://x.example/?api_key={key} failed",
        },
    )
    payload = store.append_event.call_args.kwargs["payload"]
    assert key not in str(payload)
    assert payload["doi"] == "10.1/x"
    # Non-credential params and keyless URLs survive untouched.
    assert payload["urls"][0] == "https://pub.example/a.pdf"
    assert "f=1" in payload["url"]
    assert "expires=9" in payload["urls"][1]
