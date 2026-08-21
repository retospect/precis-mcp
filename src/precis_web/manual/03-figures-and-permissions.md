# Figures and permissions

Two ways a figure gets into a draft: you upload an image, or you draw
one on a canvas. Both attach to a block in the document and travel with
it into every export.

## Uploading an image

On any block in the reader, add a figure after it: caption, **origin**,
and the file. The caption is not optional — a figure without one is not
a figure, it's decoration.

**Origin** is the clearance question, and it has three answers:

| Origin | Means |
|---|---|
| `original` | we made it |
| `own_graph` | we plotted it from our own data |
| `third_party` | someone else's, reused under permission |

The first two are clear on sight. **`third_party` will not save without
a permission record** — the same rule applies whether the figure arrives
through the web form or the tool surface, because it is enforced once,
underneath both.

## The permission record

A third-party figure carries its paper trail as fields on the figure
itself:

- **publisher** — who granted (or will grant) it
- **permission_id** — the licence or RightsLink order number
- **status** — where the request stands
- **requested_at / granted_at / expires_at** — the dates
- **scope** — what you were actually granted (print? online? one
  edition? derivative works?)
- **required_credit** — the exact wording the publisher demands
- **source_paper** — where it came from

Click the clearance badge under any figure to edit these. Editing
touches only the permission record; the caption and the image bytes are
never disturbed.

## What precis does not do

**It does not ask the publisher for you.** There is no request template,
no outbound email, no reminder when `expires_at` comes around, and
`required_credit` is stored but not automatically inserted into your
caption at export.

So the real workflow is:

1. Ask the publisher yourself — their permissions form, RightsLink, or
   an email to the rights desk.
2. Record the outcome here as soon as you have it, including the exact
   required credit line.
3. **Paste the credit into the caption yourself.** Nothing will do it
   for you and nothing will warn you if you forget.

Treat the permission record as a ledger you keep, not a process that
runs. It exists so that six months later, when a journal's production
desk asks who cleared figure 4, the answer is one click away instead of
lost in your sent mail.

## Drawing instead

A figure block with no image attached can be turned into an editable
**canvas** — an SVG you build with the model rather than upload. It
becomes a first-class object in its own right, parented on the draft's
project and linked back to the block, and opens in the figure editor.

This is the honest answer to most permission problems: a figure you draw
from the underlying data is `own_graph`, needs no clearance, no expiry,
and no credit line, and usually says what you actually mean better than
the one you were going to borrow.

Turning an already-drawn figure into a canvas again just reopens the
existing one — you cannot accidentally fork it.
