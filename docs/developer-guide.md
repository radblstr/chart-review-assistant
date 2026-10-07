# Developer Guide

Project design, how to add a check, testing and building from source, for contributors to
Physics Chart Review Assistant. Runtime architecture is in the [admin guide](admin-guide.md).

## Project design

- **Modules:** four, in call order
  - **`query_db.py`:** every SQL statement; returns a dict of DataFrames with Mosaiq's native
    column names and nothing derived
  - **`courses.py`:** `Course` and `Site`; slices the pull per patient and derives fraction
    lists, windows, counts and completion times
  - **`checks.py`:** the rule table (`CHECKS`) and one audit function per category; each
    reads derived values and appends findings
  - **`app.py`:** the Dash layout, callbacks and grid; renders findings, never computes them
- **Support modules:**
  - **`config.py`:** the three config layers, Settings thresholds, clamping
  - **`snapshot.py`:** builds a mock Mosaiq SQLite database from a pull; used by the tests
  - **`mosaiq_control.py`:** the Open in Mosaiq window messages
- **Data flow rules:**
  - **Pull:** raw rows only; no renaming, no derived columns, no config values
  - **Derivation:** in `courses.py`; config is read where it is consumed, not stored on the
    pull or passed through constructors
  - **Findings:** only `checks.py` produces them; the UI shows them
  - **Writes:** none to Mosaiq, anywhere

### Adding a check

- **Rule entry:** add a dict under its category in `CHECKS` in `checks.py` with `id`, `label`,
  `field`, `level`, `message`
  - **`field`:** the dashboard column the icon belongs to
  - **`level`:** `info`, `warning` or `error`
- **Condition:** append the entry in the category's audit function when the condition holds
- **Derived value:** if the rule needs a value the model lacks, derive it in `courses.py` from
  the raw frames
- **Settings:** the rule appears in each room's check list from `CHECKS`
  - **New ICC rule:** also needs an entry in the `extras` dict of the Settings builder in
    `app.py`
  - **New category:** also needs a title in `GROUPS`
- **Tests:** add a snapshot case that fires it and one that does not; see Testing

### Conventions

- **Style:** match the surrounding code; single quotes; `dict()` over `{}`; 100-character lines
- **Docstrings:** Google style; summary on the opening line
- **Comments:** a blank line before each comment block; no inline comments
- **Scope:** one change per pull request; no refactors alongside a fix
- **PHI:** no patient data in code, tests, fixtures, commits or issues; the test corpus is
  synthetic

### Pull requests

- **Where:** the public repository on GitHub
- **How:** fork, branch, commit, open a pull request against `main`
- **Review:** on GitHub; the tests must pass
- **Merge:** applied in the private development repository and shipped in the next release;
  the pull request is closed with that version, not merged

## Testing

Every value on the dashboard can be traced to how it was produced, without reading the code line by
line and without patient data leaving the clinical system.

- **Run the suite:** `uv run --extra test pytest chart_review_assistant/tests -q`
- **Regression corpus:**
  - **Cases:** 28 synthetic, one per rule and edge case; due, missed, late note, SBRT
    thresholds, short courses, charge remainders, prostate merge, multi-room sites
  - **Form:** each case is a SQLite database with Mosaiq's table and column names, plus a
    frozen baseline of the rendered dashboard
  - **Test:** the suite runs the real SQL against each and compares the dashboard, row by row, to
    the baseline
  - **Effect:** a rule change that moves a finding fails a test
  - **Rebuild:** `python -m chart_review_assistant.tests.make_snapshots` after any rule change;
    review the diff
- **Add a case without code:**
  - **Edit:** the `USER_*` block at the top of `chart_review_assistant/tests/make_snapshots.py`
  - **Run:** `python -m chart_review_assistant.tests.make_snapshots --user`
  - **Result:** the case joins the corpus and the suite replays it
- **Unit tests:** `test_courses.py` for model edge cases the corpus cannot express;
  `test_config.py` for the config layers and Settings
- **Demo mode:** a bundled database of made-up patients covering the same scenarios; every
  finding visible without database access or PHI
- **Logging:**
  - **Per pull:** one summary line; window, room count, patient, course and row counts, elapsed
    time
  - **Warnings:** an unreachable database or an empty pull
  - **Identifiers:** Mosaiq patient ids at debug level only; never names or MRNs

## Build from source

- **Environment:** `uv sync` in the repository root creates `.venv` with the pinned
  dependencies
- **Run:** `python -m chart_review_assistant.app`; needs a database connection or demo mode
- **Demo mode:** `chart_review_assistant\demo_launcher.bat`, or set `CRA_DEMO_MODE=1`
- **Data folder:** the `chart_review_assistant` package folder unless `CRA_DATA_DIR` is set;
  `db_config.toml` goes there
- **Wheel:** `uv build`
- **Installer:** `pwsh -File deploy/build_installer.ps1`; needs uv, Inno Setup 6 and the .NET
  Framework C# compiler; see `deploy/README.md`
- **Version:** `__version__` in `chart_review_assistant/__init__.py` is the single source
