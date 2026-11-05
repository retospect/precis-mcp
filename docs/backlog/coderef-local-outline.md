# Local Python outlines and selected source

Add `scripts/coderef outline <file.py> [--symbol Qual]` for local worktree
navigation when the Python kind serves another checkout. Reuse the script's
stdlib AST index and exact-or-unique-suffix symbol lookup; never import or
execute the target. Preserve the existing retrieval verbs and dependencies.

File mode shows module-scope import source and a source-ordered tree of
class/function signatures, line spans and durable anchors, including nested
definitions and async methods. Symbol mode prints only the selected class or
function's original source, starting at its first decorator and retaining local
imports. Missing files, invalid syntax and ambiguous or absent symbols fail
with a next step. Focused fixtures cover scope, decorated nested source,
suffix resolution and error handling; document the reusable fallback commands.
