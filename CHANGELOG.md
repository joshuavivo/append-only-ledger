# Changelog

Notable changes. Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.2.0]

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

## [0.1.0]

### Added
- Append-only ledger on SQLite: update/delete-refusing triggers, reversal mirroring,
  unique idempotency keys, a length-prefixed hash chain with `verify()`, and
  `reconcile()` against an external statement. 16 tests, several going around the
  module to prove the rules live in the database.
