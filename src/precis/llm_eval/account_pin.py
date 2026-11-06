"""``llm eval --account`` — qualify ONE hosted account on ONE model.

A hosted-provider account made through the account door (``llm_account``,
adapter ``openai_compat``) carries its own endpoint + credential in an
``llm.profiles`` entry. A plain ``precis llm eval <model>`` cannot reach
it: :func:`harness.run_eval` dispatches through :func:`router.route`,
whose operator chain can override the candidate model, and
:mod:`.pinned` pins a rung *placement*, never an account. ``--account``
pins the *wire*: every gold-task request goes to exactly that account's
``base_url`` with exactly that account's key for exactly that model id —
``router.route`` is never consulted.

:func:`resolve_account_pin` is the pre-flight. Every refusal lands
(``BadInput``) BEFORE the first request: unknown account, a
non-``openai_compat`` adapter, no resolvable profile, a model outside
the account's ``models``/entitlement, a missing or unresolvable key
(``profile.api_key_ref`` only — never the host-keyed or
``PRECIS_LLM_API_KEY`` fallback), a missing llm card, or no ``calls``
pool with a reported limit applying to the (account, model) pair.
``enabled`` is deliberately NOT a gate: qualifying a disabled account is
the point of the flag, and the eval never writes the flag either.

:func:`account_pinned_dispatch` is the wire. It stamps the pin's profile
+ model onto the request, sends it through the ``OPENAI_COMPAT``
provider directly, paces request starts so no recorded
``rolling:<s≤3600>`` calls pool overruns its limit, halts the whole run
on a provider 429 (the axis voids; nothing records), and logs each call
to the route-log like :func:`router.route` does. Pools with longer
windows are a count check instead: :func:`check_budget` refuses the run
up front when the wired-task count would exceed a recorded limit.

:func:`record_account_eval` writes the outcome where it belongs: card
``meta.account_evals[<account>][<axis>]`` plus a ``measured-eval``
review row provenanced ``account:<name>``. The card-level
``capability`` — keyed by model alone, unable to say WHICH account
measured it — is never touched.

Key hygiene: the secret VALUE is resolved at call time
(:func:`precis.secrets.get_secret`) and never lands on the pin, a log
line, a result row, a review or a card. Before a provider error or reply
can reach any of those surfaces :func:`_scrub` strips the exact key, any
6+-character run of it, ``Bearer``/``Authorization`` header shapes and
the common hosted-provider key shapes, then truncates the message.
"""

from __future__ import annotations

import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from precis.errors import BadInput
from precis.utils.llm.accounts import (
    Account,
    Pool,
    RollingReset,
    get_account,
    list_pools,
    parse_reset_rule,
)
from precis.utils.llm.router import (
    LlmRequest,
    LlmResult,
    Transport,
    provider_for,
    record_dispatch,
    resolve_profile,
)

if TYPE_CHECKING:
    from precis.store import Store

log = logging.getLogger(__name__)

#: Rolling windows at or under one hour pace request STARTS (the gap is
#: ``seconds / limit_reported``); anything longer is a whole-run count
#: check instead — pacing could not stretch a run over a day.
_PACING_WINDOW_MAX_S = 3600


@dataclass(frozen=True, slots=True)
class WindowCap:
    """A calls pool whose window is too long to pace: the run's total
    request count must not exceed ``limit_reported``."""

    pool: str
    limit: float


@dataclass(frozen=True, slots=True)
class AccountPin:
    """The resolved eval target: one account's wire for one model.

    ``api_key_ref`` NAMES the secret; the value never lands here — the
    wire resolves it at call time through
    :func:`precis.secrets.get_secret`, and :func:`_scrub` resolves it
    again only to strip it out of provider text.
    """

    account: str
    model: str
    profile_name: str
    api_key_ref: str
    #: Minimum seconds between request STARTS — the strictest applicable
    #: ``rolling:<s≤3600>`` calls pool (``s / limit_reported``).
    min_gap_s: float
    #: Longer-window calls pools — :func:`check_budget` refuses a run
    #: whose task count would overrun one.
    window_caps: tuple[WindowCap, ...]


_NON_ALNUM = re.compile(r"[^a-z0-9]")


