"""Cross-request batching in :class:`EmbedderService` (gr459844).

Admitted requests share forward passes: query-sized requests go first and
each pass stops at a token budget, so a one-text search embed waits for at
most the pass already running instead of every queued batch. Pinned here:
every request gets back exactly its own vectors in order, query-sized texts
lead the next pass, the budget splits passes, a failing text fails only its
own request, and query-sized requests are admitted on their own slots.
"""

from __future__ import annotations

import threading
import time

import pytest

from precis.embedder_service import EmbedderService, _est_tokens


class _RecordingEmbedder:
    """Deterministic per-text vectors; records each pass's texts and can
    hold a pass open until released."""

    dim = 3
    model = "recording"

    def __init__(self, *, poison: str | None = None) -> None:
        self.passes: list[list[str]] = []
        self.poison = poison
        self.hold = threading.Event()
        self.hold.set()
        self.entered = threading.Event()

    @staticmethod
    def vector(text: str) -> list[float]:
        return [float(len(text)), float(sum(map(ord, text)) % 997), 1.0]

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.passes.append(list(texts))
        self.entered.set()
        self.hold.wait(timeout=10.0)
        if self.poison is not None and self.poison in texts:
            raise ValueError("poison text")
        return [self.vector(t) for t in texts]

    def embed_one(self, text: str) -> list[float]:
        return self.vector(text)

    def is_ready(self) -> bool:
        return True

    def warmup(self) -> None:
        pass

    def unload(self) -> None:
        pass


def _service(embedder: _RecordingEmbedder, **kw: object) -> EmbedderService:
    service = EmbedderService(embedder, revision="t", warm=False, **kw)  # type: ignore[arg-type]
    service.stop_probe()
    return service


def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _queued_texts(service: EmbedderService) -> int:
    with service._sched:
        return sum(
            len(job.texts) - job.next
            for job in service._small_jobs + service._large_jobs
        )


def test_concurrent_requests_each_get_their_own_vectors_in_order() -> None:
    embedder = _RecordingEmbedder()
    embedder.hold.clear()
    service = _service(embedder, max_inflight=64)
    requests = [
        [f"r{r}-t{t}-" + "x" * (r * 7 + t) for t in range(r % 9 + 1)] for r in range(40)
    ]
    results: dict[int, list[list[float]]] = {}

    def run(r: int) -> None:
        results[r] = service.embed(requests[r])

    threads = [threading.Thread(target=run, args=(r,)) for r in range(len(requests))]
    threads[0].start()
    assert embedder.entered.wait(timeout=5.0)  # hold pass 1 so the rest queue
    for t in threads[1:]:
        t.start()
    queued = sum(len(texts) for texts in requests[1:])
    assert _wait_for(lambda: _queued_texts(service) == queued)
    embedder.hold.set()
    for t in threads:
        t.join(timeout=10.0)
    for r, texts in enumerate(requests):
        assert results[r] == [embedder.vector(t) for t in texts]
    # Requests did share passes — otherwise this tested nothing new.
    assert len(embedder.passes) < len(requests)
    assert service.metrics.passes == len(embedder.passes)


def test_query_sized_request_leads_the_next_pass() -> None:
    embedder = _RecordingEmbedder()
    embedder.hold.clear()
    service = _service(embedder, max_inflight=4)

    first = threading.Thread(target=service.embed, args=([f"a{i}" for i in range(8)],))
    first.start()
    assert embedder.entered.wait(timeout=5.0)  # pass 1 is running and held
    batch = threading.Thread(target=service.embed, args=([f"b{i}" for i in range(40)],))
    batch.start()
    assert _wait_for(lambda: _queued_texts(service) == 40)
    query = threading.Thread(target=service.embed, args=(["the query"],))
    query.start()
    assert _wait_for(lambda: _queued_texts(service) == 41)

    embedder.hold.set()
    for t in (first, batch, query):
        t.join(timeout=10.0)
    assert embedder.passes[1][0] == "the query"
    # Batch texts ride along, but the rest of the batch follows in later
    # passes rather than all being packed in with the query.
    assert embedder.passes[1][1:] == [
        f"b{i}" for i in range(len(embedder.passes[1]) - 1)
    ]
    assert sum(len(p) for p in embedder.passes[1:]) == 41


def test_a_pass_carrying_a_query_gets_a_quarter_of_the_budget() -> None:
    embedder = _RecordingEmbedder()
    embedder.hold.clear()
    service = _service(embedder, max_inflight=4, pass_token_budget=4000)

    first = threading.Thread(target=service.embed, args=([f"a{i}" for i in range(8)],))
    first.start()
    assert embedder.entered.wait(timeout=5.0)
    texts = ["m" * 400 for _ in range(30)]  # 102 estimated tokens each
    batch = threading.Thread(target=service.embed, args=(texts,))
    batch.start()
    assert _wait_for(lambda: _queued_texts(service) == 30)
    query = threading.Thread(target=service.embed, args=(["q"],))
    query.start()
    assert _wait_for(lambda: _queued_texts(service) == 31)

    embedder.hold.set()
    for t in (first, batch, query):
        t.join(timeout=10.0)
    with_query = embedder.passes[1]
    assert with_query[0] == "q"
    assert len(with_query) * max(_est_tokens(t) for t in with_query) <= 1000
    # The next pass, with no query waiting, uses the full budget again.
    assert len(embedder.passes[2]) > len(with_query)


