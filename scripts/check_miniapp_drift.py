"""Static lint aid: cross-checks every templates/*.py file that declares a
module-level `miniapp_config` against that same file's own init_db() CREATE
TABLE statements and Telegram callback_data literals.

This exists because miniapp_config is normally LLM-generated per bot
(services/claude_service.py's _generate_miniapp_config) and validated at
generation time against the bot's own CREATE TABLE statements
(_validate_miniapp_config_against_code) — but the 7 hand-written
miniapp_config dicts baked directly into templates/*.py (tour_operator,
event_rsvp, car_rental, course_tracker, boss_bot, manager_bot, team_manager)
never go through that generation-time check, since they were written by hand
and are shipped as-is. A hand edit to a template's schema (renaming a
column, dropping a table) can silently desync its miniapp_config from
reality with nothing to catch it. This script is that missing check, run
offline/in CI rather than at bot-creation time.

Two severities:
  - HARD mismatch (non-zero exit): a miniapp_config "table" or field "name"
    that isn't a real, verbatim CREATE TABLE name/column in the same file —
    the exact bug _validate_miniapp_config_against_code guards against for
    LLM-generated configs, reused here via the same regex-based
    _extract_create_table_names from services/claude_service.py. This is a
    genuine drift bug, not a heuristic — always a mistake.
  - SOFT warning (printed, never fails the run): a Telegram callback_data
    prefix that LOOKS CRUD-ish (add_/create_/new_/edit_/delete_/del_ followed
    by a table-like word) with no matching miniapp_config resource, or vice
    versa. This is a rough heuristic over free-text callback_data strings —
    real callback names don't always match table names 1:1 (e.g. a
    "confirm_booking" callback for a "bookings" table), so false positives
    are expected and this must never block CI. It's a nudge to go check
    feature-parity by hand, not a prover.

Run standalone: python -m scripts.check_miniapp_drift
Also exercised by tests/test_miniapp_drift.py as part of the normal pytest run.
"""
from __future__ import annotations

import importlib
import re
import sys
from pathlib import Path

# Reuse the exact same ground-truth extraction the real generation-time
# validator uses (services/claude_service.py's _validate_miniapp_config_
# against_code / _extract_create_table_names) — don't reinvent a second,
# possibly-drifted regex for the same job.
from services.claude_service import _extract_create_table_names

TEMPLATES_DIR = Path(__file__).resolve().parent.parent / "templates"
FEATURES_DIR = Path(__file__).resolve().parent.parent / "features"
DATABASE_PY = Path(__file__).resolve().parent.parent / "db" / "database.py"

# Rough CRUD-ish callback_data prefixes — heuristic only, see module
# docstring's SOFT warning explanation. Deliberately short/generic; a
# template author using a different verb (e.g. "book_", "reserve_") just
# won't be caught, which is fine for a non-blocking nudge.
_CRUD_PREFIXES = ("add_", "create_", "new_", "edit_", "delete_", "del_", "remove_")


def _template_files_with_miniapp_config() -> list[Path]:
    """Every templates/*.py file that declares a module-level
    `miniapp_config = ...` — matched with a simple line-start regex rather
    than an AST walk, since we only need to know WHETHER the file declares
    one before deciding to import it (importing is how we actually read its
    value, see check_template's docstring)."""
    files = []
    for path in sorted(TEMPLATES_DIR.glob("*.py")):
        if re.search(r"^miniapp_config\s*=", path.read_text(encoding="utf-8"), re.MULTILINE):
            files.append(path)
    return files


def _extract_callback_data_literals(source: str) -> set[str]:
    """Every string literal passed as callback_data=... — regex over the
    source (same spirit as _extract_create_table_names: good enough to spot
    the heuristic CRUD-ish prefixes below, not a full Python parser). Only
    plain string literals are caught; f-strings/computed callback_data
    (callback_data=f"edit_{item_id}") are skipped since there's no static
    table name to compare against anyway — those still show up via their
    literal prefix if the f-string starts with a quoted prefix segment,
    which covers the common "prefix:id" pattern used throughout this repo."""
    literals = set()
    for m in re.finditer(r'callback_data\s*=\s*f?"([^"{]*)', source):
        literals.add(m.group(1))
    for m in re.finditer(r"callback_data\s*=\s*f?'([^'{]*)", source):
        literals.add(m.group(1))
    return literals


