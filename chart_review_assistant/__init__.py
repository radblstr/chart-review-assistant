# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Mosaiq database connection.

- get_engine returns a cached SQLAlchemy engine (used read-only) for the Mosaiq DB, configured
  from db_config.toml in the data directory.
- _load_db_config reads that connection config (distributed out of band, never committed);
  load_db_config is its tolerant wrapper (missing or malformed file -> {}), and save_db_config
  writes the file atomically.
- db_configured and db_host report the configured state for the dashboard's banners.
- _build_url builds the mssql+pyodbc connection URL: SQL auth when a username/password are set,
  Windows authentication otherwise.
- _get_sql_server_driver picks the newest installed SQL Server ODBC driver.
- configure_logging wires the shared 'cra' logger to a file and the console, level from
  CRA_LOG_LEVEL.
"""

__version__ = '0.1.0a2'

import logging
import os
import tomllib

import pyodbc
from sqlalchemy import create_engine, URL

from chart_review_assistant import config

logger = logging.getLogger('cra')

# SQL Server's default TCP port, used when db_config.toml omits one.
DB_PORT_DEFAULT = 1433


def _get_sql_server_driver():
    """Newest installed SQL Server ODBC driver name, from a most-preferred-first list

    Returns:
        str: The driver name

    Raises:
        RuntimeError: none of the supported drivers is installed.
    """
    preferred_drivers = [
        'ODBC Driver 18 for SQL Server',
        'ODBC Driver 17 for SQL Server',
        'SQL Server Native Client 11.0',
        'SQL Server']
    installed_drivers = set(pyodbc.drivers())
    for driver in preferred_drivers:
        if driver in installed_drivers:
            return driver
    raise RuntimeError(
        'No SQL Server ODBC driver was found. '
        f'Installed ODBC drivers: {sorted(installed_drivers)}')


def _load_db_config():
    """Load the [database] connection parameters from db_config.toml in the data dir.

    CRA_DATA_DIR sets the data dir, else the package dir; demo mode points config.DB_CONFIG_FILE at
    demo_data/. The file is distributed out of band and never committed.

    Returns:
        dict[str, object]: The [database] table, or {} when the file has no [database] section

    Raises:
        RuntimeError: db_config.toml is missing.
        tomllib.TOMLDecodeError: db_config.toml is malformed.
    """
    path = config.DB_CONFIG_FILE
    try:
        with open(path, 'rb') as f:
            return tomllib.load(f).get('database') or {}
    except FileNotFoundError:
        raise RuntimeError(
            f'Database config not found at {path}. Copy db_config.toml from the clinic share into '
            'the data directory (start from db_config.example.toml).')


def load_db_config():
    """Return the [database] table from db_config.toml, or {} if the file is missing or malformed.

    Unlike _load_db_config this tolerates a missing file, so the Settings form can seed from it
    on a fresh install, and a malformed file (logged), so the layout still builds and the
    "not configured" banner shows instead of a crash; re-entering the Database fields rewrites it.

    Returns:
        dict[str, object]: The [database] table, or {}
    """
    try:
        return _load_db_config()
    except RuntimeError:
        return {}
    except tomllib.TOMLDecodeError as e:
        logger.error('could not read %s: %s', config.DB_CONFIG_FILE, e)
        return {}


def save_db_config(host, database, port, username='', password=''):
    """Write the [database] connection to db_config.toml.

    host/database are always written; port/username/password only when set, so a blank port
    connects on DB_PORT_DEFAULT and a blank username/password pair leaves Windows authentication
    in effect. Other keys already in the [database] table (e.g.
    trust_server_certificate) are kept. A malformed existing file is logged and overwritten without
    its other keys.

    Args:
        host (str): Server host
        database (str): Database name
        port (int | str | None): Server port
        username (str): SQL login user
        password (str): SQL login password
    """
    lines = [
        '[database]',
        f'host = {config._fmt(host or "")}',
        f'database = {config._fmt(database or "")}']
    if port:
        lines.append(f'port = {config._fmt(int(port))}')
    if username:
        lines.append(f'username = {config._fmt(username)}')
    if password:
        lines.append(f'password = {config._fmt(password)}')
    managed = ('host', 'port', 'database', 'username', 'password')
    for k, v in load_db_config().items():
        if k not in managed:
            lines.append(f'{k} = {config._fmt(v)}')
    lines.append('')

    # Write to a sibling temp file and swap it in, so a crash mid-write (the file is rewritten on
    # every debounced keystroke) can never leave a truncated db_config.toml behind.
    tmp = config.DB_CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines))
    os.replace(tmp, config.DB_CONFIG_FILE)


def db_configured():
    """True when db_config.toml exists and sets a non-empty [database] host and database

    Used by the dashboard to warn when DB access is not configured yet (e.g. right after a fresh
    install).

    Returns:
        bool: Whether host and database are both set
    """
    cfg = load_db_config()
    return bool(cfg.get('host') and cfg.get('database'))


def db_host():
    """The configured [database] host, or None when unset or unconfigured

    Named in the dashboard's connection-failure banner so a VPN/network outage is distinguishable
    from an unconfigured install.

    Returns:
        str | None: The host
    """
    return load_db_config().get('host') or None


def _build_url():
    """Build the mssql+pyodbc SQLAlchemy connection URL from db_config.toml.

    A username/password selects SQL auth; otherwise Windows authentication is used. The chosen ODBC
    driver is plugged into the query string. trust_server_certificate (default true) skips server
    certificate validation; set it false for a server with a CA-signed certificate.

    Returns:
        sqlalchemy.URL: The connection URL

    Raises:
        RuntimeError: db_config.toml is missing, [database] host or database is unset, or no
            supported SQL Server ODBC driver is installed.
        tomllib.TOMLDecodeError: db_config.toml is malformed.
    """
    cfg = _load_db_config()
    host = cfg.get('host')
    database = cfg.get('database')
    if not host or not database:
        raise RuntimeError('db_config.toml must set [database] host and database.')
    username = cfg.get('username') or None
    password = cfg.get('password') or None
    trust = 'yes' if cfg.get('trust_server_certificate', True) else 'no'
    query = dict(driver=_get_sql_server_driver(), TrustServerCertificate=trust)
    if not (username and password):
        query['trusted_connection'] = 'yes'
    return URL.create(
        'mssql+pyodbc',
        username=username,
        password=password,
        host=host,
        port=cfg.get('port', DB_PORT_DEFAULT),
        database=database,
        query=query)


# Module-level cache so the whole app shares one engine (one connection pool).
_engine = None


def get_engine():
    """Return the shared, lazily-built SQLAlchemy engine for the Mosaiq DB.

    Built once and cached at module level so the whole app shares one connection pool.
    pool_size matches the 8 concurrent pulls per poll so connections stay warm rather than being
    re-opened from overflow each time; pool_pre_ping drops dead connections;
    use_setinputsizes=False avoids a pyodbc binding quirk.

    Returns:
        sqlalchemy.Engine: The cached engine
    """
    global _engine
    if _engine is None:
        _engine = create_engine(
            _build_url(),
            pool_size=8,
            pool_pre_ping=True,
            use_setinputsizes=False)
    return _engine


def configure_logging():
    """Wire the shared 'cra' logger to a file and console handler once.

    Level comes from CRA_LOG_LEVEL (default INFO), the file from CRA_LOG_FILE (default
    logs/cra.log under CRA_DATA_DIR, else beside this package). Idempotent: a no-op once handlers
    are attached. Werkzeug's per-request access lines are silenced; its warnings/errors stay.

    Returns:
        logging.Logger: The configured 'cra' logger
    """
    if logger.handlers:
        return logger
    level = os.environ.get('CRA_LOG_LEVEL', 'INFO').upper()
    data = config.data_dir()
    path = os.environ.get('CRA_LOG_FILE') or os.path.join(data, 'logs', 'cra.log')
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fmt = logging.Formatter('%(asctime)s | %(levelname)-7s | %(message)s', '%Y-%m-%d %H:%M:%S')
    handler = logging.FileHandler(path, encoding='utf-8')
    handler.setFormatter(fmt)
    logger.addHandler(handler)
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    logger.addHandler(console)
    logger.setLevel(level)

    # Silence Werkzeug's per-request access lines (one per Dash poll); keep its warnings/errors.
    logging.getLogger('werkzeug').setLevel(logging.WARNING)
    return logger
