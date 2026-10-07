# Install

Requirements, the installer, the database connection and the clinic config for Physics Chart
Review Assistant.

## Requirements

- **Windows:** 10 or 11 with the Edge WebView2 runtime and .NET Framework 4.x (present on a
  current Windows install)
- **ODBC driver:** ODBC Driver 18 or 17 for SQL Server, SQL Server Native Client 11.0, or the
  built-in "SQL Server" driver
- **Network:** access to the Mosaiq database
- **Login:** a Windows login the database trusts, or a SQL Server login from your site
- **Mosaiq client:** on the same machine, for the Open in Mosaiq button

## Installer

1. **Download:** `ChartReviewAssistant-Setup.exe` from the GitHub Releases page
2. **Run it:** no administrator rights; it installs to `%LOCALAPPDATA%\ChartReviewAssistant`
3. **Launch:** **Physics Chart Review Assistant** from the desktop or Start menu shortcut

- **Bundled:** Python and every dependency; nothing is downloaded at install or launch
- **Site files:** `db_config.toml` and `clinic_config.toml` are needed before the dashboard shows
  rows
- **Banner:** until both exist, it names the first missing one

## Database connection

- **File:** `db_config.toml` in the `data` folder under the install directory
- **Set it one of three ways:**
  - **Settings:** **Database** fields: Host, Database, Port (blank = 1433), Username, Password
  - **Copy:** `db_config.example.toml` in the same folder to `db_config.toml` and edit it
  - **Load DB Config:** in **Settings**, pick a file your clinic hands out; it is copied into
    `data`
- **Applies:** on the next launch
- **Authentication:**
  - **Windows (default):** Username and Password blank; connects as the logged-in Windows user;
    no credentials stored
  - **SQL Server:** both set; both stored in plain text in `db_config.toml`

## Clinic config

- **File:** `clinic_config.toml` in the same `data` folder
- **Holds:**
  - **Rooms:** one `[[machines]]` block each; `room` must equal the machine's Mosaiq staff name
  - **Codes:** chart check charge code, chart check note type, MRN Ident field
  - **Lists:** QCL task names, hold activities, MRNs to skip
- **Set it one of two ways:**
  - **Load Clinic Config:** in **Settings**, pick a file your clinic hands out; it is copied
    into `data` and shown in Settings at once
  - **Copy:** `clinic_config.example.toml` to `clinic_config.toml` and fill it in; the comments
    explain every key
- **Produce one from a configured install:**
  - **Save Clinic Config:** in **Settings**
  - **Content:** the baseline with that user's Settings applied
  - **Name:** `clinic_config.toml` to hand out

## Updating and uninstalling

- **Update:** run the latest `Setup.exe`; it upgrades in place and keeps `data`
- **Repair:** run the same `Setup.exe` again
- **Uninstall:** removes the program; keeps `data` (configs, settings, logs, bug reports);
  delete it by hand if wanted

## Demo mode

- **Shortcut:** Start menu, **Physics Chart Review Assistant (Demo)**
- **Data:** bundled database of made-up patients
- **Needs:** no Mosaiq access, no config files
- **Writes:** only `demo_data`, never the live config
