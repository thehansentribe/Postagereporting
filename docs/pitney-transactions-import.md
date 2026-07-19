# Pitney Detail Transactions import & reconciliation

The Pitney Detail Transactions export is the **actual billing ledger** from Pitney
Bowes: one row per transaction (postage print, refund, adjustment, funds deposit)
with the parcel tracking number. Importing it lets the system verify that what we
paid Pitney matches what the billing records claimed, and true up supplier profit.

## File

- Name pattern: anything containing `pitney` and `transaction`, `.xlsx`
  (e.g. `05.31.26_Pitney Detail Transactions.xlsx`).
- Import paths: drop in `input/` (watcher), System page upload panel, or
  `POST /api/import/pitney-transactions`.
- Header row is located by name (`transactionType`, `amount`, `transactionId`,
  `transactionDateTime`, …); column order does not matter.

## Ledger semantics (important)

- A **`transactionId` is shared** between a Postage Print and any later refund or
  adjustment for the same piece.
- A **refund is re-listed at each lifecycle stage** — `REQUESTED` (possibly more
  than once), then `ACCEPTED` or `DENIED`. Only **ACCEPTED** refunds return money.
- Rows are therefore deduplicated by `(transactionId, transactionType,
  transactionDateTime)`; re-importing a file (or overlapping monthly exports)
  inserts nothing twice.
- `Postage Fund` rows are deposits into the postage account. They are stored for
  audit but excluded from all cost math.

## Matching to parcels

`parcelTrackingNumber` (leading backtick stripped) is matched against
`billing_records.impb_normalized` — the IMpb barcode with its routing-zip
`<FNC1>` prefix removed. A matched print's `amount` should equal the piece's
`final_postage` exactly (May 2026: 7,679 of 7,712 prints matched, all exact).

## Reconciliation & true-up

- `GET /api/pitney/reconciliation?start_date=&end_date=` and the **Pitney
  reconciliation** section on the Profit tab show: prints matched/unmatched,
  amount mismatches, parcels missing prints, refunds by lifecycle status,
  under/overpaid adjustments, and funds.
- The Parcel Profit block gains a true-up when Pitney data covers the range:

      actual USPS cost = final postage + underpaid − overpaid − accepted refunds
      Supplier (Lineage) Profit — trued-up = billing + parcel fee − actual USPS cost

- Scoped profit requests attribute refunds/adjustments through in-scope tracking
  numbers and report the unattributed remainder.
