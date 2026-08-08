# Test-suite setup tax — serialized template clones

The suite is setup-dominated (~340 s fixture setup vs ~120 s test logic;
7,774 tests, ~100 s wall @ -n6): the 6 per-worker FILE_COPY template clones
run fully serialized under the session advisory lock (the last worker waits
behind all prior clones). Options, none free: cap gate workers; shrink the
template; let clones proceed with less lock overlap — measure before
touching (real correctness/speed tradeoff). The per-test TRUNCATE base is
already the cheap isolation choice. Separate gap: no coverage is measured
anywhere (no pytest-cov). Owner `tests/conftest.py::_initialise_test_db`.