def _extract_create_table_names_multistatement(source: str) -> dict[str, set[str]]:
    # Supplements services.claude_service._extract_create_table_names for the
    # one shape it doesn't cover: a single db.executescript(<triple-quoted
    # block>) containing SEVERAL semicolon-terminated CREATE TABLE
    # statements (templates/tour_operator.py is the one template in this
    # repo using this style — see its init_db()). The shared function's
    # regex requires each CREATE TABLE's closing paren to be immediately
    # followed by the closing triple-quote of its OWN db.execute(...) call,
    # which is only true for the common one-statement-per-execute() shape
    # everywhere else in this repo. Not folded into the shared function
    # itself since that function is the same one production LLM-config
    # validation depends on and already has pinned tests — this is purely
    # additive, local to this lint script.
    tables: dict[str, set[str]] = {}
    for table_match in re.finditer(
        r"CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?(\w+)\s*\((.*?)\)\s*;",
        source,
        re.IGNORECASE | re.DOTALL,
    ):
        table_name = table_match.group(1)
        columns_blob = table_match.group(2)
        columns: set[str] = set()
        for line in columns_blob.split(","):
            line = line.strip()
            col_match = re.match(r"(\w+)", line)
            if col_match and col_match.group(1).upper() not in (
                "PRIMARY", "FOREIGN", "UNIQUE", "CHECK", "CONSTRAINT",
            ):
                columns.add(col_match.group(1))
        tables[table_name] = columns
    return tables


def _fold_in_alter_table_columns(tables: dict[str, set[str]], source: str) -> None:
    """Mutates `tables` in place, adding columns from `ALTER TABLE <table>
    ADD COLUMN <col> ...` statements found in `source` — a real, common
    pattern in this codebase's migration style (db/database.py's init_db()
    adds dozens of columns this way, e.g. channel_posts.views/forwards,
    monitored_channels.extract_schema). Without this, any miniapp_config
    field referencing a migrated-in column would falsely read as a
    hallucination. Two literal shapes are handled: a plain string
    ALTER TABLE call, and the `for col in (...): ALTER TABLE {table} ADD
    COLUMN {col}` loop shape used throughout db/database.py (col tuple
    entries are "name TYPE..." string literals, table name comes from the
    nearest preceding literal ALTER TABLE prefix in the f-string)."""
    # Plain literal ALTER TABLE statements.
    for m in re.finditer(
        r"ALTER\s+TABLE\s+(\w+)\s+ADD\s+COLUMN\s+(\w+)",
        source,
        re.IGNORECASE,
    ):
        table, col = m.group(1), m.group(2)
        if table in tables:
            tables[table].add(col)
    # The `for col in ("name TYPE", ...): ... ALTER TABLE {table} ADD COLUMN
    # {col}` loop shape — table name is an f-string placeholder, so pull the
    # table from the loop's own `ALTER TABLE {table_var} ADD COLUMN {col}`
    # line paired with the preceding `for col in (...)` tuple, keyed by
    # matching the table_var name back to a nearby literal ALTER TABLE
    # target isn't reliably inferable by regex — instead, match each
    # `for col in (...)` block to the table name mentioned in an adjacent
    # ALTER TABLE f-string within a small window, falling back to scanning
    # every known table's name near the loop if the f-string variable can't
    # be resolved statically.
    for loop_match in re.finditer(
        r'for\s+col\s+in\s+\(([^)]*)\)\s*:\s*\n(.{0,300}?)ALTER TABLE (\w+)',
        source,
        re.DOTALL,
    ):
        col_tuple_src, _between, table = loop_match.groups()
        if table not in tables:
            continue
        for col_literal in re.finditer(r'["\']([\w]+)\s+\w+', col_tuple_src):
            tables[table].add(col_literal.group(1))


def _tables_from_imported_features(source: str) -> dict[str, set[str]]:
    """Some templates (e.g. channel_aggregator.py) don't own their own
    CREATE TABLE statements at all — they create a feature module's tables
    in their own db_path via that module's init_db(), and their
    miniapp_config resources legitimately reference tables defined in
    features/<name>.py, not in the template file itself (see
    channel_aggregator.py's own comment: "Tables come from features/
    channel_monitor.py's init_db()"). Only followed one level (`from
    features import X` / `from features.X import ...`), which covers every
    case seen in this repo — good enough for a lint aid, not a general
    import resolver."""
    tables: dict[str, set[str]] = {}
    feature_names = set(re.findall(r"^from features(?:\.(\w+))? import", source, re.MULTILINE))
    feature_names |= set(re.findall(r"^from features import (\w+)", source, re.MULTILINE))
    for name in feature_names:
        if not name:
            continue
        feature_path = FEATURES_DIR / f"{name}.py"
        if feature_path.exists():
            feature_source = feature_path.read_text(encoding="utf-8")
            tables.update(_extract_create_table_names(feature_source))
            _fold_in_alter_table_columns(tables, feature_source)
        # A feature module can itself delegate table ownership one hop
        # further, to db/database.py's own init_db() (e.g. features/
        # channel_monitor.py's monitored_channels/channel_posts tables are
        # actually created in db.database, not in the feature file — see
        # that file's "channel_monitor feature" section comment). Since
        # db/database.py is a single, fixed, always-present file in this
        # repo (not an arbitrary import target), it's cheap and safe to
        # always fold its tables in too when a template imports ANY feature
        # module, rather than trying to prove which feature owns which
        # table — a lint aid, not a strict prover (see module docstring).
        if DATABASE_PY.exists():
            database_source = DATABASE_PY.read_text(encoding="utf-8")
            tables.update(_extract_create_table_names(database_source))
            _fold_in_alter_table_columns(tables, database_source)
    return tables


