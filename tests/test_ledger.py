import sqlite3
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from ledger import Ledger, LedgerError, TamperError


class AppendOnly(unittest.TestCase):
    def setUp(self):
        self.l = Ledger()
        self.e = self.l.post("acct_1", 5000, "charge-1", "order 1")

    def test_update_is_refused_by_the_database(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.l.db.execute("UPDATE entries SET amount_cents = 1 WHERE id = ?", (self.e["id"],))
        self.assertEqual(self.l.balance("acct_1"), 5000)

    def test_delete_is_refused_by_the_database(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.l.db.execute("DELETE FROM entries")
        self.assertEqual(len(self.l.entries()), 1)


class Reversals(unittest.TestCase):
    def setUp(self):
        self.l = Ledger()
        self.e = self.l.post("acct_1", 5000, "charge-1")

    def test_reversal_zeroes_the_balance_and_keeps_both_rows(self):
        r = self.l.reverse(self.e["id"], "reverse-1", "charged twice")
        self.assertEqual(self.l.balance("acct_1"), 0)
        self.assertEqual(r["reverses"], self.e["id"])
        self.assertEqual([x["amount_cents"] for x in self.l.entries("acct_1")], [5000, -5000])

    def test_an_entry_can_be_reversed_only_once(self):
        self.l.reverse(self.e["id"], "reverse-1")
        with self.assertRaises(LedgerError):
            self.l.reverse(self.e["id"], "reverse-2")
        self.assertEqual(self.l.balance("acct_1"), 0)

    def test_a_reversal_cannot_be_reversed(self):
        r = self.l.reverse(self.e["id"], "reverse-1")
        with self.assertRaises(LedgerError):
            self.l.reverse(r["id"], "reverse-2")

    def test_database_refuses_a_reversal_that_does_not_mirror(self):
        # Bypass the module entirely: the trigger still holds the rule.
        with self.assertRaises(sqlite3.IntegrityError):
            self.l.db.execute(
                "INSERT INTO entries (account, amount_cents, reverses, idempotency_key, created_at, prev_hash, hash)"
                " VALUES ('acct_1', -1, ?, 'bad', 'now', 'x', 'y')", (self.e["id"],))

    def test_missing_entry(self):
        with self.assertRaises(LedgerError):
            self.l.reverse(999, "reverse-1")


class Idempotency(unittest.TestCase):
    def setUp(self):
        self.l = Ledger()

    def test_retry_with_the_same_key_writes_once(self):
        a = self.l.post("acct_1", 5000, "charge-1")
        b = self.l.post("acct_1", 5000, "charge-1")
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(self.l.balance("acct_1"), 5000)

    def test_same_key_with_a_different_amount_is_refused(self):
        self.l.post("acct_1", 5000, "charge-1")
        with self.assertRaises(LedgerError):
            self.l.post("acct_1", 9000, "charge-1")
        self.assertEqual(self.l.balance("acct_1"), 5000)


class Amounts(unittest.TestCase):
    def test_floats_bools_and_zero_are_refused(self):
        l = Ledger()
        for bad in (49.99, True, 0, "5000"):
            with self.assertRaises(LedgerError):
                l.post("acct_1", bad, f"k-{bad!r}")
        self.assertEqual(l.entries(), [])


class TamperEvidence(unittest.TestCase):
    def test_clean_chain_verifies(self):
        l = Ledger()
        e = l.post("acct_1", 5000, "charge-1")
        l.reverse(e["id"], "reverse-1")
        l.post("acct_2", 1200, "charge-2")
        self.assertEqual(l.verify(), 3)

    def test_edit_after_removing_the_triggers_is_detected(self):
        l = Ledger()
        l.post("acct_1", 5000, "charge-1")
        l.post("acct_1", 700, "charge-2")
        l.db.execute("DROP TRIGGER entries_no_update")
        l.db.execute("UPDATE entries SET amount_cents = 50 WHERE id = 1")
        with self.assertRaises(TamperError):
            l.verify()

    def test_deleted_middle_row_is_detected(self):
        l = Ledger()
        for i in range(3):
            l.post("acct_1", 100, f"charge-{i}")
        l.db.execute("DROP TRIGGER entries_no_delete")
        l.db.execute("DELETE FROM entries WHERE id = 2")
        with self.assertRaises(TamperError):
            l.verify()


class Reconcile(unittest.TestCase):
    def setUp(self):
        self.l = Ledger()
        e = self.l.post("acct_1", 5000, "charge-1")
        self.l.post("acct_1", 700, "charge-2")
        self.l.reverse(e["id"], "refund-1")

    def test_matching_statement_reports_nothing(self):
        r = self.l.reconcile([("charge-1", 5000), ("charge-2", 700), ("refund-1", -5000)])
        self.assertEqual(r, {"missing_in_ledger": [], "missing_in_statement": [], "amount_mismatch": [],
                             "duplicate_in_statement": [], "difference_cents": 0})

    def test_each_kind_of_mismatch_is_listed(self):
        r = self.l.reconcile([("charge-1", 5000), ("charge-2", 750), ("charge-2", 750), ("charge-9", 100)])
        self.assertEqual(r["missing_in_ledger"], ["charge-9"])
        self.assertEqual(r["missing_in_statement"], ["refund-1"])
        self.assertEqual(r["amount_mismatch"], [("charge-2", 700, 750)])
        self.assertEqual(r["duplicate_in_statement"], ["charge-2"])
        self.assertEqual(r["difference_cents"], 700 - (5000 + 750 + 750 + 100))

    def test_reconcile_writes_nothing_and_can_filter_by_account(self):
        self.l.post("acct_2", 1200, "charge-3")
        before = self.l.entries()
        r = self.l.reconcile([("charge-3", 1200)], account="acct_2")
        self.assertEqual(r["missing_in_statement"], [])
        self.assertEqual(r["difference_cents"], 0)
        self.assertEqual(self.l.entries(), before)


if __name__ == "__main__":
    unittest.main()
