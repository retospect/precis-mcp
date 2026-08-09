# Store mixin collision guard

`store/store.py::Store` composes 22 mixins on convention alone — add a
unit test walking `__mro__` asserting no method is defined by more than
one mixin (cheap; catches silent shadowing as the count grows).
Mechanical. Test: the new test fails when a duplicate method is
introduced.
