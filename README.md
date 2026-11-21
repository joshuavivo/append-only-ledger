# Append-only payments ledger

[![tests](https://github.com/joshuavivo/append-only-ledger/actions/workflows/tests.yml/badge.svg?branch=main)](https://github.com/joshuavivo/append-only-ledger/actions/workflows/tests.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-informational)](LICENSE)

**When a customer disputes a charge, you have to show what happened.** An edited payment row
can't: the history now says it was never wrong, the dispute gets argued from memory, and the
accountant can't tell a bug from a correction.

This is a small ledger where money records are never edited and never deleted. A mistake is fixed
by adding a reversal row that points at the original, so the history always shows what happened
and what was done about it.

I built this to show how I think money records should be kept, in one small file of Python and SQLite.

## Why payment records specifically

Money records get read later by people who weren't there: a customer disputing a
charge, a card network asking for evidence, an accountant closing the month, an
auditor. Each of them asks the same question, "what happened, and when", and an
edited row can't answer it. If a balance was wrong and someone fixed it in place,
the history now says it was never wrong, and the fix is invisible. With reversals,
the original charge, the mistake, the correction and the reason all stay on
record, so a dispute is answered from the ledger instead of from someone's memory.
It also makes a bug in the code that writes money show up as rows you can count,
not as a balance that quietly drifted.

For the same rule inside a webhook handler (signature check, exactly-once, a ledger
row in the same Postgres transaction, dead letter), see the
[payment-path example](https://github.com/joshuavivo/exactly-once-webhooks/tree/main/examples/payment-path)
in exactly-once-webhooks.

## What it guarantees

| Rule | Where it is enforced |
|---|---|
| Rows cannot be updated, deleted or overwritten with `INSERT OR REPLACE` | Database triggers, so it holds for any code that touches the table |
| A reversal must mirror its original: same account, opposite amount | Database trigger |
| An entry can be reversed once, and a reversal cannot be reversed | Unique constraint and trigger |
| A retried request writes once | Unique idempotency key; the retry gets the first result back |
| A key reused for a different request is refused | Checked inside the write transaction |
| Amounts are whole minor units | Floats, booleans, strings, zero and amounts beyond 10^15 are refused |
| Differences from an external statement are listed, never auto-fixed | `reconcile()` reports missing rows, amount mismatches and duplicates, and writes nothing |
| Edits made after removing the triggers are detected | Each row stores a hash of itself and the previous row; `verify()` recomputes the chain. Rows cut off the end are caught only with an anchor kept outside the database (see Limits) |

## Use

```python
from ledger import Ledger

ledger = Ledger("payments.db")
charge = ledger.post("acct_1", 5000, idempotency_key="charge-1", memo="order 1")
ledger.reverse(charge["id"], idempotency_key="reverse-1", reason="charged twice")
ledger.balance("acct_1")   # 0, with both rows still on record
ledger.verify()            # 2 rows checked
anchor = ledger.anchor()   # {"entries": 2, "hash": ...}: store it outside this database
ledger.verify(anchor)      # also fails if entries up to the anchor were cut off the end
ledger.reconcile([("charge-1", 5000)])   # lists reverse-1 as missing from the statement
ledger.close()             # or use `with Ledger("payments.db") as ledger: ...`
```

How the three enforcement layers fit together, the write-path concurrency argument,
and — importantly — the threat model with what this does **not** defend against
(tail truncation without an anchor, renumbering, a full rewrite, trusted time) are in
[`DESIGN.md`](DESIGN.md).

## Tests

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests
```

Thirty-five tests, and that's the command CI runs: `-W error::ResourceWarning` makes a leaked
database connection fail the build. Several tests go around the module and write to the
database directly, to prove the rules live in the database and not only in the Python.

## Limits

- The hash chain detects an edited row or a row removed from the middle. On its own it does
  not detect the newest rows being cut off the end. `anchor()` exports the end of the chain
  (`{"entries": n, "hash": ...}`); kept somewhere the database owner cannot write (a separate
  append-only log, write-once storage, another system), `verify(anchor)` catches a cut tail,
  including one hidden by new rows appended in its place. Rows added after the last anchor are
  not covered until the next one, so anchor often. This repo doesn't store anchors for you.
- The hash covers each row's contents and the previous row's hash, not its row number. With
  the triggers dropped, renumbering a row (for example detaching a reversal from the entry it
  reverses) is not caught by `verify()`.
- Someone with full write access to the file can rebuild the whole chain. The chain is tamper
  evidence against partial edits, not a substitute for access control and backups.
- One currency per ledger. No double-entry accounts; this is a single-sided record of entries.
- SQLite, one writer at a time. The pattern carries to Postgres with the same triggers and
  constraints.

Standard library only, Python 3.11 or later.

## Credits

Architected and directed by [Joshua Vivo](https://github.com/joshuavivo) ([Shadow Vivo LLC](https://shadowvivo.com)), built with AI assistance (Claude). The design and the judgment are mine.