def _model_slug(model_id: str) -> str:
    """The pool-segment slug for a model id: the tail after the last
    ``/``, lower-cased, non-alphanumerics stripped —
    ``openai/gpt-oss-120b`` → ``gptoss120b``."""
    return _NON_ALNUM.sub("", model_id.rsplit("/", 1)[-1].lower())


def _applicable_call_pools(
    account: Account, model: str, pools: list[Pool]
) -> list[Pool]:
    """The ``calls`` pools covering (account, model).

    A ``llm:<account>:<segment>`` row whose segment starts with the
    model's slug covers that model alone (``llm:groq:gptoss120brpm``); a
    segment starting with NO listed model's slug is account-wide and
    covers every model on the account (``llm:openrouter:freerpm``). A
    segment naming a DIFFERENT listed model does not apply.
    """
    prefix = f"llm:{account.name}:"
    target = _model_slug(model)
    listed = {_model_slug(m) for m in account.models} - {""}
    out: list[Pool] = []
    for pool in pools:
        if not pool.provider.startswith(prefix) or pool.unit != "calls":
            continue
        segment = pool.provider[len(prefix) :]
        if segment.startswith(target) or not any(
            segment.startswith(slug) for slug in listed
        ):
            out.append(pool)
    return out


def resolve_account_pin(store: Store, account: str, model: str) -> AccountPin:
    """Resolve ``--account`` + model to a dispatch pin, or refuse.

    Every refusal is a :class:`BadInput` carrying ``next=`` — and every
    check runs before the first request, so a refusal never costs a call.
    ``llm_account.enabled`` is never consulted: qualifying a disabled
    account is the flag's purpose.
    """
    acct = get_account(store, account)
    if acct is None:
        raise BadInput(
            f"unknown llm account {account!r}",
            next="list accounts: get(kind='llm', id='/accounts')",
        )
    if acct.adapter != "openai_compat":
        raise BadInput(
            f"account {account!r} uses adapter {acct.adapter!r} — "
            "--account evals openai_compat accounts only",
            options=["openai_compat"],
        )
    if not acct.profile:
        raise BadInput(
            f"account {account!r} binds no llm.profiles entry",
            next="set profile on the account: "
            "put(kind='llm', args={'account': {'name': ..., 'profile': ...}})",
        )
    profile = resolve_profile(acct.profile)
    if profile is None:
        raise BadInput(
            f"profile {acct.profile!r} (account {account!r}) is missing "
            "from llm.profiles or malformed",
            next="check the llm.profiles app_settings row",
        )
    if (
        model not in acct.models
        or ((acct.entitlement.get("models") or {}).get(model) or {}).get("allowed")
        is False
    ):
        raise BadInput(
            f"model {model!r} is not on account {account!r}",
            options=list(acct.models),
        )
    if not profile.api_key_ref:
        raise BadInput(
            f"profile {acct.profile!r} has no api_key_ref — --account "
            "never falls back to the host-keyed or PRECIS_LLM_API_KEY key",
            next="set api_key_ref on the llm.profiles entry",
        )
    from precis.secrets import get_secret

    if not get_secret(profile.api_key_ref):
        # Name the reference, never a value: the ref is public config,
        # the secret is not.
        raise BadInput(
            f"secret {profile.api_key_ref!r} does not resolve (env or "
            "vault) — the account's key cannot be provisioned",
            next=f"provision the {profile.api_key_ref!r} secret",
        )
    from precis.llm_catalog import LLM_KIND

    if store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=model) is None:
        raise BadInput(
            f"no llm card for model {model!r}",
            next="mint it: put(kind='llm', args={'model_id': '<id>', "
            "'text': '<capability prose>'})",
        )
    pools = [
        p
        for p in _applicable_call_pools(acct, model, list_pools(store))
        if p.limit_reported is not None and p.limit_reported > 0
    ]
    if not pools:
        raise BadInput(
            f"no calls pool with a known limit applies to {account!r}/{model!r}",
            next="record limits on the account: put(kind='llm', "
            "args={'account': {'name': ..., 'limits': [{...}]}})",
        )
    min_gap_s = 0.0
    caps: list[WindowCap] = []
    for pool in pools:
        limit = pool.limit_reported
        if limit is None or limit <= 0:
            continue  # unreachable — filtered above; keeps the type narrow
        rule = None
        if pool.reset_rule:
            try:
                rule = parse_reset_rule(pool.reset_rule)
            except ValueError:
                rule = None  # unreadable window → conservative count check
        if isinstance(rule, RollingReset) and rule.seconds <= _PACING_WINDOW_MAX_S:
            min_gap_s = max(min_gap_s, rule.seconds / limit)
        else:
            caps.append(WindowCap(pool.provider, limit))
    return AccountPin(
        account=acct.name,
        model=model,
        profile_name=acct.profile,
        api_key_ref=profile.api_key_ref,
        min_gap_s=min_gap_s,
        window_caps=tuple(caps),
    )


