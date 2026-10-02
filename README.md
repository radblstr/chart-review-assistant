# Physics Chart Review Assistant

A radiation oncology chart review dashboard that reads live from the Mosaiq database and flags 
chart review status for each patient with a scheduled treatment within the specified time window.

![Chart Review Assistant board](images/demo.png)

## Features

### Dashboard

- Treatment-room checklist and a patient selection window: past, surrounding or next N weeks, or a
  custom date range.
- Columns: today's treatment time, room, name, MRN, site, Rx, site fractions treated, ICC date,
  last treated fraction date, last WCC date, fractions since first fraction or last check, pending
  QCLs (optional), WCCs completed/expected, FCC date, charges billed/expected.
- Rows colored green (no warnings or errors), yellow (warnings) or orange (errors). Cell icons list the
  findings on hover, and header tooltips explain each rule.
- Multi-column sort (Shift-click), remembered between sessions.
- Hide rows with checkboxes and a Hide/Unhide toggle. Hidden rows persist between sessions.
- Info pane for the selected row with all of the patient's sites and its findings by level.
- Concurrent left and right prostate sites merge into one row.
- Manual refresh and auto-refresh on a set interval.
- Help popover with refresh and sort help and the row color legend.

### Settings

- One tab per location: label, Mosaiq room name, check toggles and ICC/SBRT thresholds, with
  Restore Defaults.
- Add, remove and restore default locations.
- Pending QCLs column on/off, refresh interval, database connection fields.
- Save Clinic Config writes the current settings out as a clinic baseline file.
- Load DB Config and Load Clinic Config copy a picked `db_config.toml` or `clinic_config.toml`
  into the `data` folder, asking before replacing an existing one. Loading a clinic config shows
  it in Settings at once and discards location and General changes made in Settings.
- Clinic-level keys in `clinic_config.toml`: MRN Ident field, chart-check CPT codes, chart-check
  note types, hold activities, MRNs to skip.

### Feedback

