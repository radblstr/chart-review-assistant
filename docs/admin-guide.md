# Admin Guide

How Physics Chart Review Assistant reads Mosaiq, where its configuration comes from, what it
writes to disk and how it handles PHI. Setup is in [Install](install.md); the dashboard itself is
in the [user guide](user-guide.md).

## Architecture

- **Application:** a Dash web app served on a local port, shown in a native window (pywebview
  on Edge WebView2)
- **Grid:** AG Grid
- **Network:** nothing listens beyond the local machine

### Database pull

- **Fresh:** every refresh reads Mosaiq again
- **First query:** patients with a scheduled fraction in the selected rooms and window
- **Rest:** run concurrently for those patients; return raw rows with Mosaiq's column names
- **Read-only:** all queries are `SELECT`; the app needs no write permission

| Frame | Mosaiq tables | Contents |
|---|---|---|
| patients | Patient, Ident | names and the configured MRN field |
| sites | Site, PatCPlan, Topog | site name, prescription, course key, diagnosis |
| calendar | PatTxCal | scheduled and treated fraction times |
| dose_history | Dose_Hst | delivered fractions |
| schedule | Schedule, Staff | appointments and the room (machine staff name) |
| cc_notes | Notes | notes of the chart check types |
| cc_charges | Charge, CPT | chart check charges |
| qcls | QCLTask, QCL | pending physics tasks |

### Data model

- **Courses:** care plans (`PCP_ID`)
- **Per course:**
  - **Fractions:** treated and scheduled lists
  - **Records:** chart check notes and charges inside the course span
  - **WCC:** 5-fraction windows; completed and missed counts
  - **FCC:** status
- **Per site:**
  - **Rx:** prescription
  - **SBRT:** flag
  - **ICC:** status
- **Merge:** left/right prostate sites merge here
- **Rules:** a separate module reads the derived values and appends findings; see the developer
  guide

### Dashboard

- **Rows:** one grid row per site, grouped by course
- **Icons:** each finding attached to its column with a hover list
- **Color:** the row's worst level
- **Info pane:** the selected course
- **State file:** rooms, window, sort, hidden rows, pane height, window position
- **Refresh:** on the Settings interval and on demand

### Open in Mosaiq

- **Scope:** the only action the app takes outside itself
  - **Find:** the Select Patient control in the running Mosaiq client via Windows UI Automation
  - **Click:** posted to its drop-down arrow
  - **Paste:** the MRN into the patient field, then submit
- **Mechanism:** window messages to Mosaiq's own handles
  - **No:** mouse movement, keystrokes, focus change
  - **Database:** nothing written
- **Not found:** the button reports it and does nothing

### Configuration

- **Layers:** each overrides the one before
  1. **Code defaults:** built in
  2. **`clinic_config.toml`:** clinic baseline, distributed to users, loaded through Settings
  3. **`user_config.toml`:** the user's Settings changes, never distributed
- **Settings edits:** write only the user layer
- **Save Clinic Config:** writes a new baseline with the user's overrides applied
- **Load Clinic Config:** replaces the baseline; discards the user's location and General
  overrides

### Files

- **Location:** the `data` folder
  - **Installed:** `%LOCALAPPDATA%\ChartReviewAssistant\data`
  - **From source:** the package folder, or `CRA_DATA_DIR` when set

| File | Contents |
|---|---|
| `db_config.toml` | database connection |
| `clinic_config.toml` | clinic baseline |
| `user_config.toml` | Settings overrides |
| `app_state.json` | dashboard selection and layout, last selected row |
| `logs/cra.log` | application log |
| `logs/feedback.log` | suggestions |
| `bug_reports/` | bug report captures, unless `bug_report_dir` points elsewhere |

## PHI and security

- **Database access:**
  - **Queries:** every one is a `SELECT`
  - **Default login:** the logged-in Windows user, with that user's existing access; no
    credentials stored
  - **SQL Server login:** username and password stored in plain text in `db_config.toml`
- **Network:**
  - **Serving:** its own page on a local port, shown in a native window
  - **Outbound:** no downloads, no reporting, no outside service
  - **Bundled:** Python and every package ship in the installer
- **On screen and in memory:**
  - **Data:** names, MRNs, sites and treatment dates for patients in the window
  - **Lifetime:** held for the session, replaced on every refresh
- **Written to disk:** under `data` unless configured otherwise
  - **`app_state.json`:** selection and layout, plus the last selected row's key (course id,
    site id, site name, room); no name or MRN
  - **`logs/cra.log`:** one line per refresh (window, counts, elapsed time); config saves;
    warnings for an unreachable database, an empty pull or an invalid config value
  - **`logs/cra.log` at debug level (`CRA_LOG_LEVEL=DEBUG`):** adds Mosaiq patient ids; never
    names or MRNs; a connection warning may contain the server name
  - **`logs/feedback.log`:** each suggestion's text and the submitting Windows username
  - **`bug_reports/bug_YYYYMMDD_HHMMSS/`:** contains PHI; the dashboard data (names, MRNs, dates)
    and app state
    - **Redirect:** `bug_report_dir` in `clinic_config.toml`
    - **Storage:** point it only at a location approved for PHI
- **Open in Mosaiq:** window messages to the Mosaiq client on the same machine; sends the MRN,
  reads nothing back
- **Demo mode:** invented patients; reads and writes only `demo_data`; no PHI, no database
- **Uninstall:** removes the program, leaves `data` including bug reports; delete it by hand
