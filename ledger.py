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

-- Accounts and keys are compared as exact text, so "acct_1" and "acct_1 " would be two
-- accounts. Refuse blank values, and values padded with spaces, tabs, line breaks or a
-- non-breaking space, here too, not only in Python.
CREATE TRIGGER IF NOT EXISTS entries_clean_text BEFORE INSERT ON entries
WHEN trim(NEW.account, ' ' || char(9, 10, 11, 12, 13, 160)) = ''
  OR NEW.account != trim(NEW.account, ' ' || char(9, 10, 11, 12, 13, 160))
  OR trim(NEW.idempotency_key, ' ' || char(9, 10, 11, 12, 13, 160)) = ''
  OR NEW.idempotency_key != trim(NEW.idempotency_key, ' ' || char(9, 10, 11, 12, 13, 160))
BEGIN SELECT RAISE(ABORT, 'account and idempotency_key cannot be blank or padded with spaces'); END;

-- INSERT OR REPLACE resolves a conflict by deleting the old row, and SQLite skips DELETE
-- triggers for that unless recursive_triggers is on. Refuse any insert that would collide,
-- before conflict resolution runs, so the guarantee holds on every connection.
CREATE TRIGGER IF NOT EXISTS entries_no_replace BEFORE INSERT ON entries
WHEN EXISTS (SELECT 1 FROM entries WHERE id = NEW.id OR idempotency_key = NEW.idempotency_key)
BEGIN SELECT RAISE(ABORT, 'ledger is append-only: rows cannot be replaced'); END;

CREATE TRIGGER IF NOT EXISTS entries_reversed_once BEFORE INSERT ON entries
WHEN NEW.reverses IS NOT NULL AND EXISTS (SELECT 1 FROM entries WHERE reverses = NEW.reverses)
BEGIN SELECT RAISE(ABORT, 'that entry has already been reversed'); END;

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


class LedgerBusy(LedgerError):
    """Another connection held the database lock (a writer, or a reader at COMMIT) for longer than the ledger's timeout. Nothing was written; retry."""


class TamperError(Exception):
    """The stored rows no longer match their hash chain."""


# Bound on one entry: 10^15 minor units ($10 trillion). It keeps a single amount, and the
# sum of thousands of them, inside SQLite's signed 64-bit integers.
MAX_MINOR_UNITS = 10**15


def _check_anchor(anchor):
    if (
        not isinstance(anchor, dict)
        or set(anchor) != {"entries", "hash"}
        or isinstance(anchor["entries"], bool)
        or not isinstance(anchor["entries"], int)
        or anchor["entries"] < 0
        or not isinstance(anchor["hash"], str)
        or len(anchor["hash"]) != 64
        or any(c not in "0123456789abcdef" for c in anchor["hash"])
        or (anchor["entries"] == 0) != (anchor["hash"] == GENESIS)
    ):
        raise LedgerError('anchor must be {"entries": n, "hash": "<64 hex>"} as returned by anchor()')


def _hash(prev_hash, account, amount_cents, reverses, key, memo, created_at):
    parts = [prev_hash, account, str(amount_cents), "" if reverses is None else str(reverses), key, memo, created_at]
    # Length-prefix each field so two different rows can never join to the same string.
    data = "".join(f"{len(p)}:{p}" for p in parts)
    return hashlib.sha256(data.encode()).hexdigest()