- Bug report and suggestion dialogs with file attachments. Bug reports capture the board data and
  app state (see [Bug reports](#bug-reports)).

> ⚠️ **Not a medical device.** Provided "as is", with no warranty, and not for clinical use without
> local commissioning and validation. See [Disclaimer — not a medical device](#disclaimer--not-a-medical-device).

## Disclaimer — not a medical device

**This software is NOT a medical device.** It has not been reviewed, cleared, approved, certified,
or registered by the U.S. Food and Drug Administration (FDA), under the EU Medical Device Regulation
(MDR 2017/745), or by any other regulatory authority, and it carries no CE marking. It is **not
intended for diagnosis, treatment, or the prevention of disease**, and it must **not** be used as a
basis for any clinical decision.

The software is provided **"AS IS", WITHOUT WARRANTY OF ANY KIND**, express or implied, including
but not limited to the warranties of merchantability, fitness for a particular purpose, accuracy,
and non-infringement (see the [LICENSE](LICENSE)). It is offered as an **aid** to a qualified
medical physicist's own chart review — a warning-focused audit tool — and it does **not** replace
the independent professional judgment, verification, and secondary checks required by your clinic's
quality-assurance program and applicable professional standards.

**The user is solely responsible for commissioning and validating this software** in their own
clinical environment, against their own database, workflows, and standards, before placing any
reliance on its output, and for revalidating it after any update, configuration change, or change to
the source data. The authors and contributors accept **no liability** for any loss, harm, or damage
of any kind arising from its use or misuse. **Use it entirely at your own risk.**

## Before you start

Two ways to run it:

- **Demo mode** works out of the box: a bundled demo database of made-up patients, no Mosaiq
  access, no patient data. Use it to see what the board does. See [Demo mode](#demo-mode).
- **Your clinic** needs two files the installer does not ship, because they are specific to your
  site: `db_config.toml` (how to reach your Mosaiq server) and `clinic_config.toml` (your
  treatment rooms, charge code and QCL task names). Until both exist the board shows a warning
  naming the first missing one. The [Install](#install) steps below cover both.

## Install

1. Download the latest **`ChartReviewAssistant-Setup.exe`** from the
   [Releases page](../../releases/latest).
2. Run it. No administrator rights are needed — everything installs under your user profile.
3. Launch from the **Physics Chart Review Assistant** desktop or Start-menu shortcut.
4. Set the database connection (see [Database connection](#database-connection)). Until the host
   and database are set, the dashboard shows a "database access is not configured" warning.
5. Set up the clinic config: in the `data` folder under the install directory, copy
   `clinic_config.example.toml` to `clinic_config.toml` and fill in your treatment rooms (one
   `[[machines]]` block each, `room` exactly as the machine is named in Mosaiq), the chart-check
   charge code (`cc_cpt_code`) and, if you want the Pending QCLs column, your QCL task names. Your
   clinic may hand you a ready-made `clinic_config.toml` instead; open **Settings** and click
   **Load Clinic Config** to copy it into the `data` folder. Without this file the board has
   no rooms. To produce one from a configured install, open **Settings** and click **Save Clinic
   Config**: a save dialog asks where to write the clinic file with that user's Settings applied;
   name it `clinic_config.toml` to hand it out.

The installer includes Python and everything the app needs; nothing is downloaded.

To update, download and run the latest `Setup.exe` again. It upgrades in place and keeps your
settings. If an install or update fails, run the same `Setup.exe` again to repair it.

Uninstalling removes the program but keeps the `data` folder under the install directory
(`db_config.toml`, settings, logs, bug reports). Delete it by hand if you want it gone.

## Database connection

The connection is stored in `db_config.toml` in the `data` folder under the install directory. It
is not shipped with the installer. Set it either way:

- In the app, open **Settings** and fill in the **Database** fields: Host, Database, Port (blank =
  1433), Username and Password. Changes are written to `db_config.toml` and apply on the next
  launch.
- Copy `db_config.example.toml` in the same folder to `db_config.toml` and edit it.
- Use a `db_config.toml` distributed by your clinic: open **Settings**, click **Load DB Config**
  and pick the file. It is copied into the `data` folder and applies on the next launch.

Leave Username and Password blank to use Windows authentication. When both are set, SQL Server
authentication is used and both are stored in plaintext in `db_config.toml`.

## Clinic-specific data

The database pulls follow one clinic's Mosaiq conventions, so not every pull works as-is at every
clinic (for example, which Ident field holds your MRN). Table and column name customizations
are available in `clinic_config.toml`; see the comments in `clinic_config.example.toml`. Check
the board against your own database during commissioning.

## Requirements

- Windows 10 or 11, with the Microsoft Edge WebView2 runtime and .NET Framework 4.x (both are
  present on a current Windows install).
- A **SQL Server ODBC driver**. Windows includes the legacy "SQL Server" driver; ODBC Driver 18,
  17 or SQL Server Native Client 11.0 is used instead when installed.
- Network access to the clinical Mosaiq database.

## What it does

- Lists every patient with a scheduled or treated fraction across the selected rooms and date range,
  read live from Mosaiq with no manual data entry.
- One sortable row per treatment site, color-coded by finding severity, with per-column icons that
  list notifications, warnings, and errors on hover.
- An info pane grouping the selected course's findings as notifications, warnings, and errors.
- Per-machine check toggles and general options through an in-app Settings dialog.

## Bug reports

**Report a bug** saves a folder named `bug_YYYYMMDD_HHMMSS` with the board data the reviewer was
viewing and the app state. This is by design, so a report carries what is needed to reproduce the
problem. **The board data includes PHI**: patient names, patient IDs and treatment dates.

Reports are written to `bug_report_dir` in `clinic_config.toml` when set (e.g. a clinic network
share), else to `bug_reports` in the `data` folder. Point `bug_report_dir` only at storage approved
for PHI.

## Demo mode

Demo mode runs the board against a bundled demo database of made-up patients, with no Mosaiq
access and no patient data. It reads and saves settings in `chart_review_assistant/demo_data/`,
never the live config files. After installing, start it from the Start-menu shortcut "Physics
Chart Review Assistant (Demo)" (`ChartReviewAssistant-Demo.exe`). From a source checkout:

```
uv sync
chart_review_assistant\demo_launcher.bat
```

Or set `CRA_DEMO_MODE=1` before starting the app.

## Build from source (developers)

```
uv build                 # build the wheel
uv run --extra test pytest chart_review_assistant/tests -q
python -m chart_review_assistant.app
```

Running from source needs either a database connection or demo mode. Without `CRA_DATA_DIR` set,
the data folder is the `chart_review_assistant` package folder, so `db_config.toml` goes there.

To add a regression case of your own, fill in the `USER_*` variables at the top of
`chart_review_assistant/tests/make_snapshots.py` (room, prescription, fractions treated, chart-check
notes, charges, expected findings — the defaults are a complete example) and run
`uv run python -m chart_review_assistant.tests.make_snapshots --user`. The case lands in
`tests/snapshots/` and the test suite replays it.

The installer build pipeline lives in [`deploy/`](deploy).

## License

[Apache-2.0](LICENSE).
