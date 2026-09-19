# design chat: keep every turn in the transcript + one bounded repair round

From the first prod use of the design chat (2026-09-19, `se:unicycle-mk2`,
four turns, model answered every time at `Tier.BIG`; zero applied).

1. **Rejected and no-op turns are lost.** `precis_web/design_turn.py::run_turn`
   writes the `conv` transcript block only on an applied revision or a
   proposal; a "cannot be expressed as ops" reply or a validator rejection
   only reaches the operator as the `?chat_note=`/`?chat_error=` flash and
   is gone on the next navigation. Two of the four turns were useful
   informational answers (which load is already declared; why a "radial
   ±10 Nm" spec doesn't map onto mixed-axis connects) — keep every turn in
   `design-chat-<slug>` with its outcome tag (applied / proposal /
   rejected / no-op).
2. **One bounded repair round.** Both rejected turns were op-shape errors
   the validator named exactly (`set_load` targeted neither a block nor a
   connect; block `saddle+seatpost` doesn't exist — the model fused two
   names). Feed the dry-run error back to the model once, same tool-less
   contract, then give up. Expect most first-shot rejections to convert.

Owner anchor: `precis_web/design_turn.py` (`run_turn`, `_write_transcript`).
test: a stub that first returns an unknown block then a valid op → one
revision, transcript shows both attempts; a "cannot be expressed" reply →
transcript block with outcome no-op, no revision.
