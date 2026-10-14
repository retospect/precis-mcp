---
status: draft
title: "pcb: order a board through the JLCPCB Open API (upload → quote → create)"
pillar: 3d-design
prio: normal
---

# pcb: order through the JLCPCB Open API

Research for the P3 task ("order thru the api — at least place in shopping
cart", Reto 2026-10-14) — what the official API offers, what our key can
reach, and the shape that lands in `precis.pcb.jlc_order` +
`PcbHandler._op_order`.

## Endpoint inventory (verified against two client implementations)

Host `https://open.jlcpcb.com` — the same host and the same per-request
`JOP` HMAC-SHA256 signing `src/precis/pcb/jlc_api.py` already implements
(`api.jlcpcb.com` is the docs SPA, it 404s API paths). The docs site is
JS-rendered and the field docs sit behind the application login, so the
wire shapes below were cross-checked against `i2cjak/jlcpcb_api`
(`src/jlcpcb_api/models.py` — reversed from JLC's official Java SDK JARs)
and `Eyalm321/jlcpcb-mcp` (`src/tools/pcb.ts`).

| path | does |
|---|---|
| `POST /overseas/openapi/pcb/uploadGerber` | multipart upload; the `meta` form field (a JSON string) is what gets signed, `file` carries the zip → `fileKey` |
| `POST /overseas/openapi/pcb/calculate` | online quote, no order created: `{orderType, pcbParam{layer,width,length,qty,thickness,pcbColor,surfaceFinish,copperWeight,panelFlag,...}, smtStencilParam?, fileKey?, batchNum?, country?, postCode?, city?, shippingMethod?, achieveDate?}` |
| `POST /overseas/openapi/pcb/create` | creates the order: `{fileKey, batchNum?, orderType, pcbParam, smtStencilParam?, shippingAddress{firstName,lastName,companyName?,streetAddress,addressLine2?,city,country,province?,postalCode,cellOrMobileNumber}, billingAddress?, TaxOrVATNumber?, billingAddressFlag?, achieveDate?, shippingMethod?}` → order id + `batchNum` |
| `POST /overseas/openapi/pcb/order/detail` | `{batchNum}` → order state, cost, address |
| `POST /overseas/openapi/pcb/audit/get` | `{key, language?}` → engineering/DFM audit for an uploaded file (`key` = the fileKey) |
| `POST /overseas/openapi/pcb/wip/get` | `{orderUUID}` → production progress |
| `GET /overseas/openapi/pcb/getSteelPriceConfig` | stencil price config (pure read) |
| `POST /overseas/openapi/pcb/getImpedanceTemplateSettingList` | impedance template options for a stackup |
| `POST /overseas/openapi/pcb/uploadBlindViaHoleImg` | stackup image for blind/buried-via boards |

Sources: `https://api.jlcpcb.com/docs/api-list`,
`https://api.jlcpcb.com/docs/api-request-signature`,
`https://jlcpcb.com/help/article/jlcpcb-online-api-available-now`,
`github.com/i2cjak/jlcpcb_api`, `github.com/Eyalm321/jlcpcb-mcp`.

## "Shopping cart" maps to `pcb/create`

The documented flow is upload → quote → **create**; there is no cart
endpoint. The API exposes no payment call at all — payment exists only on
jlcpcb.com (the 3D-printing order list even carries a `waitPayOrSupplement`
filter) — so a created order lands on the account **unpaid, awaiting
payment**, i.e. the cart/checkout step. Our `op='order'` treats
`pcb/create` as the terminal "add-to-cart" action: guarded by
`confirm='add-to-cart'`, it is still the first live call Reto makes —
whether `create` could ever debit a prepaid balance is undocumented, so
the first created order must be eyeballed as "awaiting payment" before the
path is trusted again.

## What our key can reach — undetermined while upstream 500s

- `JLCPCB_APP_ID` / `JLCPCB_ACCESS_KEY` / `JLCPCB_SECRET_KEY` resolve from
  the PROD vault (present since 2026-08-28; `vault.list()` confirms the
  names, never the values). Not env vars, not the dev vault — a local
  `credentials_available()` reads `False` unless the prod store is bound.
- Live probe 2026-10-14 (read-only, via `scripts/_prod-run`):
  `GET /pcb/getSteelPriceConfig` and `POST /component/getComponentInfos`
  both answer `HTTP 500 {"code":500,"message":"An unknown error has
  occurred in the service"}` — the same upstream outage as gr481902, and
  it covers the PCB-scope endpoint too, so the outage is wider than
  `getComponentInfos`. The signature is accepted (a bad one answers 401
  `signature verify failed`; a missing scope answers 403
  `API insufficient permissions`), but **PCB-scope grant status is
  unverifiable until JLC recovers**. If `pcb/calculate` answers 403 when
  the outage clears: grant the PCB/order scope to the app in the Open API
  console (`api.jlcpcb.com`) — a human console action, same class as the
  Components-scope grant in `pcb-guided-place-route.md` slice 9.
- Unit discrepancy to watch: docs and both reference clients send
  `timestamp` in epoch **seconds**; `jlc_api` sends **milliseconds**
  (spike-verified 2026-08-27 — the signature over the literal string is
  accepted either way). If calls start 401ing for no other reason, suspect
  a newly-enforced freshness window first.
- No PCBA/assembly endpoints exist in the documented surface — no BOM/CPL
  upload, no `smtParam` on the PCB order (only `smtStencilParam`, a steel
  stencil order). Assembly attaches at jlcpcb.com checkout from the
  BOM/CPL `view='fab'` writes; if a partner PCBA surface exists it is not
  in the public docs.

## Shipped shape (this work)

- `src/precis/pcb/jlc_order.py` — `JlcOrderClient` over the same
  creds/sign/backoff seam (`jlc_api._authorization_header`,
  `_http.with_backoff`), plus pure request builders: `pcb_param(...)`
  (layers from the stackup, width/length from the outline bbox,
  qty/thickness from args) and `build_order_request(...)`. `pcbParam`'s
  numeric craft codes (`pcbColor`, `surfaceFinish`, `plateType`, …) are
  ints whose enum table lives behind the console docs — the builder
  carries the structural facts and merges `args['pcb_param']` verbatim;
  verify the codes before the first real order.
- `view='fab'` — one-shot fab package: `{slug}-fab.zip` (the existing
  `gerber.export_fab` bundle) + `{slug}-bom.csv` + `{slug}-cpl.csv` in JLC
  column format into the export dir.
- `put(args={'op':'order'})` — `dry_run=True` default builds + prints the
  exact `pcb/create` payload and attempts the read-only `pcb/calculate`
  quote when creds resolve (failures report inline). `dry_run=False`
  requires `confirm='add-to-cart'` verbatim, then upload → quote →
  `pcb/create`, and records `{batchNum, orderId}` in
  `pcb_boards.meta['jlc_order']` (`store.pcb_merge_board_meta`). The op
  never exposes a pay/submit step — the API has none.
