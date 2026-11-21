# Changelog

Notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [Unreleased]

### Added
- **Anchors catch a cut tail.** `anchor()` returns the end of the chain as
  `{"entries": n, "hash": ...}`, and `verify(anchor)` raises `TamperError` if entry *n* is
  gone or has a different hash, so the newest rows being cut off the end (README Limit 1),
  even with new rows appended to hide it, is now detectable. The anchor has to be kept
  outside the database, and rows added after it are covered only by the next anchor.
  7 tests; the existing 28 are unchanged.

### Changed
- README badges at the top: live test status for `main` and the license.
- `DESIGN.md` and README Limits now say the row id is not part of the hash, so a row
  renumbered after the triggers are dropped is not caught by `verify()`. The old
  wording ("any edit to a stored row is detectable") overstated it.
- Supported Python is now 3.11 or later, and CI tests 3.11 and 3.15. Python 3.10
  reached end of life in October 2026; CI no longer tests it, so the README no
  longer claims it.

### Fixed
- **`INSERT OR REPLACE` could overwrite a row.** SQLite resolves a REPLACE conflict by
  deleting the old row without firing `DELETE` triggers, so raw SQL could rewrite any
  row (and, with a recomputed hash, the newest row would still pass `verify()`). A new
  `BEFORE INSERT` trigger refuses any colliding insert. Found by an AI review.
- A write blocked at `COMMIT` by an open read on another connection now raises
  `LedgerBusy` with nothing written, like a write blocked at `BEGIN`. It used to
  surface as a raw `sqlite3.OperationalError`, so 0.3.0's `LedgerBusy` covered only the
  `BEGIN` case.
- A blank (whitespace-only) account or idempotency key, and a memo or reversal
  reason that isn't text (for example `None`), are refused with `LedgerError`.
  A `None` memo used to crash inside the hash with a `TypeError`.
- Inputs the ledger can't store safely are refused with `LedgerError` instead of a raw
  error: amounts beyond 10^15 minor units (2^63 used to raise `OverflowError`), text
  with lone surrogates (`UnicodeEncodeError`), and accounts or keys padded with spaces,
  which a new trigger also refuses for raw SQL. `DESIGN.md` now says exactly what raw
  SQL's type affinity lets through.
- A write interrupted mid-transaction (for example by Ctrl-C) now rolls back. It used to
  leave the transaction open, so the next write failed.

## [0.3.0] - 2026-11-20

Fixes found by an AI review.

### Changed
- A reused idempotency key now compares `memo` too. Posting `"order 2"` under the
  key already used for `"order 1"` raises `LedgerError` instead of silently
  returning the `"order 1"` row. An exact retry still returns the first row.
- A writer that times out waiting for another writer's lock raises `LedgerBusy`
  (a `LedgerError`) with nothing written, instead of a raw
  `sqlite3.OperationalError`. `Ledger(path, timeout=...)` sets the wait.

### Added
- Two-connection concurrency tests: concurrent writers produce one unforked chain,
  and a blocked writer gets `LedgerBusy` and succeeds on retry. `DESIGN.md` cites
  them and states what they do not cover.

### Removed
- An unused variable in `verify()`.

## [0.2.0] - 2026-11-19

### Added
- `Ledger.close()` and context-manager support (`with Ledger(path) as ledger:`) so
  the SQLite connection is released deterministically.
- `DESIGN.md` — the three-layer integrity model, the write-path concurrency
  argument, a hash-chain diagram, and an explicit threat model with a
  "what this does NOT defend against" section (tail truncation, full rewrite,
  trusted time, scale, accounting completeness).
- `SECURITY.md` — a private disclosure channel.

### Changed
- CI now runs `python -W error::ResourceWarning`, turning a leaked database
  connection into a build failure; the tests close every connection they open.

## [0.1.0] - 2026-11-15

### Added
- Append-only ledger on SQLite: update/delete-refusing triggers, reversal mirroring,
  unique idempotency keys, a length-prefixed hash chain with `verify()`, and
  `reconcile()` against an external statement. 16 tests, several going around the
  module to prove the rules live in the database.
