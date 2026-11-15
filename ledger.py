"""An append-only payments ledger on SQLite.

Rows are never edited or deleted. A mistake is corrected by a reversal row that
points at the original. The database enforces this with triggers, so the rule
holds even for code that bypasses this module. Every row also carries a hash of
itself and the row before it, so edits made by someone who removed the triggers
are still detected by verify().

Amounts are integers in minor units (cents). One currency per ledger.
"""
import hashlib
import sqlite3
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    account         TEXT    NOT NULL CHECK (length(account) > 0),
    amount_cents    INTEGER NOT NULL CHECK (typeof(amount_cents) = 'integer' AND amount_cents != 0),
    reverses        INTEGER UNIQUE REFERENCES entries(id),
    idempotency_key TEXT    NOT NULL UNIQUE,
    memo            TEXT    NOT NULL DEFAULT '',
    created_at      TEXT    NOT NULL,
    prev_hash       TEXT    NOT NULL,
    hash            TEXT    NOT NULL
);

CREATE TRIGGER IF NOT EXISTS entries_no_update BEFORE UPDATE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: rows cannot be updated'); END;

CREATE TRIGGER IF NOT EXISTS entries_no_delete BEFORE DELETE ON entries
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: rows cannot be deleted'); END;

CREATE TRIGGER IF NOT EXISTS reversal_must_mirror BEFORE INSERT ON entries
WHEN NEW.reverses IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'reversal must target an existing, non-reversal entry and mirror its account and amount')
    WHERE NOT EXISTS (
        SELECT 1 FROM entries o
        WHERE o.id = NEW.reverses
          AND o.reverses IS NULL
          AND o.account = NEW.account
          AND o.amount_cents = -NEW.amount_cents
    );
END;
"""

GENESIS = "0" * 64


class LedgerError(Exception):
    """A request the ledger refuses."""


class TamperError(Exception):
    """The stored rows no longer match their hash chain."""


def _hash(prev_hash, account, amount_cents, reverses, key, memo, created_at):
    parts = [prev_hash, account, str(amount_cents), "" if reverses is None else str(reverses), key, memo, created_at]
    # Length-prefix each field so two different rows can never join to the same string.
    data = "".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(data.encode()).hexdigest()


class Ledger:
    def __init__(self, path=":memory:"):
        self.db = sqlite3.connect(path, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def post(self, account, amount_cents, idempotency_key, memo=""):
        """Record a new entry. Positive credits the account, negative debits it."""
        return self._append(account, amount_cents, None, idempotency_key, memo)

    def reverse(self, entry_id, idempotency_key, reason=""):
        """Cancel an entry by appending its mirror image. The original stays as written."""
        original = self.db.execute("SELECT * FROM entries WHERE id = ?", (entry_id,)).fetchone()
        if original is None:
            raise LedgerError(f"entry {entry_id} does not exist")
        return self._append(original["account"], -original["amount_cents"], entry_id, idempotency_key, reason)

    def balance(self, account):
        row = self.db.execute("SELECT COALESCE(SUM(amount_cents), 0) FROM entries WHERE account = ?", (account,)).fetchone()
        return row[0]

    def entries(self, account=None):
        if account is None:
            return [dict(r) for r in self.db.execute("SELECT * FROM entries ORDER BY id")]
        return [dict(r) for r in self.db.execute("SELECT * FROM entries WHERE account = ? ORDER BY id", (account,))]

    def verify(self):
        """Recompute the hash chain. Returns the number of rows checked, or raises TamperError."""
        prev, count, last_id = GENESIS, 0, 0
        for r in self.db.execute("SELECT * FROM entries ORDER BY id"):
            expected = _hash(prev, r["account"], r["amount_cents"], r["reverses"], r["idempotency_key"], r["memo"], r["created_at"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                raise TamperError(f"entry {r['id']} does not match its hash")
            prev, count, last_id = r["hash"], count + 1, r["id"]
        return count

    def _append(self, account, amount_cents, reverses, key, memo):
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise LedgerError("amount_cents must be an integer number of minor units, never a float")
        if amount_cents == 0:
            raise LedgerError("amount_cents cannot be zero")
        if not account or not key:
            raise LedgerError("account and idempotency_key are required")
        # IMMEDIATE takes the write lock before reading the chain tip, so two writers cannot chain off the same row.
        self.db.execute("BEGIN IMMEDIATE")
        try:
            seen = self.db.execute("SELECT * FROM entries WHERE idempotency_key = ?", (key,)).fetchone()
            if seen is not None:
                same = (seen["account"], seen["amount_cents"], seen["reverses"]) == (account, amount_cents, reverses)
                self.db.execute("ROLLBACK")
                if same:
                    return dict(seen)  # a retry of the same request: return the first result, write nothing
                raise LedgerError(f"idempotency key {key!r} was already used for a different request")
            tip = self.db.execute("SELECT hash FROM entries ORDER BY id DESC LIMIT 1").fetchone()
            prev = tip["hash"] if tip else GENESIS
            now = datetime.now(timezone.utc).isoformat()
            h = _hash(prev, account, amount_cents, reverses, key, memo, now)
            cur = self.db.execute(
                "INSERT INTO entries (account, amount_cents, reverses, idempotency_key, memo, created_at, prev_hash, hash)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (account, amount_cents, reverses, key, memo, now, prev, h),
            )
            row = self.db.execute("SELECT * FROM entries WHERE id = ?", (cur.lastrowid,)).fetchone()
            self.db.execute("COMMIT")
            return dict(row)
        except sqlite3.IntegrityError as e:
            self.db.execute("ROLLBACK")
            raise LedgerError(str(e)) from None
        except LedgerError:
            raise
        except Exception:
            self.db.execute("ROLLBACK")
            raise