def test_token_budget_splits_a_large_request_into_passes() -> None:
    embedder = _RecordingEmbedder()
    texts = ["y" * 4000 for _ in range(10)]  # ~1002 estimated tokens each
    service = _service(embedder, max_inflight=4, pass_token_budget=3000)

    assert service.embed(texts) == [embedder.vector(t) for t in texts]
    assert [len(p) for p in embedder.passes] == [2, 2, 2, 2, 2]
    for p in embedder.passes:
        assert len(p) * max(_est_tokens(t) for t in p) <= 3000


def test_a_long_text_does_not_pad_a_pass_of_short_ones() -> None:
    # The model pads every text in a pass to the longest, so the budget is
    # texts x longest. A first cut summed tokens: one ~4k-token text then
    # shared a pass with 30 short ones and a query riding it took 96 s.
    embedder = _RecordingEmbedder()
    service = _service(embedder, max_inflight=4, pass_token_budget=16_384)
    texts = ["s" * 600 for _ in range(30)]
    texts.insert(17, "L" * 16_000)
    assert service.embed(texts) == [embedder.vector(t) for t in texts]
    for p in embedder.passes:
        assert len(p) * max(_est_tokens(t) for t in p) <= 16_384
    long_pass = next(p for p in embedder.passes if "L" * 16_000 in p)
    assert len(long_pass) <= 4


def test_a_text_over_the_budget_still_runs_alone() -> None:
    embedder = _RecordingEmbedder()
    service = _service(embedder, max_inflight=4, pass_token_budget=100)
    texts = ["short", "z" * 16_000, "short too"]
    assert service.embed(texts) == [embedder.vector(t) for t in texts]
    assert ["z" * 16_000] in embedder.passes


def test_a_failing_text_fails_only_its_own_request() -> None:
    embedder = _RecordingEmbedder(poison="bad")
    embedder.hold.clear()
    service = _service(embedder, max_inflight=4)

    blocker = threading.Thread(
        target=service.embed, args=([f"w{i}" for i in range(5)],)
    )
    blocker.start()
    assert embedder.entered.wait(timeout=5.0)
    outcome: dict[str, object] = {}

    def run(name: str, texts: list[str]) -> None:
        try:
            outcome[name] = service.embed(texts)
        except Exception as exc:
            outcome[name] = exc

    good = threading.Thread(target=run, args=("good", ["fine", "also fine"]))
    bad = threading.Thread(target=run, args=("bad", ["ok", "bad"]))
    good.start()
    bad.start()
    assert _wait_for(lambda: _queued_texts(service) == 4)
    embedder.hold.set()
    for t in (blocker, good, bad):
        t.join(timeout=10.0)

    assert outcome["good"] == [embedder.vector("fine"), embedder.vector("also fine")]
    assert isinstance(outcome["bad"], ValueError)


def test_query_sized_requests_have_their_own_admission_slots() -> None:
    embedder = _RecordingEmbedder()
    embedder.hold.clear()
    service = _service(embedder, max_inflight=1, queue_wait_s=0.05)

    batch = threading.Thread(target=service.embed, args=([f"b{i}" for i in range(20)],))
    batch.start()
    assert embedder.entered.wait(timeout=5.0)
    query_result: dict[str, object] = {}
    query = threading.Thread(
        target=lambda: query_result.__setitem__("v", service.embed(["q"]))
    )
    query.start()
    assert _wait_for(lambda: _queued_texts(service) == 1)  # admitted, not 429
    embedder.hold.set()
    for t in (batch, query):
        t.join(timeout=10.0)
    assert query_result["v"] == [embedder.vector("q")]
    assert service.metrics.rejected_429 == 0


@pytest.mark.parametrize("texts", [[], ["one"]])
def test_trivial_requests(texts: list[str]) -> None:
    embedder = _RecordingEmbedder()
    service = _service(embedder, max_inflight=1)
    assert service.embed(texts) == [embedder.vector(t) for t in texts]


class _OverlapEmbedder(_RecordingEmbedder):
    """Counts threads inside the model at once; warmup encodes slowly."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = 0
        self.max_inside = 0
        self.warm_started = threading.Event()
        self._count = threading.Lock()

    def _enter(self) -> None:
        with self._count:
            self.inside += 1
            self.max_inside = max(self.max_inside, self.inside)

    def _leave(self) -> None:
        with self._count:
            self.inside -= 1

    def warmup(self) -> None:
        self._enter()
        self.warm_started.set()
        time.sleep(0.3)
        self._leave()

    def embed(self, texts: list[str]) -> list[list[float]]:
        self._enter()
        try:
            time.sleep(0.05)
            return [self.vector(t) for t in texts]
        finally:
            self._leave()


def test_warmup_encode_never_overlaps_a_pass() -> None:
    """gr460207: a pass running beside warmup's encode segfaulted MPS."""
    from precis.errors import Upstream

    embedder = _OverlapEmbedder()
    service = EmbedderService(embedder, revision="t", max_inflight=4, warm=True)
    assert embedder.warm_started.wait(timeout=2.0)
    with pytest.raises(Upstream):  # still warming: fail fast, don't queue
        service.embed(["early"])
    assert service._ready.wait(timeout=5.0)
    assert service.embed(["after"]) == [embedder.vector("after")]
    service.stop_probe()
    assert embedder.max_inside == 1


def test_idle_reload_encode_never_overlaps_a_pass() -> None:
    embedder = _OverlapEmbedder()
    service = _service(embedder)
    service._loaded = False  # as after an idle unload
    threads = [
        threading.Thread(target=service.embed, args=([f"t{i}"],)) for i in range(4)
    ]
    probe = threading.Thread(target=service._run_probe_once)
    for t in [*threads, probe]:
        t.start()
    for t in [*threads, probe]:
        t.join(timeout=5.0)
    assert embedder.max_inside == 1