def check_budget(pin: AccountPin, n_requests: int) -> None:
    """Refuse a run whose request count would overrun a long-window pool.

    A ``rolling:`` window above one hour (or a calendar/expiry rule)
    cannot be paced inside one eval run, so the check is a count:
    ``n_requests`` (gold tasks with a wired scorer) must fit every
    applicable pool's ``limit_reported``.
    """
    for cap in pin.window_caps:
        if n_requests > cap.limit:
            raise BadInput(
                f"eval of {n_requests} tasks would exceed pool "
                f"{cap.pool!r} (limit {cap.limit:g}) — a single run "
                "cannot overrun a provider window",
                next="shrink the gold set or raise the account's recorded limit",
            )


# ── dispatch ──────────────────────────────────────────────────────────


def _error(req: LlmRequest, model: str, msg: str) -> LlmResult:
    return LlmResult(
        text="",
        cost_usd=None,
        turns_used=None,
        model=model,
        tier=req.tier,
        error=msg,
    )


_BEARER = re.compile(r"Bearer\s+\S+")
_AUTHORIZATION = re.compile(r"Authorization\s*[:=]\s*\S+")
_HOSTED_KEY_SHAPE = re.compile(r"\b(?:sk|gsk|or)[-_][A-Za-z0-9_-]{8,}")
#: Provider error text is kept to this length after scrubbing — enough
#: for ``HTTP Error <code>: <message>``, not enough to smuggle a blob.
_MAX_ERROR_CHARS = 300


def _scrub(text: str, key: str) -> str:
    """Strip anything that could carry the credential out of provider text.

    Removes the exact key value, any run of 6+ consecutive characters of
    it (so a partial echo cannot reassemble it), ``Bearer <token>`` and
    ``Authorization: <v>`` header shapes, and the common hosted-provider
    key shapes (``sk-…``, ``gsk_…``, ``or-…``). The caller truncates error
    text to :data:`_MAX_ERROR_CHARS` afterwards; reply text keeps its
    length (scorers need it whole).
    """
    if not text:
        return text
    if key:
        text = text.replace(key, "[key]")
        # Any 6+ run of the key must not survive: erasing every length-6
        # window leaves fragments too short to reassemble.
        for i in range(len(key) - 5):
            text = text.replace(key[i : i + 6], "[key]")
    text = _BEARER.sub("[redacted]", text)
    text = _AUTHORIZATION.sub("[redacted]", text)
    return _HOSTED_KEY_SHAPE.sub("[redacted]", text)


def _scrubbed_result(pin: AccountPin, result: LlmResult) -> LlmResult:
    """Scrub ``error``/``text``/``raw_text`` before the result can reach a
    log, a route-log row (``record_dispatch`` stores the reply), a review
    or a card. The key value is resolved only to be erased — it is never
    logged or stored itself."""
    from precis.secrets import get_secret

    key = get_secret(pin.api_key_ref) or ""
    fields: dict[str, Any] = {}
    if result.error:
        fields["error"] = _scrub(result.error, key)[:_MAX_ERROR_CHARS]
    if result.text:
        fields["text"] = _scrub(result.text, key)
    if result.raw_text:
        fields["raw_text"] = _scrub(result.raw_text, key)
    return replace(result, **fields) if fields else result


