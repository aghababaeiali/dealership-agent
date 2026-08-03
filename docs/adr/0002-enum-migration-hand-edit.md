# ADR 0002: Hand-edited the initial migration's enum literals

## Status

Accepted.

## Context

`OrderStatus` and `TestDriveStatus` in `db/models.py` are `enum.StrEnum`
subclasses (e.g. `PENDING = "pending"`). SQLAlchemy's `sa.Enum(...)`, by
default, uses the Python enum member *names* ("PENDING") as the Postgres
enum labels, not the `.value` strings ("pending"). This was only caught
after autogenerating and applying the initial migration - `enum_range()`
showed `{PENDING, CONFIRMED, ...}` in the database, not the lowercase
values the project's status lifecycle is documented with.

## Decision

- Fixed the root cause in `db/models.py`: both enum columns now pass
  `values_callable=_enum_values`, so SQLAlchemy stores `.value` strings.
- Rather than generate a corrective migration on top of the broken one,
  **hand-edited the already-applied initial migration**
  (`cbd3d488305d_initial_schema.py`) to create the Postgres enum types with
  the correct lowercase literals from the start, and added an explicit
  `DROP TYPE IF EXISTS ...` to its `downgrade()` (native Postgres enum types
  are shared across columns and aren't reliably dropped by `op.drop_table`
  when more than one column references them - this was a second bug found
  while doing the rebuild below).
- Rebuilt the local schema from scratch (`alembic downgrade base` then
  `alembic upgrade head`) to apply the corrected migration.

This was safe only because no real data had been loaded into any table yet
(Part D's ETL load happened after this fix) and the migration had not been
shared, pushed, or applied anywhere outside local dev. Editing an
already-applied, already-committed migration is normally the wrong move -
once a migration has run anywhere else (another developer's machine, CI,
any deployed environment), the correct fix is always a new forward
migration, never rewriting history. This exception applied only because the
blast radius was provably zero.

## Consequences

- The initial schema migration in git history does not match what a
  bisector would see if they checked out the commit between the original
  (buggy) apply and this fix - there is only one version of that file, ever,
  in the repo. This is intentional and is what "no data loaded yet" buys.
- Any future migration bug discovered after this point (or after any data
  load) must be fixed with a new migration, not by editing history again.