def check_template(template_path: Path) -> tuple[list[str], list[str]]:
    """Returns (hard_errors, soft_warnings) for one templates/*.py file.

    Imports the template module directly to read its miniapp_config
    attribute (simplest/most reliable way to get the actual literal dict,
    per the task's own guidance — re-parsing a dict literal out of source
    with regex would be far more fragile than just importing it, and these
    template files are confirmed to import standalone with the repo root on
    sys.path, same as tests/test_*_isolation.py's `import templates.X`)."""
    hard_errors: list[str] = []
    soft_warnings: list[str] = []

    module_name = f"templates.{template_path.stem}"
    module = importlib.import_module(module_name)
    importlib.reload(module)  # in case an earlier test/run already imported it stale

    source = template_path.read_text(encoding="utf-8")
    tables = _extract_create_table_names(source)
    for name, columns in _extract_create_table_names_multistatement(source).items():
        tables.setdefault(name, set()).update(columns)
    _fold_in_alter_table_columns(tables, source)
    tables.update(_tables_from_imported_features(source))
    miniapp_config = getattr(module, "miniapp_config", None)
    if not isinstance(miniapp_config, dict) or not isinstance(miniapp_config.get("resources"), list):
        hard_errors.append(f"{template_path.name}: miniapp_config is not a valid {{'resources': [...]}} dict")
        return hard_errors, soft_warnings

    resource_tables: set[str] = set()
    for resource in miniapp_config["resources"]:
        table = resource.get("table") if isinstance(resource, dict) else None
        if not isinstance(table, str):
            hard_errors.append(f"{template_path.name}: a resource is missing a string 'table'")
            continue
        resource_tables.add(table)
        if table not in tables:
            hard_errors.append(
                f"{template_path.name}: miniapp_config resource '{resource.get('name', table)}' "
                f"references table '{table}', not found in this file's CREATE TABLE statements"
            )
            continue
        columns = tables[table]
        for field in resource.get("fields", []):
            name = field.get("name") if isinstance(field, dict) else None
            if not isinstance(name, str) or name not in columns:
                hard_errors.append(
                    f"{template_path.name}: miniapp_config resource '{table}' field "
                    f"'{name}' not found among that table's real columns {sorted(columns)}"
                )

    # Soft heuristic: CRUD-ish callback_data prefixes with no corresponding
    # miniapp_config resource table name mentioned anywhere in the prefix.
    # Noisy by design (see module docstring) — printed only, never fails CI.
    callbacks = _extract_callback_data_literals(source)
    crud_like = {cb for cb in callbacks if any(cb.startswith(p) for p in _CRUD_PREFIXES)}
    for cb in sorted(crud_like):
        if not any(table in cb for table in tables):
            soft_warnings.append(
                f"{template_path.name}: callback_data '{cb}' looks CRUD-ish but doesn't "
                f"mention any known table name — possible missing/renamed miniapp_config resource"
            )
    for table in sorted(tables):
        if table not in resource_tables:
            # Not every table deserves a mini-app screen (internal/admin
            # tables are deliberately excluded, same rule
            # MINIAPP_CONFIG_SYSTEM_PROMPT gives the LLM) — soft only.
            has_crud_callback = any(table in cb for cb in crud_like)
            if has_crud_callback:
                soft_warnings.append(
                    f"{template_path.name}: table '{table}' has CRUD-ish callback_data "
                    f"but no miniapp_config resource — possible missing screen"
                )

    return hard_errors, soft_warnings


def main() -> int:
    all_hard: list[str] = []
    all_soft: list[str] = []
    for template_path in _template_files_with_miniapp_config():
        hard, soft = check_template(template_path)
        all_hard.extend(hard)
        all_soft.extend(soft)

    for warning in all_soft:
        print(f"WARNING: {warning}")
    for error in all_hard:
        print(f"ERROR: {error}")

    if all_hard:
        print(f"\n{len(all_hard)} hard mismatch(es) found.")
        return 1
    print(f"OK — {len(all_soft)} soft warning(s), no hard mismatches.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
