# Split oversized store/web modules

`src/precis/store/_blocks_ops.py` + `_draft_ops.py` (72 functions) by concern
(SQL builders / rankers / card writers); `src/precis_web/routes/drafts.py`
(~3,000 lines) into per-concern modules. Mechanical refactors, each its own
pass.
