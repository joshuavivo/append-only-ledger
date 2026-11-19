# Append-only payments ledger

A small ledger where money records are never edited and never deleted. A mistake is fixed by
adding a reversal row that points at the original, so the history always shows what happened
and what was done about it.

I built this to show how I think money records should be kept, in about 160 lines of Python and SQLite.

## What it guarantees

| Rule | Where it is enforced |
|---|---|
| Rows cannot be updated or deleted | Database triggers, so it holds for any code that touches the table |
| A reversal must mirror its original: same account, opposite amount | Database trigger |
| An entry can be reversed once, and a reversal cannot be reversed | Unique constraint and trigger |
| A retried request writes once | Unique idempotency key; the retry gets the first result back |
| A key reused for a different request is refused | Checked inside the write transaction |
| Amounts are whole minor units | Floats, booleans, strings and zero are refused |
| Differences from an external statement are listed, never auto-fixed | `reconcile()` reports missing rows, amount mismatches and duplicates, and writes nothing |
| Edits made after removing the triggers are detected | Each row stores a hash of itself and the previous row; `verify()` recomputes the chain |

## Use

```python
from ledger import Ledger

ledger = Ledger("payments.db")
charge = ledger.post("acct_1", 5000, idempotency_key="charge-1", memo="order 1")
ledger.reverse(charge["id"], idempotency_key="reverse-1", reason="charged twice")
ledger.balance("acct_1")   # 0, with both rows still on record
ledger.verify()            # 2 rows checked
ledger.reconcile([("charge-1", 5000)])   # lists reverse-1 as missing from the statement
ledger.close()             # or use `with Ledger("payments.db") as ledger: ...`
```

How the three enforcement layers fit together, the write-path concurrency argument,
and — importantly — the threat model with what this does **not** defend against
(tail truncation, a full rewrite, trusted time) are in [`DESIGN.md`](DESIGN.md).

## Tests

```bash
python3 -W error::ResourceWarning -m unittest discover -s tests
```

Sixteen tests, and that's the command CI runs: `-W error::ResourceWarning` makes a leaked
database connection fail the build. Several tests go around the module and write to the
database directly, to prove the rules live in the database and not only in the Python.

## Limits

- The hash chain detects an edited row or a row removed from the middle. It does not detect
  the newest rows being cut off the end. Catching that needs the latest hash stored somewhere
  the database owner cannot write, such as a separate log.
- Someone with full write access to the file can rebuild the whole chain. The chain is tamper
  evidence against partial edits, not a substitute for access control and backups.
- One currency per ledger. No double-entry accounts; this is a single-sided record of entries.
- SQLite, one writer at a time. The pattern carries to Postgres with the same triggers and
  constraints.

Standard library only, Python 3.10 or later.

## Credits

Architected and directed by [Joshua Vivo](https://github.com/joshuavivo) ([Shadow Vivo LLC](https://shadowvivo.com)), built with AI assistance (Claude). The design and the judgment are mine.