def account_pinned_dispatch(
    pin: AccountPin,
    *,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> Callable[[LlmRequest], LlmResult]:
    """A ``dispatch_fn(req) -> LlmResult`` pinned to the account's wire.

    Every request is ``replace``d with the pin's profile + model and run
    through the ``OPENAI_COMPAT`` provider directly — ``router.route``
    is never consulted, so no operator chain can substitute the model.
    ``clock``/``sleep`` are injectable so tests measure the pacing gap
    (``pin.min_gap_s`` between request STARTS) without real waiting.

    A provider 429 (``reason_class == 'rate'`` or ``HTTP Error 429``)
    halts the run: every later call returns a "halted" error without
    sending, so the axis reads void and nothing is recorded. Every
    provider exception becomes an error result — this function never
    raises.
    """
    last_start: float | None = None
    halted = False

    def dispatch(req: LlmRequest) -> LlmResult:
        nonlocal last_start, halted
        if halted:
            return _error(req, pin.model, "halted after provider rate limit")
        call_req = replace(req, profile=pin.profile_name, model=pin.model)
        now = clock()
        if last_start is not None:
            wait = pin.min_gap_s - (now - last_start)
            if wait > 0:
                sleep(wait)
                now = clock()
        last_start = now
        try:
            result = provider_for(Transport.OPENAI_COMPAT).run(
                call_req, model=pin.model
            )
        except Exception as exc:
            result = _error(req, pin.model, f"{type(exc).__name__}: {exc}")
            result = _scrubbed_result(pin, result)
            # The log line gets the SCRUBBED error — never the exception.
            log.warning(
                "llm eval: account %s dispatch raised: %s",
                pin.account,
                result.error,
            )
        else:
            result = _scrubbed_result(pin, result)
        duration_ms = int((clock() - now) * 1000)
        record_dispatch(
            call_req,
            result,
            transport=Transport.OPENAI_COMPAT,
            duration_ms=duration_ms,
            routed="cloud",
        )
        if result.error is not None and (
            result.reason_class == "rate" or "HTTP Error 429" in result.error
        ):
            halted = True
            log.warning(
                "llm eval: account %s hit a provider rate limit — halting "
                "the run (the axis is void; nothing is recorded)",
                pin.account,
            )
        return result

    return dispatch


# ── recording ─────────────────────────────────────────────────────────


def record_account_eval(
    store: Store,
    pin: AccountPin,
    axis: str,
    ordinal: int,
    n: int,
    mean: float,
) -> int:
    """Record a measured-eval axis against the (account, model) pair.

    Card-level ``capability`` is keyed by model alone and cannot say
    WHICH account produced the number, so the score lands in
    ``meta.account_evals[<account>][<axis>]`` (one entry per account;
    later accounts merge beside earlier ones) and the review row carries
    ``provenance='account:<name>'``. ``capability`` is never written
    here. Returns the review number, like :func:`llm_catalog.record_eval`.
    """
    from precis.llm_catalog import CAPABILITY_AXES, LLM_KIND, append_review

    if axis not in CAPABILITY_AXES:
        raise BadInput(
            f"unknown capability axis {axis!r}", options=list(CAPABILITY_AXES)
        )
    if (
        not isinstance(ordinal, int)
        or isinstance(ordinal, bool)
        or not 1 <= ordinal <= 5
    ):
        raise BadInput(f"ordinal must be an int 1..5, got {ordinal!r}")
    ref = store.find_ref_by_meta(kind=LLM_KIND, key="model_id", value=pin.model)
    if ref is None:
        raise BadInput(f"no llm card for model {pin.model!r}")
    meta = ref.meta or {}
    # meta_patch is a shallow merge (meta || patch): copy the whole
    # account_evals map, merge per account, write it back.
    account_evals = dict(meta.get("account_evals") or {})
    per_axis = dict(account_evals.get(pin.account) or {})
    per_axis[axis] = {
        "score": ordinal,
        "model": pin.model,
        "n": n,
        "mean": mean,
        "provenance": "measured-eval",
        "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    account_evals[pin.account] = per_axis
    store.update_ref(ref.id, meta_patch={"account_evals": account_evals})
    return append_review(
        store,
        ref.id,
        text=(
            f"golden eval via account {pin.account}: {axis} = {ordinal} "
            f"(mean {mean:.2f} over {n} tasks)"
        ),
        review_type="measured-eval",
        by="agent",
        provenance=f"account:{pin.account}",
        axis=axis,
        ordinal=ordinal,
    )


__all__ = [
    "AccountPin",
    "WindowCap",
    "account_pinned_dispatch",
    "check_budget",
    "record_account_eval",
    "resolve_account_pin",
]
