"""Seed data: the regulatory catalogue and the template library.

Both are *content*, not code, and both are versioned with the code rather than
loaded from a database dump. That is deliberate: an obligation's due-date rule
is the thing the regulatory test suite of section 9.2 asserts against, and a
rule that lives only in a production table cannot be tested before it ships.

:func:`app.data.seed.seed_all` upserts everything here into the database and is
safe to re-run — which is what makes a catalogue correction a deploy rather
than a migration.
"""
