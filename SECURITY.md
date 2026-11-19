# Security policy

This is a reference / portfolio project, not a hosted service — there is no
production deployment to attack. But the code makes integrity claims, so if you
find a way those claims fail, I want to know.

## Reporting

If you find a real issue in the code — a way to edit or delete a row that `verify()`
does not catch, an idempotency or reversal bypass, a hash-chain weakness, or a
concurrency hole in the write path — please report it privately:

- Open a **GitHub Security Advisory** on this repository (Security → Report a
  vulnerability), or
- email **security@shadowvivo.com**.

Please don't open a public issue for a security problem until it's fixed. No bounty
is offered, but I'll credit you unless you'd rather stay anonymous.

## Scope

In scope: the integrity properties in [`DESIGN.md`](DESIGN.md) — append-only
enforcement, reversal mirroring, idempotency, and the hash chain.

Out of scope (documented limitations, not bugs — see DESIGN.md "What this does NOT
defend against"): tail truncation without an external anchor, a full rewrite by
someone who already has file write access and the hashing code, trusted
timestamping, and anything requiring OS-level access control or backups.