class Ledger:
    def __init__(self, path=":memory:", timeout=5.0):
        """timeout is how many seconds a write waits on another connection's lock before LedgerBusy."""
        self.db = sqlite3.connect(path, isolation_level=None, timeout=timeout)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self):
        """Close the underlying connection. Also usable as a context manager:
        `with Ledger(path) as ledger: ...` closes it on exit."""
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
        return False

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

    def verify(self, anchor=None):
        """Recompute the hash chain. Returns the number of rows checked, or raises TamperError.

        The chain alone can't tell that the newest rows were cut off the end. Pass an anchor
        (from anchor(), kept somewhere the database owner can't write) to catch that: the nth
        entry (by position, not id) must still be there with the same hash. Rows added after it
        are fine.
        """
        if anchor is not None:
            _check_anchor(anchor)
        prev, count = GENESIS, 0
        anchored = anchor is not None and anchor["entries"] == 0
        for r in self.db.execute("SELECT * FROM entries ORDER BY id"):
            expected = _hash(prev, r["account"], r["amount_cents"], r["reverses"], r["idempotency_key"], r["memo"], r["created_at"])
            if r["prev_hash"] != prev or r["hash"] != expected:
                raise TamperError(f"entry {r['id']} does not match its hash")
            prev, count = r["hash"], count + 1
            if anchor is not None and count == anchor["entries"]:
                if r["hash"] != anchor["hash"]:
                    raise TamperError(
                        f"entry {count} of the chain (by position, not id) no longer matches the anchor: entries "
                        "up to it were changed or replaced, or the anchor is from another ledger"
                    )
                anchored = True
        if anchor is not None and not anchored:
            raise TamperError(
                f"the ledger has {count} entries, but the anchor was taken at {anchor['entries']} "
                "(counted by position, not id): the newest entries were removed, or the anchor is from "
                "another ledger"
            )
        return count

    def anchor(self):
        """The current end of the chain: {"entries": n, "hash": hash of the nth entry}.

        Store it outside this database, somewhere its owner can't rewrite (an append-only log,
        write-once object storage, a message to someone else), then pass it to verify(). It is
        plain JSON-safe data. Entries added after it are not covered until the next anchor.
        It counts the rows: an O(n) read, though much cheaper than verify(), which hashes each row.
        """
        tip = self.db.execute("SELECT COUNT(*) AS n, (SELECT hash FROM entries ORDER BY id DESC LIMIT 1) AS h FROM entries").fetchone()
        return {"entries": tip["n"], "hash": tip["h"] or GENESIS}

    def reconcile(self, statement, account=None):
        """Compare the ledger with an external statement (a bank or processor export).

        statement is a list of (reference, amount_cents) pairs, where reference is the
        idempotency key the entry was posted with. Nothing is written: differences are
        reported for a person to resolve with new entries. Returns a dict of four lists
        and the difference in totals (ledger minus statement).
        """
        ours = {e["idempotency_key"]: e["amount_cents"] for e in self.entries(account)}
        theirs, duplicates = {}, []
        for reference, amount in statement:
            if reference in theirs:
                duplicates.append(reference)
            else:
                theirs[reference] = amount
        return {
            "missing_in_ledger": sorted(k for k in theirs if k not in ours),
            "missing_in_statement": sorted(k for k in ours if k not in theirs),
            "amount_mismatch": sorted((k, ours[k], theirs[k]) for k in ours if k in theirs and ours[k] != theirs[k]),
            "duplicate_in_statement": sorted(set(duplicates)),
            "difference_cents": sum(ours.values()) - sum(amount for _, amount in statement),
        }

    def _append(self, account, amount_cents, reverses, key, memo):
        if isinstance(amount_cents, bool) or not isinstance(amount_cents, int):
            raise LedgerError("amount_cents must be an integer number of minor units, never a float")
        if amount_cents == 0:
            raise LedgerError("amount_cents cannot be zero")
        if abs(amount_cents) > MAX_MINOR_UNITS:
            raise LedgerError(f"amount_cents is out of range (at most {MAX_MINOR_UNITS:,} minor units)")
        for name, value in (("account", account), ("idempotency_key", key)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise LedgerError(f"{name} is required and cannot be blank or padded with spaces")
        if not isinstance(memo, str):
            raise LedgerError("memo (or a reversal's reason) must be text")
        for value in (account, key, memo):
            try:
                value.encode("utf-8")
            except UnicodeEncodeError:
                raise LedgerError("text must be valid Unicode (no lone surrogates)") from None
        # IMMEDIATE takes the write lock before reading the chain tip, so two writers cannot chain off the same row.
        try:
            self.db.execute("BEGIN IMMEDIATE")
        except sqlite3.OperationalError as e:
            if "locked" not in str(e):
                raise
            raise LedgerBusy("another writer is holding the ledger; nothing was written, retry the request") from e
        try:
            seen = self.db.execute("SELECT * FROM entries WHERE idempotency_key = ?", (key,)).fetchone()
            if seen is not None:
                # Every field the caller supplies is part of "the same request", memo included:
                # a key reused for a different order must be refused, not answered with the first row.
                same = (seen["account"], seen["amount_cents"], seen["reverses"], seen["memo"]) == (account, amount_cents, reverses, memo)
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
        except sqlite3.OperationalError as e:
            # COMMIT can also wait on a lock: an open read on another connection blocks it even
            # though BEGIN IMMEDIATE succeeded. Same outcome as a busy BEGIN: roll back, nothing written.
            self.db.execute("ROLLBACK")
            if "locked" not in str(e):
                raise
            raise LedgerBusy("another connection is holding the ledger; nothing was written, retry the request") from e
        except BaseException:  # KeyboardInterrupt included: never leave the transaction open
            self.db.execute("ROLLBACK")
            raise
