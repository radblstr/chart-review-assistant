# SPDX-FileCopyrightText: 2026 Alex Egan
# SPDX-License-Identifier: Apache-2.0
"""
Dash / AG Grid dashboard and application entry point

- Reads the selectable treatment rooms from the merged config (config.active_machines)
- build_table turns Course/Site objects into dashboard rows, date-windowed.
- create_layout builds the page; register_callbacks wires refresh, filtering, settings,
  feedback, and state.
- show_report renders the per-patient info panel for a selected row.
- create_app builds the Dash app: HTML shell, layout, and callbacks.
- Persists UI state (load_state / save_state) between sessions
"""

import datetime
import json
import logging
import os
import shutil
import tomllib

import dash_ag_grid as dag
import pandas as pd
from dash import Dash, dcc, html, Input, Output, State, ctx, no_update, get_asset_url

from chart_review_assistant import (configure_logging, config, db_configured, db_host, demo_data,
                                     load_db_config, save_db_config, snapshot)
from chart_review_assistant.checks import CHECKS
from chart_review_assistant.query_db import query_db, selection_window, week_span, build_courses

logger = logging.getLogger('cra')

# Demo mode: runs the board against the bundled demo in demo_data/ (a mock Mosaiq DB built from
# the test corpus, its clinic config, and a pinned clock) so the app can be run and explored
# without Mosaiq access or patient data. Settings, DB config and UI state are read from and saved
# to demo_data/ instead of the live files; logs, feedback and bug reports still go to the live data
# dir. Enabled by CRA_DEMO_MODE=1 (demo_launcher.bat) or by setting
# DEMO_MODE_OVERRIDE True here in code; either one turns it on.
DEMO_MODE_OVERRIDE = False
DEMO_MODE = DEMO_MODE_OVERRIDE or os.environ.get('CRA_DEMO_MODE') == '1'

# Redirect the DB engine and config files before anything below reads them (DEFAULT_STATE).
if DEMO_MODE:
    import sqlalchemy
    import chart_review_assistant
    chart_review_assistant._engine = sqlalchemy.create_engine(
        f'sqlite:///{demo_data.DEMO_DB.as_posix()}')
    config.CLINIC_CONFIG_FILE = str(demo_data.DEMO_DIR / 'clinic_config.toml')
    config.USER_CONFIG_FILE = str(demo_data.DEMO_DIR / 'user_config.toml')
    config.DB_CONFIG_FILE = str(demo_data.DEMO_DIR / 'db_config.toml')

# Deid mode: the poll de-identifies the live DB pull in memory and pins the clock to its rewound
# NOW. Enabled by CRA_DEID_MODE=1 (the dev-only launch_deid_mode.bat) or by setting
# DEID_MODE_OVERRIDE True here in code; either one turns it on.
DEID_MODE_OVERRIDE = False
DEID_MODE = DEID_MODE_OVERRIDE or os.environ.get('CRA_DEID_MODE') == '1'

# Settings-dialog sections in display order, mapping each check category to its section title. A
# check joins a section through its category key in CHECKS; this is the sole declaration of the
# sections' titles. UI-only -- no audit reads it.
GROUPS = dict(
    icc='Initial Chart Check',
    wcc='Weekly Chart Check',
    fcc='Final Chart Check',
    cc_charge='CC Charges')

# Selection-window defaults when the state file has none: a 4-week window around today.
WEEKS_DEFAULT = 4
DIRECTION_DEFAULT = 'surrounding'


def active_rooms(cfg):
    """The active machines' room names, skipping a slot with no room yet (board and checklist)."""
    return [m['room'] for m in config.active_machines(cfg) if m.get('room')]


# Demo mode keeps its UI state in demo_data/; deid mode keeps its own file beside the live one, so
# a deid session (weeks-only window, surrogate row keys) never overwrites the live user's state.
STATE_FILE = os.path.join(demo_data.DEMO_DIR if DEMO_MODE else config.data_dir(),
                          'app_state.deid.json' if DEID_MODE else 'app_state.json')
DEFAULT_STATE = dict(
    locations=active_rooms(config.load_config()),
    weeks=WEEKS_DEFAULT,
    direction=DIRECTION_DEFAULT,
    column_state=[],
    selected_key=None,
    report_height=None,
    custom_start=None,
    custom_end=None,
    window_geo=None,
    hide_keys=[],
    hide_applied=False,
)

# Module-global dict of the current DB pull, shared across the callbacks
db_pull = {}

# Dashboard grid columns. Order here determines the rendering order.
COLUMNS = [
    dict(
        key='tx_time',
        label=["Today's", 'Treatment Time'],
        hover="Today's scheduled Tx time."),
    dict(
        key='room',
        label='Room',
        hover=''),
    dict(
        key='name',
        label='Name',
        hover=''),
    dict(
        key='mrn',
        label='MRN',
        hover=''),
    dict(
        key='site_name',
        label='Site',
        hover=''),
    dict(
        key='rx',
        label='Rx',
        hover=''),
    dict(
        key='n_fxs_txd',
        label=['Number of Site', 'Fractions Treated'],
        hover='Number of fractions treated in this site.'),
    dict(
        key='icc_completion_dt',
        label=['Initial Site CC', 'Completion Date'],
        hover='ICC becomes due after the first fraction has been delivered and is considered '
              'missed if no CC note is recorded by the machine\'s missed-threshold fraction '
              '(SBRT courses use the SBRT missed threshold). When enabled in Settings, an '
              'on-time ICC also counts as the course\'s first WCC.'),
    dict(
        key='last_fx_txd_dt',
        label=['Last Treated', 'Fraction Date'],
        hover='Date of most recently treated fraction of course.'),
    dict(
        key='last_wcc_completion_dt',
        label=['Last Weekly CC', 'Completion Date'],
        hover='WCC completion is logged when a CC note is recorded within a 5 fraction '
              'course-wise treated fraction window: [1-6, 6-11, 11-16...] Multiple CC notes '
              'completed within the same window are considered duplicates. Windows with no CC '
              'notes are counted as missed WCCs.'),
    dict(
        key='n_fxs_since_last_cc',
        label=['Number of Course', 'Fxs Treated Since', 'First Fx or Last CC'],
        hover='Number of course fractions treated in the course since the first fraction or last '
              'CC.'),
    dict(
        key='pending_qcls',
        label='Pending QCLs',
        hover='Next pending physics QCL by due date.'),
    dict(
        key='n_wcc_completed',
        label=['Number of Weekly CCs', 'Completed / Expected'],
        hover='Number of WCCs completed during the course vs. the number that should have been '
              'completed up to the current time point.'),
    dict(
        key='fcc_completion_dt',
        label=['Final CC', 'Completion Date'],
        hover='FCC complete based on CC note logged after treatment course is complete but '
              'before 5 business days after the last fraction.'),
    dict(
        key='allowed_cc_charges',
        label=['Number of CCs', 'Charged / Expected'],
        hover='Number of CC charges during the course vs the number allowed up to the current '
              'time point: 1 per 5 fractions and 1 additional if last window is 3 fxs (8, '
              '13, 18...)'),
]

WARNING_STYLE = dict(
    padding='8px 16px',
    backgroundColor='#fff3cd',
    borderBottom='1px solid #ffc107',
    color='#856404',
    fontSize='13px',
    fontWeight='600',
    flexShrink='0',
)
WARNING_HIDDEN = dict(
    display='none',
)

# Full-window backdrop for the Bug/Suggestion and Settings dialogs; toggled between flex (shown)
# and none
FEEDBACK_MODAL_STYLE = dict(
    position='fixed',
    top='0',
    left='0',
    right='0',
    bottom='0',
    backgroundColor='rgba(0,0,0,0.35)',
    display='flex',
    alignItems='center',
    justifyContent='center',
    zIndex='2000',
)

# Location settings tab-bar buttons: one per location, only the active one shows its box below.
# The active tab merges into the panel (white bottom edge cutting the tab-strip baseline) so the
# tab and its box read as one frame -- no second hairline.
TAB_STYLE = dict(fontSize='12px', padding='4px 12px', cursor='pointer', border='1px solid #ccc',
                 borderRadius='4px 4px 0 0', backgroundColor='#eee', marginRight='2px',
                 marginBottom='-1px')
TAB_ACTIVE_STYLE = dict(TAB_STYLE, backgroundColor='#fff', fontWeight='600',
                        borderBottomColor='#fff')

# Style for the instruction hint shown above the note box in the feedback dialogs
FEEDBACK_HINT_STYLE = dict(
    fontSize='12px',
    color='#555',
    backgroundColor='#f0f4f8',
    border='1px solid #cdd7e0',
    borderRadius='4px',
    padding='6px 8px',
    marginBottom='8px',
)

# The feedback/settings dialogs shown and hidden; red status text under a dialog's controls; the
# legend of a titled box (fieldset) in the header and the Settings dialog.
MODAL_SHOWN = dict(FEEDBACK_MODAL_STYLE, display='flex')
MODAL_HIDDEN = dict(FEEDBACK_MODAL_STYLE, display='none')
ERROR_TEXT_STYLE = dict(color='#b00', fontSize='12px')
BOX_TITLE_STYLE = dict(fontSize='11px', fontWeight='600', color='#555', padding='0 6px')


def create_app():
    """Build the Dash app: set the HTML shell, assign the layout, and register callbacks."""
    configure_logging()
    app = Dash(__name__, title='Physics Chart Review Assistant')
    app.index_string = '''<!DOCTYPE html>
<html>
    <head>{%metas%}<title>{%title%}</title>{%favicon%}{%css%}
    <style>
        html, body { height: 100%; margin: 0; padding: 0; overflow: hidden; }
        #react-entry-point, #react-entry-point > div { height: 100%; }
        input[type=number]::-webkit-inner-spin-button,
        input[type=number]::-webkit-outer-spin-button { -webkit-appearance: none; margin: 0; }
        input[type=number] { -moz-appearance: textfield; appearance: textfield; }
        #weeks_dropdown { text-align: center; height: 24px; line-height: 24px; }
        /* Drop the trailing gap after the last room so the box hugs it. */
        #location_checklist label:last-of-type { margin-right: 0 !important; }
        /* Rich cell tooltip: bold headings + bulleted lists, like the report pane. */
        .cr-tooltip { background: #fff; border: 1px solid #bbb; border-radius: 4px;
            padding: 6px 10px; box-shadow: 0 2px 8px rgba(0,0,0,0.15); font-size: 12px;
            color: #333; max-width: 340px; }
        .cr-tooltip b { display: block; margin-top: 4px; }
        .cr-tooltip b:first-child { margin-top: 0; }
        .cr-tooltip ul { margin: 1px 0 4px; padding-left: 18px; }
        .cr-tooltip li { margin-bottom: 2px; }
        /* Header hint tooltip: light box like the grid tooltip, replaces the native title. */
        .cr-hint { position: relative; }
        .cr-hint:hover::after {
            content: attr(data-tip); position: absolute; top: 0; left: calc(100% + 8px);
            background: #fff; border: 1px solid #bbb; border-radius: 4px; padding: 6px 10px;
            box-shadow: 0 2px 8px rgba(0,0,0,0.15); font-size: 12px; color: #333;
            white-space: nowrap; z-index: 1000; pointer-events: none; }
        /* Tighten left/right padding on every header and data cell (default 18px). */
        .ag-theme-alpine { --ag-cell-horizontal-padding: 6px; }
        /* Selection: keep the status color, mark the row with a bar + outline, not a fill. */
        .ag-theme-alpine .ag-row-selected::before { background-color: transparent !important; }
        .ag-theme-alpine .ag-row-selected {
            box-shadow: inset 4px 0 0 0 #1565c0, inset 0 0 0 1px #1565c0;
            font-weight: 600;
        }
    </style>
    </head>
    <body>{%app_entry%}
    <footer>{%config%}{%scripts%}{%renderer%}</footer>
    <script>
    // Drag the report-pane handle to resize the pane vertically.
    document.addEventListener('mousedown', function(e) {
        if (!e.target || e.target.id !== 'report_handle') return;
        e.preventDefault();
        var pane = document.getElementById('report_pane');
        if (!pane) return;
        var startY = e.clientY, startH = pane.getBoundingClientRect().height;
        var cur = startH;
        function mm(ev) {
            cur = Math.max(60, Math.min(startH + (startY - ev.clientY), window.innerHeight * 0.8));
            pane.style.flexBasis = cur + 'px';
        }
        function mu() {
            document.removeEventListener('mousemove', mm);
            document.removeEventListener('mouseup', mu);
            // Persist the new height to app state.
            window.dash_clientside.set_props('report_height', {data: Math.round(cur)});
        }
        document.addEventListener('mousemove', mm);
        document.addEventListener('mouseup', mu);
    });
    // Help panel: toggle on the Help button, close on any click outside it.
    document.addEventListener('click', function(e) {
        var panel = document.getElementById('help_panel');
        var btn = document.getElementById('help_icon');
        if (!panel || !btn) return;
        if (btn.contains(e.target)) {
            panel.style.display = (panel.style.display === 'block') ? 'none' : 'block';
        } else if (!panel.contains(e.target)) {
            panel.style.display = 'none';
        }
    });
    </script>
    </body>
</html>'''

    # Layout is passed as a function so it re-runs (and re-reads saved state) on every page load.
    app.layout = create_layout
    register_callbacks(app)
    return app


def create_layout():
    """Build the page layout returned to Dash.

    Assembles the header (room filter, selection-window controls, refresh, help), the
    AG Grid board, the resizable report pane, and the hidden feedback and settings dialogs,
    restoring saved filters, sort, hide state, and pane height from the state file (STATE_FILE).

    Returns:
        html.Div: The page root.
    """
    font ="'Segoe UI', Roboto, Arial, sans-serif"
    color_green = '#d4f4dd'
    color_orange = '#ffd9b3'
    color_yellow = '#fffde7'
    color_deid = '#ff6a00'

    # Titled box (fieldset) whose legend breaks the top border
    box = dict(
        border='1px solid #bbb',
        borderRadius='4px',
        padding='0 12px 6px',
        margin='0',
        display='flex',
        alignItems='center',
    )
    box_title = BOX_TITLE_STYLE

    # Reformat ISO date strings (yyyy-mm-dd) as m/d/yyyy for display while the column still sorts
    # by the ISO value (chronological as text). Content-detected: only string cells matching the
    # ISO shape are touched, so it applies to every column and non-dates pass through unchanged.
    date_fmt = {'function': r'''
        (function(v) {
            if (typeof v !== 'string') return v;
            return /^\d{4}-\d{2}-\d{2}$/.test(v)
                ? parseInt(v.slice(5, 7)) + '/' + parseInt(v.slice(8, 10)) + '/' + v.slice(0, 4)
                : v;
        })(params.value)
    '''}

    # Pending QCLs cell is "<ISO date> - <type> CC QCL"; reformat just the date part.
    pending_fmt = {'function': r'''
        (function(v) {
            if (!v) return '';
            var i = String(v).indexOf(' ');
            var d = i < 0 ? String(v) : String(v).substring(0, i);
            var r = i < 0 ? '' : String(v).substring(i);
            var p = d.split('-');
            return p.length === 3 ? parseInt(p[1]) + '/' + parseInt(p[2]) + '/' + p[0] + r : v;
        })(params.value)
    '''}

    # Tx Time cell displays the status label; the field itself holds a numeric sort key.
    tx_time_fmt = dict(function='params.data.tx_time_label')

    # Sort pending QCLs by date (ISO-prefixed string), pushing blanks (no pending QCL) to bottom.
    pending_cmp = {'function': r'''
        (!params.valueA && !params.valueB) ? 0
            : !params.valueA ? 1
            : !params.valueB ? -1
            : params.valueA < params.valueB ? -1
            : (params.valueA > params.valueB ? 1 : 0)
    '''}

    # Sort count and 'x/y' columns numerically (numerator, then denominator), pushing blanks and
    # N/A to the bottom.
    frac_cmp = {'function': r'''
        (function(a, b) {
            var pa = String(a == null ? '' : a).split('/').map(Number);
            var pb = String(b == null ? '' : b).split('/').map(Number);
            var na = a == null || a === '' || isNaN(pa[0]);
            var nb = b == null || b === '' || isNaN(pb[0]);
            if (na || nb) return na === nb ? 0 : (na ? 1 : -1);
            return (pa[0] - pb[0]) || ((pa[1] || 0) - (pb[1] || 0));
        })(params.valueA, params.valueB)
    '''}
    frac_keys = ('n_fxs_txd', 'n_fxs_since_last_cc', 'n_wcc_completed', 'allowed_cc_charges')
    column_defs = []
    cfg = config.load_config()
    refresh_seconds = cfg['general']['refresh_seconds']

    # Build one AG Grid column def per configured column, wiring the value formatters, header
    # tooltip, and the severity-aware cell renderer.
    for col in COLUMNS:
        key, label = col['key'], col['label']
        if isinstance(label, (list, tuple)):
            label = '\n'.join(label)
        d = dict(
            field=key,
            headerName=label,
            tooltipField=f'{key}__tip',
            tooltipComponent='HtmlTooltip',
        )
        if col['hover']:
            d['headerTooltip'] = col['hover']
        d['valueFormatter'] = date_fmt
        if key == 'pending_qcls':
            d['valueFormatter'] = pending_fmt
            d['comparator'] = pending_cmp
        elif key == 'tx_time':
            d['valueFormatter'] = tx_time_fmt
        elif key in frac_keys:
            d['comparator'] = frac_cmp
        d['cellStyle'] = dict(textAlign='center')
        d['headerClass'] = 'cr-center-header'
        d['cellRenderer'] = ('CopyCell' if key == 'mrn'
                             else 'PendingCell' if key == 'pending_qcls' else 'SevCell')
        column_defs.append(d)

    # Hide column: a per-row checkbox (built outside the loop so it keeps the native boolean
    # checkbox renderer instead of SevCell). Checked rows are removed from view by the Hide Rows
    # toggle via a clientside external filter.
    hide_col = dict(
        field='hide',
        headerComponent='HideHeader',
        sortable=False,
        editable=True,
        cellDataType='boolean',
        width=60,
        minWidth=52,
        pinned='left',
        resizable=False,
        cellStyle={'display': 'flex', 'alignItems': 'center', 'justifyContent': 'center'},
        headerClass='cr-center-header',
    )
    column_defs = [hide_col] + column_defs

    # Track pending QCLs off: drop the Pending QCLs column. all_column_defs keeps the full set so
    # apply_track_qcls can restore it when the setting changes.
    all_column_defs = column_defs
    column_defs = track_qcls_column_defs(all_column_defs, cfg)
    default_col_def = dict(
        sortable=True,
        resizable=True,
        filter=False,
        minWidth=70,
        autoHeaderHeight=True,
    )
    row_style = dict(
        styleConditions=[
            dict(
                condition="params.data.alert == 'green'",
                style=dict(backgroundColor=color_green),
            ),
            dict(
                condition="params.data.alert == 'orange'",
                style=dict(backgroundColor=color_orange),
            ),
            dict(
                condition="params.data.alert == 'yellow'",
                style=dict(backgroundColor=color_yellow),
            ),
        ],
    )
    grid_options = dict(
        tooltipShowDelay=0,
        tooltipHideDelay=3600000,
        rowSelection='single',
        suppressCellFocus=True,

        # Let the mouse select and copy cell text (AG Grid disables this by default);
        # ensureDomOrder keeps a drag across rows selecting in visual order.
        enableCellTextSelection=True,
        ensureDomOrder=True,
        autoSizeStrategy=dict(type='fitCellContents'),
        getRowId=dict(function='params.data.__rowkey'),
    )

    # Help drop-down (toggled by the '?' icon in the utility cluster)
    help_panel_style = dict(
        position='absolute',
        top='30px',
        right='0',
        width='380px',
        backgroundColor='#fff',
        border='1px solid #bbb',
        borderRadius='4px',
        boxShadow='0 4px 14px rgba(0,0,0,0.18)',
        padding='10px 14px',
        zIndex='1000',
        fontSize='12px',
        color='#333',
        lineHeight='1.5',
        textAlign='left',
        whiteSpace='normal',
    )
    help_sections = [
        ('Refresh', ['Refreshing board updates every value in the board from the current Mosaiq '
                     'DB state. Board refreshes automatically every ',
                     html.Span(str(refresh_seconds), id='help_refresh_seconds'),
                     ' seconds in the background; the Refresh button reloads immediately.']),
        ('Sorting', 'Click a column header to sort; click again to reverse, a third time to '
                    'clear. Hold Shift and click more headers to sort by several at once -- the '
                    'numbers by the arrows show priority (1 is primary). Sort choices are '
                    'remembered.'),
    ]
    s = load_state()

    # Reconcile the two date-range controls on load. If a saved custom range
    # differs from the weeks+direction window, the custom range wins and the
    # weeks field is blanked; otherwise weeks+direction wins and the custom
    # picker mirrors its window so the two always agree on start.
    win_start, win_end = week_span(board_today(), max(1, s['weeks'] or WEEKS_DEFAULT),
                                   s['direction'])
    win_start, win_end = win_start.isoformat(), win_end.isoformat()
    cs, ce = s.get('custom_start'), s.get('custom_end')

    # Deid mode stays on the weeks/direction window so the rewound header cannot fall out of sync
    # with a stale custom range saved in real (unshifted) dates.
    if not DEID_MODE and cs and ce and (cs, ce) != (win_start, win_end):
        weeks_value, picker_start, picker_end = None, cs, ce
    else:
        weeks_value = (s['weeks'] or WEEKS_DEFAULT) if DEID_MODE else s['weeks']
        picker_start, picker_end = win_start, win_end

    # Restore the saved sort via AG Grid initialState (applied once at grid
    # creation, unlike the columnState prop which autoSize would override).
    sort_model = [
        dict(
            colId=c['colId'],
            sort=c['sort'],
            sortIndex=c.get('sortIndex'),
        )
        for c in (s.get('column_state') or []) if c.get('sort')
    ]
    grid_options = dict(grid_options, initialState=dict(sort=dict(sortModel=sort_model)))

    # Report pane height: saved px if present, else 30% of the viewport
    saved_h = s.get('report_height')
    pane_basis = f'{int(saved_h)}px' if saved_h else '30vh'
    return html.Div([
        dcc.Store(id='report_height', data=saved_h),

        # Header bar
        html.Div([

            # Brand zone
            html.Div([
                html.Img(src=get_asset_url('icons/icon_header.png'), style=dict(
                    height='48px',
                    marginRight='8px',
                )),
                html.H2('Physics Chart Review Assistant',
                        style=dict(
                            margin='0',
                            fontSize='20px',
                            whiteSpace='nowrap',
                        )),
            ], style=dict(display='flex', alignItems='center', flexShrink='0')),

            # Filters zone
            html.Div([
                html.Fieldset([
                    html.Legend('Treatment Rooms', style=box_title),
                    dcc.Checklist(
                        id='location_checklist',
                        options=[dict(label=f'  {m["label"]}', value=m['room'])
                                 for m in config.active_machines(cfg) if m.get('room')],
                        value=s['locations'],
                        inline=True,
                        inputStyle=dict(
                            marginRight='4px',
                            accentColor='#2196f3',
                        ),
                        labelStyle=dict(
                            marginRight='10px',
                            cursor='pointer',
                            userSelect='none',
                        ),
                    ),
                ], style=dict(box, flexShrink='0')),
                html.Fieldset([
                    html.Legend('Patient Selection Window', style=box_title),
                    html.Div([
                        dcc.Dropdown(
                            id='direction_dropdown',
                            options=[
                                dict(label='Past', value='past'),
                                dict(label='Surrounding', value='surrounding'),
                                dict(label='Next', value='next'),
                            ],
                            value=s['direction'],
                            clearable=False,
                            searchable=False,
                            style=dict(
                                width='120px',
                                fontSize='13px',
                            ),
                        ),
                        html.Div([
                            dcc.Input(
                                id='weeks_dropdown',
                                type='number',
                                min=1,
                                step=1,
                                debounce=True,
                                value=weeks_value,
                                style=dict(
                                    width='46px',
                                    fontSize='13px',
                                    padding='0 4px',
                                    textAlign='center',
                                    boxSizing='border-box',
                                ),
                            ),
                            html.Span('wks', style=dict(
                                fontSize='12px',
                                color='#555',
                                whiteSpace='nowrap',
                            )),
                        ], style=dict(
                            display='flex',
                            alignItems='center',
                            gap='4px',
                        )),
                        html.Span('or',
                                  style=dict(
                                      fontSize='12px',
                                      color='#888',
                                      whiteSpace='nowrap',
                                  )),
                        dcc.DatePickerRange(
                            id='custom_date_range',
                            start_date=picker_start,
                            end_date=picker_end,
                            display_format='YYYY-MM-DD',
                            style=dict(
                                fontSize='13px',
                            ),
                        ),
                    ], className='cr-hint',
                        style=dict(
                            display='flex',
                            alignItems='center',
                            gap='8px',
                        ),
                        **{'data-tip': 'Any patient with Tx in time window'}),
                ], style=dict(box, flexShrink='0')),
            ], style=dict(
                display='flex',
                alignItems='stretch',
                gap='12px',
                flexShrink='0',
            )),

            # Status zone
            html.Fieldset([
                html.Legend('Refresh status', style=box_title),
                html.Div([
                    html.Button('Refresh', id='refresh_btn', n_clicks=0, style=dict(
                        fontSize='12px',
                        padding='3px 10px',
                        cursor='pointer',
                    )),
                    html.Span(id='last_updated', style=dict(
                        fontSize='12px',
                        color='#666',
                        whiteSpace='nowrap',
                    )),
                ], style=dict(
                    display='flex',
                    alignItems='center',
                    gap='8px',
                )),
            ], style=dict(box, flexShrink='0')),

            # Deid/demo mode indicator: persistent, bright-orange, sits in the gap before the
            # utility cluster (which is pushed right by margin-left auto).
            *([html.Div([
                html.Div('Demo Mode' if DEMO_MODE else 'De-identify Mode', style=dict(
                    fontSize='13px',
                    fontWeight='700',
                    color=color_deid,
                )),
                html.Div(
                    (f'Demo data. Clock pinned to {demo_data.DEMO_NOW:%Y-%m-%d}.'
                     if DEMO_MODE else
                     'Patient data de-identified. Dates rewound by a random number of weeks.'),
                    style=dict(fontSize='10px', color=color_deid)),
            ], style=dict(
                display='flex',
                flexDirection='column',
                justifyContent='center',
                flexShrink='0',
            ))] if DEID_MODE or DEMO_MODE else []),

            # Hidden proxy: the visible 'Clear Checks' button lives in the Hide column header
            # (HideHeader) and clicks this to run the clear-checks callback.
            html.Button(id='hide_clear', n_clicks=0, style=dict(display='none')),

            # Utility cluster (right-aligned): Bug Report and Suggestion Box stay as text buttons
            # so feedback is always visible; Settings and Help are compact icons.
            html.Div([
                html.Fieldset([
                    html.Legend('Feedback', style=box_title),
                    html.Div([
                        html.Button('Bug report', id='bug_btn', n_clicks=0,
                                    title='Report a bug', style=dict(
                                fontSize='12px',
                                padding='3px 10px',
                                cursor='pointer',
                            )),
                        html.Button('suggestion box', id='suggestion_btn', n_clicks=0,
                                    title='Suggest an improvement', style=dict(
                                fontSize='12px',
                                padding='3px 10px',
                                cursor='pointer',
                            )),
                    ], style=dict(
                        display='flex',
                        alignItems='center',
                        gap='8px',
                    )),
                ], style=dict(box, flexShrink='0', alignSelf='stretch')),
                html.Button('⚙', id='settings_btn', n_clicks=0,
                            title='Machine and check settings', style=dict(
                        fontSize='16px',
                        padding='1px 8px',
                        lineHeight='1',
                        cursor='pointer',
                    )),

                # Help (drop-down toggled by the Help button; closes on outside click via JS)
                html.Div([
                    html.Button('?', id='help_icon', n_clicks=0, title='Dashboard Features',
                                style=dict(
                                    fontSize='15px',
                                    fontWeight='700',
                                    padding='1px 9px',
                                    lineHeight='1',
                                    cursor='pointer',
                                )),
                    html.Div([
                        html.Div('Dashboard Features', style=dict(
                            fontWeight='700',
                            fontSize='13px',
                            marginBottom='6px',
                        )),
                        *[html.Div([html.Span(f'{t}: ', style=dict(fontWeight='600')),
                                    html.Span(txt)],
                                   style=dict(marginBottom='6px')) for t, txt in help_sections],

                        # Legend folded into the Help popover
                        html.Div('Legend', style=dict(
                            fontWeight='700',
                            fontSize='13px',
                            margin='10px 0 6px',
                            borderTop='1px solid #eee',
                            paddingTop='8px',
                        )),
                        html.Div([
                            html.Span('Selected', style=dict(
                                padding='1px 8px',
                                fontWeight='600',
                                boxShadow='inset 4px 0 0 0 #1565c0, inset 0 0 0 1px #1565c0',
                            )),
                            html.Span('No Warnings or Errors', style=dict(
                                backgroundColor=color_green,
                                padding='1px 8px',
                                borderRadius='3px',
                                border='1px solid #b0d8ba',
                            )),
                            html.Span('Course/Site has Errors', style=dict(
                                backgroundColor=color_orange,
                                padding='1px 8px',
                                borderRadius='3px',
                                border='1px solid #e8b88a',
                            )),
                            html.Span('Course/Site has Warnings', style=dict(
                                backgroundColor=color_yellow,
                                padding='1px 8px',
                                borderRadius='3px',
                                border='1px solid #e0d87a',
                            )),
                        ], style=dict(
                            display='flex',
                            flexWrap='wrap',
                            alignItems='center',
                            gap='6px',
                            fontSize='11px',
                            color='#444',
                        )),
                    ], id='help_panel', style=dict(help_panel_style, display='none')),
                ], style=dict(
                    position='relative',
                    flexShrink='0',
                )),
            ], style=dict(
                display='flex',
                alignItems='center',
                gap='10px',
                marginLeft='auto',
                flexShrink='0',
            )),
        ], style=dict(
            display='flex',
            alignItems='stretch',
            gap='24px',
            padding='8px 16px',
            borderBottom='2px solid #ccc',
            backgroundColor='#f5f5f5',
            flexShrink='0',
        )),

        # Bug and Suggestion dialogs: hidden overlays toggled by the header buttons; each Submit
        # appends its note (and any attached files) to the feedback log (see save_feedback).
        build_feedback_modal(
            'bug_modal', 'Report a Bug', 'bug_text', 'bug_status', 'bug_upload', 'bug_attach',
            'bug_cancel', 'bug_submit',
            hint='If the bug is associated with a site row, please select that row in the '
                 'dashboard before submission so it is identified in the captured board state. '
                 'A screenshot of the issue is often the easiest way to describe it.'),
        build_feedback_modal(
            'suggestion_modal', 'Suggestion Box', 'suggestion_text', 'suggestion_status',
            'suggestion_upload', 'suggestion_attach', 'suggestion_cancel', 'suggestion_submit',
            hint='A screenshot is often the easiest way to describe your suggestion.'),

        # Settings dialog: hidden overlay toggled by the header Settings button; each non-database
        # change writes user_config.toml and bumps settings_applied so the board rebuilds with the
        # new filters.
        build_settings_modal(cfg),
        dcc.Store(id='settings_applied', data=0, storage_type='memory'),
        dcc.Store(id='all_column_defs', data=all_column_defs, storage_type='memory'),
        html.Div(id='db_warning', style=WARNING_HIDDEN),
        html.Div(id='state_sink', style=dict(
            display='none',
        )),

        # Hide feature: hide_keys holds the checked rows' __rowkey. The Hide/Unhide toggle lives in
        # the Hide column header (HideHeader) and tracks its applied state via window.__craHide.
        # Seeded from saved state so hidden rows persist across sessions.
        dcc.Store(id='hide_keys', data=s.get('hide_keys') or [], storage_type='memory'),

        # Whether the Hide filter is applied (rows removed from view); persisted like hide_keys
        dcc.Store(id='hide_applied', data=bool(s.get('hide_applied')), storage_type='memory'),
        html.Div(id='hide_sink', style=dict(display='none')),
        dcc.Interval(id='interval', interval=refresh_seconds * 1000, n_intervals=0),

        # Table
        html.Div([
            dag.AgGrid(
                id='table',
                columnDefs=column_defs,
                rowData=[],
                defaultColDef=default_col_def,
                getRowStyle=row_style,
                dashGridOptions=grid_options,
                columnSize='autoSize',
                className='ag-theme-alpine',
                style=dict(
                    height='100%',
                    width='100%',
                ),
            ),
        ], id='table_container', style=dict(
            flex='1 1 auto',
            minHeight='0',
        )),

        # Report pane (drag the handle to resize vertically)
        html.Div(id='report_handle', style=dict(
            flex='0 0 6px',
            cursor='ns-resize',
            backgroundColor='#ccc',
            borderTop='1px solid #999',
        )),
        html.Div(id='report_pane', style=dict(
            flex=f'0 0 {pane_basis}',
            overflowY='auto',
            backgroundColor='#fafafa',
            padding='8px 16px',
        )),
    ], style=dict(
        fontFamily=font,
        height='100vh',
        display='flex',
        flexDirection='column',
    ))


def build_feedback_modal(modal_id, title, text_id, status_id, upload_id, attach_id, cancel_id,
                         submit_id, hint=None):
    """Build a hidden feedback dialog overlay (bug or suggestion).

    Any file type may be attached; staged files are listed under the upload control and saved with
    the note on Submit (see save_feedback).

    Args:
        modal_id (str): Overlay component id.
        title (str): Dialog title.
        text_id (str): Note textarea id.
        status_id (str): Status message div id.
        upload_id (str): dcc.Upload id.
        attach_id (str): Staged-file list div id.
        cancel_id (str): Cancel button id.
        submit_id (str): Submit button id.
        hint (str | None): Shown above the note box, if given.

    Returns:
        html.Div: The hidden dialog overlay.
    """
    body = [html.Div(title, style=dict(fontWeight='700', fontSize='15px', marginBottom='8px'))]
    if hint:
        body.append(html.Div(hint, style=FEEDBACK_HINT_STYLE))
    body += [
        dcc.Textarea(
            id=text_id,
            placeholder='Describe the bug or suggestion...',
            style=dict(width='100%', height='120px', fontSize='13px', fontFamily='inherit',
                       boxSizing='border-box', resize='vertical'),
        ),
        dcc.Upload(
            id=upload_id,
            children=html.Div('Attach a screenshot or file (optional)'),
            multiple=True,
            style=dict(fontSize='12px', color='#555', border='1px dashed #cdd7e0',
                       borderRadius='4px', padding='6px 8px', marginTop='6px',
                       textAlign='center', cursor='pointer'),
        ),
        html.Div(id=attach_id, style=dict(marginTop='4px')),
        html.Div(id=status_id, style=dict(minHeight='16px', marginTop='6px')),
        html.Div([
            html.Button('Cancel', id=cancel_id, n_clicks=0,
                        style=dict(fontSize='12px', padding='4px 12px', cursor='pointer')),
            html.Button('Submit', id=submit_id, n_clicks=0,
                        style=dict(fontSize='12px', padding='4px 12px', cursor='pointer',
                                   fontWeight='600')),
        ], style=dict(display='flex', justifyContent='flex-end', gap='8px', marginTop='10px')),
    ]
    return html.Div([
        html.Div(body, style=dict(backgroundColor='#fff', borderRadius='6px',
                                  boxShadow='0 6px 24px rgba(0,0,0,0.25)', padding='16px 18px',
                                  width='440px', maxWidth='90vw')),
    ], id=modal_id, style=dict(FEEDBACK_MODAL_STYLE, display='none'))


def build_settings_modal(cfg):
    """Build the Settings dialog overlay from the merged config.

    One fieldset per machine (up to config.MAX_MACHINES, behind a tab bar with Add/Remove/Restore)
    carries an editable label/room, a checklist of every toggleable check, the ICC and SBRT
    threshold inputs and the ICC-counts-as-WCC toggle; General and Database fieldsets hold the app-wide options. Values are seeded from cfg;
    each change is written to user_config.toml (database fields to db_config.toml).

    Args:
        cfg (dict[str, object]): Merged config from config.load_config.

    Returns:
        html.Div: The hidden settings-modal overlay.
    """
    box = dict(border='1px solid #ccc', borderRadius='4px', padding='0 10px 8px',
               margin='0 0 10px')

    # Location panel: no top border so it merges with the active tab above into one frame.
    loc_box = dict(box, borderTop='none', borderRadius='0 0 4px 4px', padding='8px 10px')
    box_title = BOX_TITLE_STYLE
    section = dict(border='1px solid #e0e0e0', borderRadius='4px', padding='0 8px 6px',
                   margin='0 0 8px')
    section_title = dict(fontSize='11px', fontWeight='600', color='#777', padding='0 5px')
    label_style = dict(fontSize='12px', color='#555', marginRight='4px')
    input_style = dict(fontSize='13px', height='24px', padding='0 6px', boxSizing='border-box')
    btn_style = dict(fontSize='12px', padding='3px 10px', cursor='pointer', borderRadius='4px',
                     border='1px solid #bbb', backgroundColor='#f5f5f5')
    general = cfg['general']
    machines = cfg.get('machines') or []
    flags = _slot_flags(machines)
    first = flags.index(True)
    machine_boxes = []
    check_specs = machine_check_specs()

    # Shared checklist styling for the single-row checkboxes (ICC per-finding, WCC toggle, Track
    # QCLs toggle).
    row_label_style = dict(display='flex', alignItems='center', margin='0', fontSize='12px',
                           cursor='pointer')
    check_input_style = dict(marginRight='6px', accentColor='#2196f3')

    # Finding level shown after each check label
    level_names = dict(info='Notification', warning='Warning', error='Error')
    for i in range(config.MAX_MACHINES):
        m = machines[i] if i < len(machines) else {}
        (m_label, m_room, checks, icc_missed_fx, sbrt_dose,
         sbrt_missed, icc_wcc_value) = baseline_values(m)
        checks_map = dict(zip([suffix for suffix, _ in check_specs], checks))
        sections = []
        for gid, name in GROUPS.items():
            opts = [dict(label=f"  {c['label']} ({level_names[c['level']]})", value=cid)
                    for cid, c in CHECKS[gid].items()]
            if gid == 'icc':
                row = dict(display='flex', alignItems='center', height='28px', margin='0',
                           fontSize='12px')
                check_opts = {o['value']: o for o in opts}
                icc_box = dcc.Input(id=f'machine_icc_missed_fx_{i}', type='number', min=1, step=1,
                                    debounce=True, value=icc_missed_fx,
                                    style=dict(input_style, width='52px'))
                sbrt_dose_box = dcc.Input(id=f'machine_sbrt_fx_dose_{i}', type='number', min=1,
                                          step=1, debounce=True, value=sbrt_dose,
                                          style=dict(input_style, width='64px'))
                sbrt_missed_box = dcc.Input(id=f'machine_sbrt_missed_fx_{i}', type='number', min=1,
                                            step=1, debounce=True, value=sbrt_missed,
                                            style=dict(input_style, width='52px'))
                extras = dict(
                    icc_due=[html.Label('Missed fraction',
                                        style=dict(label_style, marginLeft='16px')), icc_box],
                    sbrt_icc_due=[
                        html.Label('SBRT fx dose (cGy)',
                                   style=dict(label_style, marginLeft='16px')), sbrt_dose_box,
                        html.Label('SBRT missed fx', style=dict(label_style, marginLeft='10px')),
                        sbrt_missed_box],
                    icc_missed=[])
                icc_rows = []
                for cid, o in check_opts.items():
                    checkbox = dcc.Checklist(
                        id=f'machine_checks_{i}_{gid}_{cid}',
                        options=[o],
                        value=checks_map[f'icc_{cid}'],
                        labelStyle=row_label_style,
                        inputStyle=check_input_style,
                        style=dict(width='200px'),
                    )
                    icc_rows.append(html.Div([checkbox, *extras[cid]], style=row))
                body = html.Div(icc_rows)
            else:
                body = dcc.Checklist(
                    id=f'machine_checks_{i}_{gid}',
                    options=opts,
                    value=checks_map[gid],
                    labelStyle=dict(display='block', fontSize='12px', cursor='pointer'),
                    inputStyle=check_input_style,
                )
                if gid == 'wcc':
                    icc_wcc = dcc.Checklist(
                        id=f'machine_icc_wcc_{i}',
                        options=[dict(
                            label='  Count an ICC as a WCC.',
                            value='on')],
                        value=icc_wcc_value,
                        labelStyle=row_label_style,
                        inputStyle=check_input_style,
                        style=dict(marginTop='4px'),
                    )
                    body = html.Div([body, icc_wcc])
            sections.append(html.Fieldset([
                html.Legend(name, style=section_title),
                body,
            ], style=section))
        machine_boxes.append(html.Div(html.Fieldset([
            html.Div([
                html.Label('Label', style=label_style),
                dcc.Input(id=f'machine_label_{i}', type='text', value=m_label,
                          debounce=True, style=dict(input_style, width='120px')),
                html.Label('Mosaiq Location', style=dict(label_style, marginLeft='10px')),
                dcc.Input(id=f'machine_room_{i}', type='text', value=m_room,
                          debounce=True, style=dict(input_style, width='120px')),
                html.Button('Restore Defaults', id=f'restore_defaults_{i}',
                            n_clicks=0, style=dict(btn_style, marginLeft='10px')),
            ], style=dict(display='flex', alignItems='center', margin='0 0 8px')),
            *sections,
        ], style=loc_box), id=f'location_box_{i}', style=_box_vis(i, first, flags)))
    tabbar = html.Div(
        [html.Button(_tab_label(machines[i] if i < len(machines) else {}, i),
                     id=f'tab_btn_{i}', n_clicks=0, style=_tab_style(i, first, flags))
         for i in range(config.MAX_MACHINES)],
        style=dict(display='flex', flexWrap='wrap', borderBottom='1px solid #ccc'))
    general_box = html.Fieldset([
        html.Legend('General', style=box_title),
        html.Div([
            html.Label('Auto-refresh (seconds)', style=label_style),
            dcc.Input(id='cfg_refresh_seconds', type='number', min=config.REFRESH_SECONDS_MIN,
                      max=config.REFRESH_SECONDS_MAX, step=5, debounce=True,
                      value=general.get('refresh_seconds', config.REFRESH_SECONDS_DEFAULT),
                      style=dict(input_style, width='64px')),
        ], style=dict(display='flex', alignItems='center', margin='6px 0 8px')),
        dcc.Checklist(
            id='cfg_track_qcls',
            options=[dict(label='  Track pending QCLs', value='on')],
            value=(['on'] if general.get('track_cc_qcls', config.TRACK_CC_QCLS_DEFAULT)
                   else []),
            labelStyle=row_label_style,
            inputStyle=check_input_style,
            style=dict(margin='0 0 8px'),
        ),
    ], style=box)
    db = load_db_config()
    db_box = html.Fieldset([
        html.Legend('Database (applies on next launch)', style=box_title),
        html.Div([
            html.Label('Host', style=label_style),
            dcc.Input(id='cfg_db_host', type='text', debounce=True,
                      value=db.get('host', ''), style=dict(input_style, width='160px')),
            html.Label('Database', style=dict(label_style, marginLeft='10px')),
            dcc.Input(id='cfg_db_database', type='text', debounce=True,
                      value=db.get('database', ''), style=dict(input_style, width='140px')),
            html.Label('Port', style=dict(label_style, marginLeft='10px')),
            dcc.Input(id='cfg_db_port', type='number', min=1, step=1, debounce=True,
                      value=db.get('port'), style=dict(input_style, width='72px')),
        ], style=dict(display='flex', alignItems='center', margin='6px 0 6px')),
        html.Div([
            html.Label('Username (blank = Windows auth)', style=label_style),
            dcc.Input(id='cfg_db_username', type='text', debounce=True,
                      value=db.get('username', ''), style=dict(input_style, width='140px')),
            html.Label('Password', style=dict(label_style, marginLeft='10px')),
            dcc.Input(id='cfg_db_password', type='password', debounce=True,
                      value=db.get('password', ''), style=dict(input_style, width='140px')),
        ], style=dict(display='flex', alignItems='center', margin='0 0 4px')),
        html.Div('Password is saved as plain text in db_config.toml. Prefer Windows auth.',
                 style=dict(color='red', fontSize='11px', margin='0 0 4px')),
        html.Div(id='db_settings_status', style=dict(minHeight='16px')),
    ], style=box)
    return html.Div([
        # Full-overlay click target behind the panel: clicking outside the panel closes settings.
        html.Div(id='settings_backdrop', n_clicks=0, style=dict(
            position='absolute', top='0', left='0', right='0', bottom='0')),
        html.Div([
            html.Div('Settings',
                     style=dict(fontWeight='700', fontSize='15px', marginBottom='8px')),
            general_box,
            db_box,
            html.Div([
                html.Button('Add Location', id='add_location_btn', n_clicks=0, style=btn_style),
                html.Button('Remove Location', id='remove_location_btn', n_clicks=0,
                            style=dict(btn_style, marginLeft='8px')),
                html.Button('Restore Default Locations', id='restore_locations_btn', n_clicks=0,
                            style=dict(btn_style, marginLeft='8px')),
            ], style=dict(display='flex', alignItems='center', margin='0 0 10px')),
            dcc.Store(id='slot_active_store', data=flags, storage_type='memory'),
            dcc.Store(id='active_tab_store', data=first, storage_type='memory'),
            tabbar,
            *machine_boxes,
            html.Div(id='settings_status', style=dict(minHeight='16px', marginTop='6px')),

            # Save Clinic Config: write the effective settings (clinic baseline + this user's
            # overrides) as a complete clinic config, to a file picked in a save dialog, to hand
            # out as a new baseline. Load DB/Clinic Config: copy a picked file into the data dir.
            html.Div([
                html.Button('Save Clinic Config', id='save_clinic_btn', n_clicks=0,
                            style=btn_style),
                html.Button('Load DB Config', id='load_db_btn', n_clicks=0,
                            style=dict(btn_style, marginLeft='8px')),
                html.Button('Load Clinic Config', id='load_clinic_btn', n_clicks=0,
                            style=dict(btn_style, marginLeft='8px')),
                html.Span(id='config_file_status',
                          style=dict(fontSize='12px', color='#555', marginLeft='10px')),
            ], style=dict(display='flex', alignItems='center', marginTop='10px')),
        ], style=dict(
            backgroundColor='#fff',
            borderRadius='6px',
            boxShadow='0 6px 24px rgba(0,0,0,0.25)',
            padding='16px 18px',
            width='620px',
            maxWidth='92vw',
            maxHeight='85vh',
            overflowY='auto',
            position='relative',
            zIndex='1',
        )),
    ], id='settings_modal', style=dict(FEEDBACK_MODAL_STYLE, display='none'))


def register_callbacks(app):
    """Register the app's callbacks.

    Covers board refresh, window sync, state save, report pane, hide feature, settings, and
    feedback dialogs.
    """

    @app.callback(
        Output('table', 'rowData'),
        Output('table', 'selectedRows'),
        Output('last_updated', 'children'),
        Output('db_warning', 'children'),
        Output('db_warning', 'style'),
        Input('refresh_btn', 'n_clicks'),
        Input('location_checklist', 'value'),
        Input('weeks_dropdown', 'value'),
        Input('direction_dropdown', 'value'),
        Input('custom_date_range', 'start_date'),
        Input('custom_date_range', 'end_date'),
        State('hide_keys', 'data'),
        running=[
            (Output('table_container', 'style'),
             dict(
                 flex='1 1 auto',
                 minHeight='0',
                 opacity='0.35',
                 pointerEvents='none',
             ),
             dict(
                 flex='1 1 auto',
                 minHeight='0',
             )),
        ],
    )
    def update_table(refresh_btn, location_checklist, weeks_dropdown, direction_dropdown,
                     custom_date_range_start, custom_date_range_end, hide_keys):
        """Rebuild the board with a loading overlay when filters change or Refresh is clicked."""
        return rebuild_rows(location_checklist, weeks_dropdown, direction_dropdown,
                            custom_date_range_start, custom_date_range_end, hide_keys)

    # Settings changes affect how findings are derived (thresholds, via courses_from_pull) and
    # bucketed, not the underlying DB data, so the board is re-derived from the last pull in the
    # background -- no DB query, no loading overlay.
    @app.callback(
        Output('table', 'rowData', allow_duplicate=True),
        Output('table', 'selectedRows', allow_duplicate=True),
        Input('settings_applied', 'data'),
        State('location_checklist', 'value'),
        State('weeks_dropdown', 'value'),
        State('direction_dropdown', 'value'),
        State('custom_date_range', 'start_date'),
        State('custom_date_range', 'end_date'),
        State('hide_keys', 'data'),
        prevent_initial_call=True,
    )
    def apply_settings(settings_applied, location_checklist, weeks_dropdown, direction_dropdown,
                       custom_date_range_start, custom_date_range_end, hide_keys):
        """Re-derive the board from the last pull when settings change (soft refresh, no query)."""
        row_data, selected, *_ = rebuild_rows(location_checklist, weeks_dropdown,
                                              direction_dropdown, custom_date_range_start,
                                              custom_date_range_end, hide_keys, soft=True)
        return row_data, selected

    @app.callback(
        Output('table', 'columnDefs'),
        Input('settings_applied', 'data'),
        State('all_column_defs', 'data'),
        prevent_initial_call=True,
    )
    def apply_track_qcls(settings_applied, all_column_defs):
        """Add or drop the Pending QCLs column per the general track_cc_qcls setting."""
        return track_qcls_column_defs(all_column_defs, config.load_config())

    @app.callback(
        Output('interval', 'interval'),
        Output('help_refresh_seconds', 'children'),
        Input('settings_applied', 'data'),
        prevent_initial_call=True,
    )
    def apply_refresh_seconds(settings_applied):
        """Apply general refresh_seconds to the auto-refresh interval (ms) and the Help text."""
        refresh_seconds = config.load_config()['general']['refresh_seconds']
        return refresh_seconds * 1000, str(refresh_seconds)

    @app.callback(
        Output('location_checklist', 'options'),
        Output('location_checklist', 'value'),
        Input('settings_applied', 'data'),
        State('location_checklist', 'options'),
        State('location_checklist', 'value'),
        prevent_initial_call=True,
    )
    def apply_locations(settings_applied, options, value):
        """Rebuild the Treatment Rooms checklist from the active locations.

        Removed rooms are dropped from the selection; newly added rooms start selected.
        """
        machines = config.active_machines(config.load_config())
        old_rooms = {o['value'] for o in options or []}
        new_options = [dict(label=f'  {m["label"]}', value=m['room'])
                       for m in machines if m.get('room')]
        rooms = [o['value'] for o in new_options]
        new_value = [r for r in rooms if r in (value or []) or r not in old_rooms]
        if new_options == options and set(new_value) == set(value or []):
            return no_update, no_update
        return new_options, new_value

    # Auto-refresh runs in the background (no loading overlay), so periodic updates never block
    # the board. Filters are read as State so it only fires on the interval tick.
    @app.callback(
        Output('table', 'rowData', allow_duplicate=True),
        Output('table', 'selectedRows', allow_duplicate=True),
        Output('last_updated', 'children', allow_duplicate=True),
        Output('db_warning', 'children', allow_duplicate=True),
        Output('db_warning', 'style', allow_duplicate=True),
        Input('interval', 'n_intervals'),
        State('location_checklist', 'value'),
        State('weeks_dropdown', 'value'),
        State('direction_dropdown', 'value'),
        State('custom_date_range', 'start_date'),
        State('custom_date_range', 'end_date'),
        State('hide_keys', 'data'),
        prevent_initial_call=True,
    )
    def auto_refresh(n_intervals, location_checklist, weeks_dropdown, direction_dropdown,
                     custom_date_range_start, custom_date_range_end, hide_keys):
        """Rebuild the board on the interval tick in the background (no loading overlay)."""
        return rebuild_rows(location_checklist, weeks_dropdown, direction_dropdown,
                            custom_date_range_start, custom_date_range_end, hide_keys)

    @app.callback(
        Output('custom_date_range', 'start_date'),
        Output('custom_date_range', 'end_date'),
        Output('weeks_dropdown', 'value'),
        Input('weeks_dropdown', 'value'),
        Input('direction_dropdown', 'value'),
        Input('custom_date_range', 'start_date'),
        Input('custom_date_range', 'end_date'),
        prevent_initial_call=True,
    )
    def sync_window(weeks_dropdown, direction_dropdown, custom_start, custom_end):
        """Keep the weeks box and the custom date picker in agreement.

        A weeks/direction change mirrors the picker to that window; a real custom range blanks the
        weeks box; clearing the picker restores the default weeks window.
        """
        direction = direction_dropdown or DIRECTION_DEFAULT
        today = board_today()
        if ctx.triggered_id in ('weeks_dropdown', 'direction_dropdown'):

            # Weeks mode: mirror the picker to the weeks window (unless the box was blanked).
            if weeks_dropdown is None:
                return no_update, no_update, no_update

            # The box's min=1 is browser-side only; a typed 0 or negative must mirror the same
            # window load_rows uses (0 -> the default, negative -> 1 week), or the picker and the
            # board disagree.
            start, end = week_span(today, max(1, weeks_dropdown or WEEKS_DEFAULT), direction)
            return start.isoformat(), end.isoformat(), no_update

        # The date picker changed.
        if not custom_start or not custom_end:

            # Cleared with the X -> return to the default weeks window.
            start, end = week_span(today, WEEKS_DEFAULT, direction)
            return start.isoformat(), end.isoformat(), WEEKS_DEFAULT
        if weeks_dropdown is not None:
            start, end = week_span(today, max(1, weeks_dropdown or WEEKS_DEFAULT), direction)
            if (custom_start, custom_end) == (start.isoformat(), end.isoformat()):

                # Our own mirror of the weeks window -> leave the weeks box alone.
                return no_update, no_update, no_update

        # A real custom range -> wipe the weeks box.
        return no_update, no_update, None

    @app.callback(
        Output('state_sink', 'children'),
        Input('location_checklist', 'value'),
        Input('weeks_dropdown', 'value'),
        Input('direction_dropdown', 'value'),
        Input('custom_date_range', 'start_date'),
        Input('custom_date_range', 'end_date'),
        Input('table', 'columnState'),
        Input('table', 'selectedRows'),
        Input('report_height', 'data'),
        Input('hide_keys', 'data'),
        Input('hide_applied', 'data'),
        prevent_initial_call=True,
    )
    def persist_state(location_checklist, weeks_dropdown, direction_dropdown,
                      custom_date_range_start, custom_date_range_end, column_state,
                      selected, report_height, hide_keys, hide_applied):
        """Persist filters, column state, selection, pane height, and hidden rows to app state."""

        # Keep the last remembered selection when the grid reports none selected.
        selected_key = (selected[0].get('__rowkey') if selected
                        else load_state().get('selected_key'))

        # Weeks and custom range are mutually exclusive. A weeks value recomputes relative to
        # today on each open, so don't persist the mirrored window as a stale custom range; only a
        # real custom range (weeks blank) is stored.
        if weeks_dropdown is not None:
            custom_start = custom_end = None
        else:
            custom_start, custom_end = custom_date_range_start, custom_date_range_end
        save_state(dict(
            locations=location_checklist or [],
            weeks=weeks_dropdown,
            direction=direction_dropdown or DIRECTION_DEFAULT,
            custom_start=custom_start,
            custom_end=custom_end,
            column_state=column_state or [],
            selected_key=selected_key,
            report_height=report_height,
            hide_keys=hide_keys or [],
            hide_applied=bool(hide_applied),
        ))
        return ''

    # Hide feature (clientside). Checking a Hide box updates the remembered key set and, while
    # hiding is on, re-runs the external filter so the row leaves immediately.
    app.clientside_callback(
        '''async function(changed, hideKeys){
            const keys = new Set(hideKeys || []);
            (changed || []).forEach(function(c){
                if(c.colId === 'hide'){
                    const k = c.data.__rowkey;
                    if(c.data.hide){ keys.add(k); } else { keys.delete(k); }
                }
            });
            if(window.__craHide){
                const api = await window.dash_ag_grid.getApiAsync('table');
                if(api){ api.onFilterChanged(); }
            }
            return Array.from(keys).sort();
        }''',
        Output('hide_keys', 'data'),
        Input('table', 'cellValueChanged'),
        State('hide_keys', 'data'),
        prevent_initial_call=True,
    )

    # Clear every Hide check in one click and re-run the filter so any hidden rows return.
    app.clientside_callback(
        '''async function(n){
            const api = await window.dash_ag_grid.getApiAsync('table');
            if(api){
                api.forEachNode(function(node){ if(node.data){ node.data.hide = false; } });
                api.refreshCells({force: true});
                api.onFilterChanged();
            }
            return [];
        }''',
        Output('hide_keys', 'data', allow_duplicate=True),
        Input('hide_clear', 'n_clicks'),
        prevent_initial_call=True,
    )

    # On load and every rebuild, re-apply the saved Hide state so hidden rows stay hidden across
    # sessions. Reads the persisted hide_applied flag (State, so it doesn't fight the live toggle).
    app.clientside_callback(
        '''async function(rowData, applied){
            const api = await window.dash_ag_grid.getApiAsync('table');
            if(!api){ return ''; }
            // getRowId reuses row nodes across refreshes, so AG Grid only re-renders a cell whose
            // own bound value changed. The severity icon reads a sibling __sev field, so toggling
            // a check off (which clears __sev but not the cell's text) would leave a stale icon
            // until the text itself changed. Force a re-render of every cell against the new row
            // data.
            try{ api.refreshCells({force: true, suppressFlash: true}); }catch(e){}
            window.__craHide = applied === true;
            if(applied){
                window.dashAgGridComponentFunctions.installHideFilter(api);
                try{ api.onFilterChanged(); }catch(e){}
            }
            return '';
        }''',
        Output('hide_sink', 'children'),
        Input('table', 'rowData'),
        State('hide_applied', 'data'),
        prevent_initial_call=True,
    )

    def feedback_dialog_step(prefix, filenames, text):
        """Shared Bug/Suggestion dialog state machine for the pre-submit triggers

        Returns:
            tuple[tuple[object, ...] | None, str | None]: (result, note). result is the callback's
                6-tuple for the open/cancel/attach/empty-note triggers (note is None), or None
                with the stripped note when the submit should proceed.
        """
        trig = ctx.triggered_id
        if trig == f'{prefix}_btn':
            return (MODAL_SHOWN, '', '', '', None, None), None
        if trig == f'{prefix}_cancel':
            return (MODAL_HIDDEN, no_update, '', '', None, None), None
        if trig == f'{prefix}_upload':

            # Render the names of files staged on the upload control
            names = [n for n in (filenames or []) if n]
            attach = '' if not names else html.Div(
                [html.Div(n) for n in names], style=dict(fontSize='11px', color='#555'))
            return (no_update, no_update, no_update, attach, no_update, no_update), None
        note = (text or '').strip()
        if not note:
            return (no_update, no_update, html.Span(
                'Please enter a note before submitting.', style=ERROR_TEXT_STYLE),
                    no_update, no_update, no_update), None
        return None, note

    def feedback_submit(kind, note, contents, filenames, **kwargs):
        """Save a Bug/Suggestion note with its attached files and return the callback 6-tuple.

        Args:
            kind (str): 'bug' or 'suggestion'.
            note (str): Stripped note text.
            contents (list[str] | None): dcc.Upload contents.
            filenames (list[str] | None): dcc.Upload filenames.
            **kwargs: Extra save_feedback keywords (bug_case, report_dir).

        Returns:
            tuple[object, ...]: The dialog's 6 output values; the dialog closes on success.
        """
        try:
            save_feedback(kind, note, attachments=_zip_uploads(contents, filenames), **kwargs)
        except Exception:
            logger.exception('feedback save failed')
            return (no_update, no_update, html.Span(
                'Could not save -- check the log.', style=ERROR_TEXT_STYLE),
                    no_update, no_update, no_update)
        logger.info('feedback saved kind=%s len=%d bug_case=%s', kind, len(note),
                    kwargs.get('bug_case'))
        return MODAL_HIDDEN, '', '', '', None, None

    @app.callback(
        Output('bug_modal', 'style'),
        Output('bug_text', 'value'),
        Output('bug_status', 'children'),
        Output('bug_attach', 'children'),
        Output('bug_upload', 'contents'),
        Output('bug_upload', 'filename'),
        Input('bug_btn', 'n_clicks'),
        Input('bug_cancel', 'n_clicks'),
        Input('bug_submit', 'n_clicks'),
        Input('bug_upload', 'filename'),
        State('bug_text', 'value'),
        State('bug_upload', 'contents'),
        State('table', 'rowData'),
        prevent_initial_call=True,
    )
    def bug_report(open_n, cancel_n, submit_n, filenames, text, contents, row_data):
        """Open/close the Bug Report dialog; on Submit log the note with any attached files.

        A bug may not be tied to the selected row, so the whole board the reviewer saw is dumped;
        the selected row is identified natively by selected_key in app_state.json.
        """
        result, note = feedback_dialog_step('bug', filenames, text)
        if result is not None:
            return result

        # The report folder gets the whole board the reviewer saw (retained state, no re-poll)
        # plus the app state.
        stem = 'bug_' + datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        report_dir = os.path.join(config.bug_report_dir(), stem)
        try:
            os.makedirs(report_dir, exist_ok=True)
            with open(os.path.join(report_dir, 'app_state.json'), 'w') as f:
                json.dump(load_state(), f, indent=2)
            if row_data:
                snapshot.write_snapshot(report_dir, db_pull, row_data, 'state')
        except Exception:
            logger.exception('state dump failed for bug %s', stem)
        return feedback_submit('bug', note, contents, filenames, bug_case=stem,
                               report_dir=report_dir)

    @app.callback(
        Output('suggestion_modal', 'style'),
        Output('suggestion_text', 'value'),
        Output('suggestion_status', 'children'),
        Output('suggestion_attach', 'children'),
        Output('suggestion_upload', 'contents'),
        Output('suggestion_upload', 'filename'),
        Input('suggestion_btn', 'n_clicks'),
        Input('suggestion_cancel', 'n_clicks'),
        Input('suggestion_submit', 'n_clicks'),
        Input('suggestion_upload', 'filename'),
        State('suggestion_text', 'value'),
        State('suggestion_upload', 'contents'),
        prevent_initial_call=True,
    )
    def suggestion_box(open_n, cancel_n, submit_n, filenames, text, contents):
        """Open/close the Suggestion Box dialog; on Submit log the note with any attached files.

        No patient data is touched.
        """
        result, note = feedback_dialog_step('suggestion', filenames, text)
        if result is not None:
            return result
        return feedback_submit('suggestion', note, contents, filenames)

    @app.callback(
        Output('settings_modal', 'style'),
        Input('settings_btn', 'n_clicks'),
        Input('settings_backdrop', 'n_clicks'),
        prevent_initial_call=True,
    )
    def settings_visibility(open_n, backdrop_n):
        """Open the Settings dialog from the toolbar; close it on a click outside the panel."""
        if ctx.triggered_id == 'settings_btn':
            return MODAL_SHOWN
        return MODAL_HIDDEN

    # Per-location controls, in the order settings_apply slices them: label, room, one Input per
    # checklist component (see machine_check_specs -- ICC is split per finding), the three ICC
    # threshold boxes, then the WCC "count on-time ICC" toggle. Built programmatically so raising
    # config.MAX_MACHINES needs no callback edits.
    check_specs = machine_check_specs()
    loc_ids = [[f'machine_label_{i}',
                f'machine_room_{i}',
                *[f'machine_checks_{i}_{suffix}' for suffix, _ in check_specs],
                f'machine_icc_missed_fx_{i}',
                f'machine_sbrt_fx_dose_{i}',
                f'machine_sbrt_missed_fx_{i}',
                f'machine_icc_wcc_{i}']
               for i in range(config.MAX_MACHINES)]
    per_loc = len(loc_ids[0])
    apply_inputs = [Input(cid, 'value') for ids in loc_ids for cid in ids]
    apply_inputs.append(Input('cfg_refresh_seconds', 'value'))
    apply_inputs.append(Input('cfg_track_qcls', 'value'))
    apply_inputs.append(Input('slot_active_store', 'data'))

    def settings_apply(*vals):
        """Persist each settings control the moment it changes -- there is no Save button.

        Location label/room/checks and the general options are assembled into the config shape and
        saved to user_config.toml as overrides of the clinic baseline (config.save_settings);
        bumping settings_applied re-runs apply_settings so the board reflects the new per-location
        filters. Every location slot is saved with its active flag from slot_active_store, so
        Add/Remove Location also lands here. settings_applied also re-applies refresh_seconds
        (apply_refresh_seconds) and the Pending QCLs column (apply_track_qcls).
        """
        refresh_seconds = vals[config.MAX_MACHINES * per_loc]
        track_qcls = vals[config.MAX_MACHINES * per_loc + 1]
        flags, applied = vals[-2], vals[-1]
        machines = []
        for i in range(config.MAX_MACHINES):
            base = i * per_loc
            label, room = vals[base], vals[base + 1]
            group_vals = vals[base + 2:base + 2 + len(check_specs)]
            icc_missed_fx, sbrt_fx_dose, sbrt_missed_fx, icc_wcc = (
                vals[base + 2 + len(check_specs):base + per_loc])
            picked = {cid for g in group_vals for cid in (g or [])}
            machines.append(dict(
                label=(label or '').strip(),
                room=(room or '').strip(),
                active=bool(flags[i]),
                enabled_checks=[cid for g in CHECKS.values() for cid in g if cid in picked],
                icc_missed_fx=config.clamp('icc_missed_fx', icc_missed_fx),
                sbrt_fx_dose_cgy=config.clamp('sbrt_fx_dose_cgy', sbrt_fx_dose),
                sbrt_missed_fx=config.clamp('sbrt_missed_fx', sbrt_missed_fx),
                icc_counts_as_wcc=bool(icc_wcc),
            ))
        general = dict(
            refresh_seconds=config.clamp('refresh_seconds', refresh_seconds),
            track_cc_qcls=bool(track_qcls),
        )

        # Two active locations on one room would mis-merge on load (config._merge keys machines by
        # room) and list the room twice on the board, so refuse the save and say which room.
        rooms = [m['room'] for m in machines if m['active'] and m['room']]
        dupes = sorted({r for r in rooms if rooms.count(r) > 1})
        if dupes:
            return (html.Span(f'Room {", ".join(dupes)} is used by two locations.',
                              style=ERROR_TEXT_STYLE), no_update)
        try:
            config.save_settings(general, machines)
        except Exception:
            logger.exception('settings save failed')
            return (html.Span('Could not save settings -- check the log.',
                              style=ERROR_TEXT_STYLE), no_update)
        logger.info('settings saved: %d locations', sum(1 for m in machines if m['active']))
        return '', (applied or 0) + 1

    app.callback(
        [Output('settings_status', 'children'), Output('settings_applied', 'data')],
        apply_inputs,
        [State('settings_applied', 'data')],
        prevent_initial_call=True,
    )(settings_apply)

    @app.callback(
        Output('db_settings_status', 'children'),
        Input('cfg_db_host', 'value'),
        Input('cfg_db_database', 'value'),
        Input('cfg_db_port', 'value'),
        Input('cfg_db_username', 'value'),
        Input('cfg_db_password', 'value'),
        prevent_initial_call=True,
    )
    def db_settings_apply(host, database, port, username, password):
        """Persist the database connection to db_config.toml on change. Applies on next launch.

        Fields that already match the file are not written, so the refill after Load DB Config
        leaves the copied file as is.
        """
        db = load_db_config()
        fields = (host or '', database or '', port or None, username or '', password or '')
        saved = (db.get('host') or '', db.get('database') or '', db.get('port') or None,
                 db.get('username') or '', db.get('password') or '')
        if fields == saved:
            return no_update
        try:
            save_db_config(host, database, port, username, password)
        except Exception:
            logger.exception('db config save failed')
            return html.Span('Could not save database settings -- check the log.',
                             style=ERROR_TEXT_STYLE)
        return ''

    @app.callback(
        Output('config_file_status', 'children'),
        Output('cfg_db_host', 'value'),
        Output('cfg_db_database', 'value'),
        Output('cfg_db_port', 'value'),
        Output('cfg_db_username', 'value'),
        Output('cfg_db_password', 'value'),
        Output('slot_active_store', 'data', allow_duplicate=True),
        *[Output(cid, 'value', allow_duplicate=True) for ids in loc_ids for cid in ids],
        Output('cfg_refresh_seconds', 'value'),
        Output('cfg_track_qcls', 'value'),
        Input('save_clinic_btn', 'n_clicks'),
        Input('load_db_btn', 'n_clicks'),
        Input('load_clinic_btn', 'n_clicks'),
        prevent_initial_call=True,
    )
    def config_file_buttons(save_n, load_db_n, load_clinic_n):
        """Save Clinic Config writes the effective settings to a file picked in a save dialog.

        Load DB Config and Load Clinic Config copy a file picked in an open dialog over
        db_config.toml or clinic_config.toml in the data dir, asking first if it exists. After a
        load the Database fields, or the location and General controls, are refilled from the
        loaded file, so a later edit does not write the old values back over it. The location and
        General controls take the clinic baseline without user overrides, so the loaded file is
        shown as is; refilling them triggers settings_apply, which drops the old overrides from
        user_config.toml and updates the board.
        """
        n_db = 5
        n_clinic = 1 + config.MAX_MACHINES * per_loc + 2
        keep = (no_update,) * (n_db + n_clinic)
        if ctx.triggered_id == 'save_clinic_btn':
            path = _ask_save_path()
            if path is None:
                return '', *keep
            try:
                config.save_clinic_config(path)
            except Exception:
                logger.exception('clinic config save failed')
                return html.Span('Could not save -- check the log.', style=ERROR_TEXT_STYLE), *keep
            logger.info('clinic config saved to %s', path)
            return '', *keep
        dest = (config.DB_CONFIG_FILE if ctx.triggered_id == 'load_db_btn'
                else config.CLINIC_CONFIG_FILE)
        path = _ask_open_path()
        if path is None:
            return '', *keep
        if os.path.exists(dest) and not _confirm_replace(dest):
            return '', *keep
        try:
            shutil.copyfile(path, dest)
        except shutil.SameFileError:
            return '', *keep
        except Exception:
            logger.exception('config load failed')
            return html.Span('Could not load -- check the log.', style=ERROR_TEXT_STYLE), *keep
        logger.info('loaded %s to %s', path, dest)
        if ctx.triggered_id == 'load_db_btn':
            db = load_db_config()
            return ('', db.get('host', ''), db.get('database', ''), db.get('port'),
                    db.get('username', ''), db.get('password', ''), *keep[n_db:])
        cfg = config.clinic_defaults()
        machines = cfg.get('machines') or []
        out = [_slot_flags(machines)]
        for i in range(config.MAX_MACHINES):
            label, room, checks, *rest = baseline_values(machines[i] if i < len(machines) else {})
            out += [label, room, *checks, *rest]
        general = cfg['general']
        out += [general['refresh_seconds'], ['on'] if general['track_cc_qcls'] else []]
        return '', *keep[:n_db], *out

    @app.callback(
        Output('slot_active_store', 'data'),
        Output('active_tab_store', 'data', allow_duplicate=True),
        *[Output(cid, 'value', allow_duplicate=True) for ids in loc_ids for cid in ids],
        Input('add_location_btn', 'n_clicks'),
        Input('remove_location_btn', 'n_clicks'),
        State('slot_active_store', 'data'),
        State('active_tab_store', 'data'),
        prevent_initial_call=True,
    )
    def change_locations(add_n, remove_n, flags, active):
        """Add/Remove Location: turn one location slot on or off.

        Add turns on the first inactive slot, blanks its label and room (checks and thresholds
        take the code defaults) and selects it. Remove turns off the selected slot unless it is
        the last active one. settings_apply reacts to slot_active_store and persists each slot's
        active flag; the tab view callback reacts to it for visibility.
        """
        flags = list(flags)
        values = [no_update] * (config.MAX_MACHINES * per_loc)
        if ctx.triggered_id == 'add_location_btn':
            if all(flags):
                return (no_update, no_update, *values)
            active = flags.index(False)
            flags[active] = True
            label, room, checks, *rest = baseline_values({})
            values[active * per_loc:(active + 1) * per_loc] = [label, room, *checks, *rest]
        else:
            if sum(flags) <= 1:
                return (no_update, no_update, *values)
            flags[active] = False
        return (flags, active, *values)

    # Tab view: one tab button per active location slot, only the selected tab's box is shown.
    # Recomputes on a tab click or a slot change (add/remove/restore), moving the selection to the
    # first active slot when its slot was turned off.
    @app.callback(
        Output('active_tab_store', 'data'),
        *[Output(f'location_box_{i}', 'style') for i in range(config.MAX_MACHINES)],
        *[Output(f'tab_btn_{i}', 'style') for i in range(config.MAX_MACHINES)],
        *[Input(f'tab_btn_{i}', 'n_clicks') for i in range(config.MAX_MACHINES)],
        Input('slot_active_store', 'data'),
        State('active_tab_store', 'data'),
        prevent_initial_call=True,
    )
    def location_tabs_view(*args):
        """Show only the selected tab's location box, keeping the selection on an active slot.

        Runs after a tab click or a slot change.

        Args:
            *args (int | list[bool] | None): The per-tab n_clicks then slot_active_store, with
                active_tab_store as trailing State.

        Returns:
            tuple[int | dict[str, str], ...]: The active index, every box style, then every
                tab-button style.
        """
        flags = args[-2]
        active = int(args[-1] or 0)
        trig = ctx.triggered_id
        if isinstance(trig, str) and trig.startswith('tab_btn_'):
            active = int(trig.rsplit('_', 1)[1])
        if not flags[active]:
            active = flags.index(True)
        boxes = [_box_vis(i, active, flags) for i in range(config.MAX_MACHINES)]
        tabs = [_tab_style(i, active, flags) for i in range(config.MAX_MACHINES)]
        return (active, *boxes, *tabs)

    @app.callback(
        *[Output(f'tab_btn_{i}', 'children') for i in range(config.MAX_MACHINES)],
        *[Input(f'machine_label_{i}', 'value') for i in range(config.MAX_MACHINES)],
        prevent_initial_call=True,
    )
    def sync_tab_labels(*labels):
        """Recompute each tab caption from its location's Label input.

        This way a Restore that rewrites the Labels also updates the tab captions.
        """
        return tuple(_tab_label(dict(label=labels[i]), i) for i in range(config.MAX_MACHINES))

    @app.callback(
        Output('slot_active_store', 'data', allow_duplicate=True),
        *[Output(cid, 'value', allow_duplicate=True) for ids in loc_ids for cid in ids],
        Input('restore_locations_btn', 'n_clicks'),
        prevent_initial_call=True,
    )
    def restore_default_locations(n_clicks):
        """Restore Default Locations: reset all locations to the clinic baseline.

        Resets every location's active flag and label/room/checks/thresholds to the clinic
        baseline (clinic_config.toml), in its order. Writing these values triggers settings_apply,
        which persists them to user_config.toml.
        """
        baseline = config.clinic_defaults().get('machines') or []
        out = [_slot_flags(baseline)]
        for i in range(config.MAX_MACHINES):
            m = baseline[i] if i < len(baseline) else {}
            label, room, checks, *rest = baseline_values(m)
            out += [label, room, *checks, *rest]
        return tuple(out)

    def restore(n_clicks, label, room):
        """Reset one location's label/room/checks/thresholds to the clinic baseline.

        Writing these values triggers settings_apply to persist them. The baseline is the clinic
        machine with the slot's room. A room not in the clinic config keeps its label and room and
        gets the code defaults.

        Returns:
            tuple[str | list[str] | int, ...]: (label, room, *per-checklist check lists,
                icc_missed_fx, sbrt_fx_dose, sbrt_missed_fx, icc_wcc).
        """
        baseline = config.clinic_defaults().get('machines') or []
        room = (room or '').strip()
        m = next((b for b in baseline if (b.get('room') or '').strip() == room),
                 dict(label=label or '', room=room))
        label, room, checks, *rest = baseline_values(m)
        return (label, room, *checks, *rest)

    # One Restore Defaults callback per location, registered in a loop (string-id Outputs cannot
    # be a single MATCH callback). Each resets that location's label/room/checks/thresholds to the
    # clinic baseline; writing those values then triggers settings_apply, which saves to
    # user_config.toml.
    for i in range(config.MAX_MACHINES):
        app.callback(
            [Output(cid, 'value') for cid in loc_ids[i]],
            Input(f'restore_defaults_{i}', 'n_clicks'),
            State(f'machine_label_{i}', 'value'),
            State(f'machine_room_{i}', 'value'),
            prevent_initial_call=True,
        )(restore)

    @app.callback(
        Output('report_pane', 'children'),
        Input('table', 'selectedRows'),
    )
    def show_report(selected):
        """Render the info panel for the selected row: sites table and warnings by level."""
        if not selected:
            return html.Div('Select a row to see its details and warnings.',
                            style=dict(
                                color='#888',
                                fontSize='13px',
                            ))

        # Row identity label: 'Name (MRN)', or the name alone when no MRN is present.
        r = selected[0]
        name = r.get('name', '')
        u = r.get('mrn', '')
        ident = f'{name} ({u})' if u else name

        def section(title, body):
            """Wrap body in a titled report-pane section."""
            return html.Div([
                html.Div(title, style=dict(
                    fontWeight='600',
                    fontSize='13px',
                    margin='6px 0 1px',
                )),
                body,
            ])

        # Sites table: every site of the patient's courses, with the selected board row
        # highlighted
        sites = r.get('__sites') or []
        if sites:
            th = dict(
                textAlign='left',
                fontWeight='600',
                color='#555',
                borderBottom='1px solid #ccc',
                padding='1px 12px 1px 6px',
            )
            td = dict(
                textAlign='left',
                padding='1px 12px 1px 6px',
                whiteSpace='nowrap',
            )
            head = html.Tr([html.Th(h, style=th) for h in
                            ['Dx', 'Course', 'Room', 'Site', 'Rx', 'Status', 'Start Date']])
            body_rows = []
            for p in sites:
                cur = p.get('current')

                # Always show the Dx and Course (repeat per row).
                dx = p.get('dx', '')
                course_lbl = p.get('course', '')
                cells = [dx, course_lbl, p['location'], p['site'], p['rx'],
                         p.get('status', ''), p['date']]
                body_rows.append(html.Tr(
                    [html.Td(c, style=td) for c in cells],
                    style=dict(
                        boxShadow='inset 4px 0 0 0 #1565c0, inset 0 0 0 1px #1565c0',
                        fontWeight='700',
                    ) if cur else dict(
                        fontWeight='400',
                    )))
            site_body = html.Table([head] + body_rows,
                                   style=dict(
                                       borderCollapse='separate',
                                       borderSpacing='0',
                                       fontSize='13px',
                                   ))
        else:
            site_body = html.Div('No sites', style=dict(
                color='#888',
                fontSize='13px',
            ))

        def warn_list(items):
            """Render a bullet list of messages, or a green 'None' when empty."""
            if not items:
                return html.Div('None', style=dict(
                    color='#080',
                    fontSize='13px',
                ))
            return html.Ul([html.Li(w, style=dict(marginBottom='2px')) for w in items],
                           style=dict(
                               margin='0',
                               paddingLeft='18px',
                               fontSize='13px',
                           ))

        # Assemble the panel: identity, sites table, then findings grouped by level.
        return html.Div([
            html.Div(ident, style=dict(
                fontWeight='600',
                fontSize='14px',
                marginBottom='4px',
            )),
            section('Sites', site_body),
            section('Notifications', warn_list(r.get('__notifications') or [])),
            section('Warnings', warn_list(r.get('__warnings') or [])),
            section('Errors', warn_list(r.get('__errors') or [])),
        ])


def rebuild_rows(location_checklist, weeks_dropdown, direction_dropdown, custom_start, custom_end,
                 hide_keys, soft=False):
    """Rebuild the board rows for the refresh callbacks.

    Load (a live query, or the retained pull when soft) and stamp hide flags.
    """
    row_data, *rest = load_rows(location_checklist, weeks_dropdown, direction_dropdown,
                                custom_start, custom_end, soft=soft)

    # Set each row's Hide checkbox from the remembered hide_keys so checks survive a rebuild
    keys = set(hide_keys or [])
    for r in row_data:
        r['hide'] = r.get('__rowkey') in keys
    return (row_data, *rest)


def load_rows(location_checklist, weeks_dropdown, direction_dropdown, custom_start_in,
              custom_end_in, soft=False):
    """Query the DB for the current filters and build the grid rows plus status outputs.

    Called via rebuild_rows by the refresh, auto-refresh, and settings callbacks.

    Args:
        location_checklist (list[str]): Selected rooms.
        weeks_dropdown (int | None): Relative weeks window; None selects the custom date range.
        direction_dropdown (str | None): 'past' | 'surrounding' | 'next'.
        custom_start_in (str | None): Custom range start (ISO), used when weeks is blank.
        custom_end_in (str | None): Custom range end (ISO), used when weeks is blank.
        soft (bool): Rebuild from the last DB pull when True (used for settings changes); falls
            back to a live query when no pull is retained.

    Returns:
        tuple[list[dict[str, object]], list[dict[str, object]], str, str, dict[str, str]]:
            (row_data, selected_rows, last_updated, warning_children, warning_style).
    """
    global db_pull
    t0 = datetime.datetime.now()
    ts = fmt_time(t0)
    direction = direction_dropdown or DIRECTION_DEFAULT

    # Mode is the weeks box: a number = relative weeks window; blank = custom date range (4 weeks
    # when no custom range is set).
    if weeks_dropdown is None and custom_start_in and custom_end_in:
        weeks = None
        custom_start, custom_end = custom_start_in, custom_end_in
    else:
        weeks = max(1, weeks_dropdown or WEEKS_DEFAULT)
        custom_start = custom_end = None

    # Before any query, flag an unconfigured install so the empty board is explained (distinct from
    # a connection outage below): no db_config.toml, or its host/database are blank.
    if not DEMO_MODE and not db_configured():
        msg = (f'{chr(0x26A0)} Database access is not configured -- set the server and database '
               f'in db_config.toml (see db_config.example.toml) -- {ts}')
        return [], [], '', msg, WARNING_STYLE

    # Likewise a fresh install with no usable clinic_config.toml: say so before the "no rooms"
    # message, which is the symptom, not the cause.
    if not DEMO_MODE:
        problem = clinic_config_problem()
        if problem:
            return [], [], '', f'{chr(0x26A0)} {problem} -- {ts}', WARNING_STYLE
    if not location_checklist:
        logger.warning('poll no rooms selected')
        return [], [], f'No rooms selected -- {ts}', '', WARNING_HIDDEN

    # Assemble the poll state, query the DB, and build the board rows.
    try:
        state = dict(
            locations=location_checklist,
            weeks=weeks,
            direction=direction,
            custom_start=custom_start,
            custom_end=custom_end,
        )

        # Demo mode: pin the clock to the demo's NOW so the pull, findings and window match its
        # data.
        if DEMO_MODE:
            state['now'] = demo_data.DEMO_NOW

        # Soft refresh (settings change): rebuild rows from the last pull with no query, so a
        # toggle applies without re-hitting Mosaiq or (in deid mode) re-shifting the
        # already-shifted dates.
        if soft and db_pull:
            if DEID_MODE:
                state['now'] = pd.Timestamp(db_pull['now'])
            courses = courses_from_pull(db_pull)
        else:
            # The live board resolves names normally; in deid mode the scrubbed pull below is the
            # single gate, replacing the queried names with surrogates before courses are built.
            # The pull stays local until it is scrubbed: the server is threaded, and a bug report
            # or soft refresh reading the global db_pull meanwhile must never see the identified
            # pull.
            courses, pull = query_db(state)

            # Deid mode: de-identify the live pull in memory and pin the clock to its rewound NOW
            # so the window rewinds with the shifted sites.
            if DEID_MODE and pull:
                pull = snapshot.deid_pull(pull)
                state['now'] = pd.Timestamp(pull['now'])
                courses = courses_from_pull(pull)
            db_pull = pull

        window = selection_window(state)
        row_data = build_table(courses, window, location_checklist)

        # Restore the last selection if its site is still in the window, else the first row.
        sel_key = load_state().get('selected_key')
        match = [r for r in row_data if r.get('__rowkey') == sel_key]
        selected = match or (row_data[:1])
        logger.info('poll win=%s..%s dir=%s rooms=%d pats=%d courses=%d rows=%d %.2fs',
                    window[0], window[1], direction, len(location_checklist),
                    len({c.pat_id for c in courses}), len(courses), len(row_data),
                    (datetime.datetime.now() - t0).total_seconds())

        # Empty board: log why every site was dropped -- window (no calendar fraction in range) vs
        # room (site's calendar rooms miss the selected locations). Counts only, no patient data.
        if not row_data and courses:
            sites = [s for c in courses for s in c.sites]
            inwin = sum(1 for s in sites if window_contains(s.tx_cal_dts, window))
            roomok = sum(1 for s in sites if set(s.rooms) & set(location_checklist))
            caldts = sum(1 for s in sites if any(pd.notna(dt) for dt in s.tx_cal_dts))
            logger.info('poll empty diag sites=%d caldts=%d inwin=%d roomok=%d locs=%s',
                        len(sites), caldts, inwin, roomok, location_checklist)

        # Deid/demo mode: the header carries a persistent mode indicator, so leave the top warning
        # banner clear and skip the live clinic-config warning (irrelevant here).
        if DEID_MODE or DEMO_MODE:
            return row_data, selected, f'Last updated: {ts}', '', WARNING_HIDDEN

        # Non-blocking warning: the board still renders, but the Pending QCLs column stays empty
        # and charge findings are skipped until the clinic sets these in clinic_config.toml.
        # Easy to miss, hence the banner. ICC/WCC/FCC findings come from CC notes and are
        # unaffected.
        gen = config.load_config()['general']
        missing = []
        if not gen.get('qcl_task_types'):
            missing.append('qcl_task_types (Pending QCLs column stays empty)')
        if not gen.get('cc_cpt_code'):
            missing.append('cc_cpt_code (charge findings will not report)')
        if missing:
            msg = (f'{chr(0x26A0)} clinic_config.toml [general] not set: {"; ".join(missing)} '
                   f'-- {ts}')
            return row_data, selected, f'Last updated: {ts}', msg, WARNING_STYLE
        return row_data, selected, f'Last updated: {ts}', '', WARNING_HIDDEN
    except Exception as e:
        from sqlalchemy.exc import InterfaceError, OperationalError

        # Distinguish a DB-connection failure from any other build error in the status banner.
        # Only connection-level errors get the VPN message; a SQL/permission error (also a
        # DBAPIError) is a build error and is logged with its traceback.
        if isinstance(e, (OperationalError, InterfaceError)):
            logger.warning('poll db connection failed: %s', e)
            host = db_host()
            where = f" '{host}'" if host else ''
            msg = (f'{chr(0x26A0)} Cannot reach the Mosaiq database server{where} -- '
                   f'check your VPN/network connection -- {ts}')
        else:
            logger.exception('poll build error')
            msg = f'{chr(0x26A0)} Error building table -- {ts}'
        return [], [], '', msg, WARNING_STYLE


def build_table(courses, window=None, locations=None):
    """Flatten the Course list into AG Grid rows -- one row per (site, room) of every course.

    Sites are filtered individually: only those with a fraction inside the window are listed
    (siblings stay visible in the info-pane site table). A site scheduled across several rooms
    yields one row per room; when `locations` is given, only rooms in it are emitted (the board
    room filter). Display labels are resolved at the poll level (query_db) and read off
    course.last_name, course.first_name and course.mrn here. Rows are ordered by room, then patient surname/first name, then site.

    Args:
        courses (list[Course]): Courses to render.
        window (tuple[datetime.date, datetime.date] | None): (start, end) date range; sites
            entirely outside it are dropped, None keeps every site.
        locations (list[str] | None): Rooms to include; a site's row is emitted once per room it is
            scheduled in that is also in this list. None keeps every room.

    Returns:
        list[dict[str, object]]: One record per rendered (site, room), keyed by column plus the
            report-pane fields.
    """

    # Keep only sites with at least one fraction inside the selection window. Sibling sites in the
    # same course that are already treated or start after the window drop off the board (they still
    # appear in the info-pane site table).
    triples = []
    for c in courses:
        for p in c.sites:
            if window is not None and not window_contains(p.tx_cal_dts, window):
                continue

            # Location scoping happens here: a site scheduled across several rooms yields one board
            # row per room, restricted to `locations` when given (query_db room-filters only the
            # Schedule pulls). A roomless site is represented by a single blank room only when no
            # location filter is in effect.
            rooms = p.rooms or ['']
            if locations is not None:
                rooms = [r for r in rooms if r in locations]
            triples += [(c, p, r) for r in rooms]

    cfg = config.load_config()
    skip_mrns = cfg['general'].get('skip_mrns') or []
    triples = [(c, p, r) for c, p, r in triples if c.mrn not in skip_mrns]

    # Per-patient diagnosis list for the info panel (every course's sites, all courses).
    # Group courses by diagnosis (MED_ID) so same-ICD-10 episodes stay distinct; each entry is a
    # (dx label, sites earliest-first) pair.
    by_dx = {}
    for c in courses:
        by_dx.setdefault((c.pat_id, c.med_id), []).append(c)
    pat_dx = {}
    for (pat_id, med_id), cs in by_dx.items():
        sites = sorted(
            [s for c in cs for s in c.sites],
            key=lambda s: s.tx_cal_dts[0] if pd.notna(s.tx_cal_dts[0]) else pd.Timestamp.max)
        pat_dx.setdefault(pat_id, []).append((cs[0].dx, sites))

    # Order the surviving triples, then build one board record per (course, site, room) below.
    ordered = sorted(triples, key=lambda cpr: (
        (cpr[2] or ''),
        (cpr[0].last_name or ''),
        (cpr[0].first_name or ''),
        (cpr[1].site_name or ''),
    ))

    # Per-machine settings: drop a site's findings whose check id is disabled for its room.
    # Defaults enable every check, so an unconfigured install is unchanged.
    room_checks = config.settings_enabled_map(cfg)
    rows = []
    for course, site, room in ordered:
        rowkey = f'{course.pat_id}|{course.pcp_id}|{site.site_name}|{room}'

        # Tx Time: scheduled today (top, sorted by time), else complete, else not scheduled (course
        # started), else not started (no course fractions). Field holds a numeric sort key; the
        # label is shown via valueFormatter.
        complete = site.complete
        appt = course.tx_appt_dt
        if not complete and appt is not None:
            tx_time_label = fmt_time(appt)
            tx_time_sort = appt.hour * 60 + appt.minute
        elif complete:
            tx_time_label = 'Site Complete'
            tx_time_sort = 100000
        elif len(course.tx_hst):
            tx_time_label, tx_time_sort = 'Not Scheduled', 150000
        else:
            tx_time_label, tx_time_sort = 'Not Started', 200000
        rec = dict(
            tx_time=tx_time_sort,
            room=room,
            name=', '.join(x for x in [course.last_name, course.first_name] if x),
            mrn=course.mrn,
            site_name=site.site_name,
            rx=site.rx,
            n_fxs_txd=(f'{len(site.tx_hst)}/{site.n_rx_fxs:.0f}'
                       if pd.notna(site.n_rx_fxs) else str(len(site.tx_hst))),
            last_fx_txd_dt=fmt_date(site.tx_hst['Tx_DtTm'].iloc[-1]
                                    if not site.tx_hst.empty else None),
            pending_qcls=site.pending_qcls,
            n_fxs_since_last_cc=('N/A' if len(course.tx_hst_dts) == 0
                                 else course.n_fxs_since_last_cc),
            icc_completion_dt=fmt_date(site.icc_completion_dt),
            last_wcc_completion_dt=fmt_date(course.last_wcc_completion_dt),
            n_wcc_completed=f'{course.n_wcc_completed}/{course.expected_wccs}',
            fcc_completion_dt=fmt_date(course.fcc_completion_dt),
            allowed_cc_charges=f'{len(course.cc_charges)}/{course.allowed_cc_charges}',
        )
        for col in COLUMNS:
            key = col['key']
            rec[f'{key}__tip'] = ''
            rec[f'{key}__sev'] = ''

        # Findings bucketed by level for the report pane (info = notification, warning, error), and
        # per column: the icon severity (__sev) and a rich hover (__tip) grouped by level like the
        # report pane (empty sections omitted)
        errors, warnings, notifications = [], [], []
        cell = {}
        enabled = room_checks.get(room)
        for w in site.warnings:
            if enabled is not None and w['id'] not in enabled:
                continue
            level, col_key, message = w['level'], w['field'], w['message']
            bucket = dict(
                error=errors,
                warning=warnings,
            ).get(level, notifications)
            bucket.append(message)
            cell.setdefault(col_key, dict(
                error=[],
                warning=[],
                info=[],
            ))[level].append(message)
        for col_key, groups in cell.items():
            sev = ''
            if groups['info']:
                sev += 'i'
            if groups['warning']:
                sev += 'w'
            if groups['error']:
                sev += 'e'
            rec[f'{col_key}__sev'] = sev

            # Cell hover HTML: bold level headings over bulleted messages, like the report pane
            parts = []
            for title, level in (('Notifications', 'info'), ('Warnings', 'warning'),
                                 ('Errors', 'error')):
                items = groups.get(level)
                if items:
                    lis = ''.join(f'<li>{m}</li>' for m in items)
                    parts.append(f'<b>{title}</b><ul>{lis}</ul>')
            rec[f'{col_key}__tip'] = ''.join(parts)

        # Row color by severity: errors outrank warnings; clean (no warnings/errors) is green.
        if errors:
            rec['alert'] = 'orange'
        elif warnings:
            rec['alert'] = 'yellow'
        else:
            rec['alert'] = 'green'
        rec['tx_time_label'] = tx_time_label
        rec['__errors'] = errors
        rec['__warnings'] = warnings
        rec['__notifications'] = notifications

        # Info-panel site table: every site of all the patient's courses, grouped by diagnosis,
        # with the selected board row flagged
        rec['__sites'] = [
            dict(
                dx=dx,
                course=s.course_n,
                site=s.site_name,
                rx=s.rx,
                location=', '.join(s.rooms),
                status=('Complete' if s.complete
                        else 'Active' if len(s.tx_hst) else 'Not started'),
                date='' if pd.isna(s.tx_cal_dts[0]) else s.tx_cal_dts[0].strftime('%Y-%m-%d'),
                current=s is site,
            )
            for dx, sites in pat_dx.get(course.pat_id, [])
            for s in sites
        ]
        rec['__rowkey'] = rowkey
        rows.append(rec)
    return rows


def save_feedback(kind, text, bug_case=None, report_dir=None, attachments=None):
    """Append one bug/suggestion note as a JSON line to the feedback log.

    Args:
        kind (str | None): 'bug' or 'suggestion'; None is logged as 'other'.
        text (str): Note text.
        bug_case (str | None): The bug report folder name for this report; a PHI-free reference
            back to the captured data under the bug report dir.
        report_dir (str | None): When set (the bug path), that report's own timestamped folder:
            the note is written there as report.txt and attachments are saved into it, so the
            folder is self-contained. Otherwise attachments go under the feedback attachments
            dir.
        attachments (list[dict[str, str]] | None): {filename, content} upload dicts (content a
            base64 data URI).
    """
    stamp = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
    if report_dir:
        os.makedirs(report_dir, exist_ok=True)
        with open(os.path.join(report_dir, 'report.txt'), 'w', encoding='utf-8') as f:
            f.write('ts: ' + datetime.datetime.now().isoformat(timespec='seconds') + '\n')
            f.write('user: ' + os.environ.get('USERNAME', '') + '\n\n')
            f.write(text or '')
        dest = report_dir
    else:
        dest = os.path.join(os.path.dirname(feedback_file()), 'attachments',
                            f'{kind or "other"}_{stamp}')

    # Decode uploaded files into dest; the saved paths go into the log record.
    saved = []
    if attachments:
        import base64
        os.makedirs(dest, exist_ok=True)
        for item in attachments:
            name = os.path.basename(item.get('filename') or '')
            data = item.get('content') or ''
            if not name or ',' not in data:
                continue
            with open(os.path.join(dest, name), 'wb') as f:
                f.write(base64.b64decode(data.split(',', 1)[1]))
            saved.append(os.path.join(dest, name))
    rec = dict(
        ts=datetime.datetime.now().isoformat(timespec='seconds'),
        user=os.environ.get('USERNAME', ''),
        kind=kind or 'other',
        text=text,
        bug_case=bug_case,
        report_dir=report_dir,
        attachments=saved,
    )
    path = feedback_file()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, 'a', encoding='utf-8') as f:
        f.write(json.dumps(rec) + '\n')


def run_app(app):
    """Launch the Dash app in the configured mode: desktop, browser, or headless.

    Replaces any prior instance recorded in app state, binds a free OS port and records it, then
    serves. Desktop opens a native WebView2 window, browser opens the OS default browser, and
    headless serves with no window. CRA_NO_BROWSER forces headless; otherwise
    mode comes from CRA_MODE (default desktop).

    Args:
        app (dash.Dash): The Dash app to serve.
    """
    import threading, time, socket, subprocess

    def _wait_port(host_, port_, timeout=15):
        """Block until the server accepts connections on host:port, or the timeout elapses."""
        end = time.time() + timeout
        while time.time() < end:
            with socket.socket() as s_:
                s_.settimeout(0.5)
                if s_.connect_ex((host_, port_)) == 0:
                    return
            time.sleep(0.2)

    host = os.environ.get('DASH_HOST', '127.0.0.1')

    # Kill the prior instance (its port is recorded in app state) so this launch replaces it.
    prior = load_state().get('port')
    if prior:
        out = subprocess.run(['netstat', '-ano'], capture_output=True, text=True,
                             creationflags=subprocess.CREATE_NO_WINDOW).stdout
        me = os.getpid()
        for line in out.splitlines():
            parts = line.split()
            if len(parts) >= 5 and parts[1].endswith(f':{prior}') and parts[3] == 'LISTENING':
                pid = int(parts[4])
                if pid != me:
                    subprocess.run(['taskkill', '/PID', str(pid), '/T', '/F'], capture_output=True,
                                   creationflags=subprocess.CREATE_NO_WINDOW)

    # Pick a free port from the OS and record it so the next launch can find and replace us.
    with socket.socket() as s:
        s.bind((host, 0))
        port = s.getsockname()[1]
    save_state(dict(port=port))

    mode = ('headless' if os.environ.get('CRA_NO_BROWSER')
            else os.environ.get('CRA_MODE', 'desktop'))
    if mode == 'headless':

        # Headless: serve without a native window.
        app.run(host=host, port=port, debug=False, threaded=True)
    elif mode == 'browser':

        # Browser: serve on the main thread; open the OS default browser once the port is up.
        import webbrowser
        def _open():
            """Wait for the server port, then open the board in the OS default browser."""
            _wait_port(host, port)
            webbrowser.open(f'http://{host}:{port}')

        threading.Thread(target=_open, daemon=True).start()
        app.run(host=host, port=port, debug=False, threaded=True)
    else:
        # Desktop: serve on a background thread. If the server dies before the port opens (port
        # in use, import error) the window must not open on a dead page: the error is logged and
        # shown in the window instead.
        server_error = []

        def _serve():
            """Run the server; record a startup failure for the window to show."""
            try:
                app.run(host=host, port=port, debug=False, threaded=True)
            except Exception as e:
                logger.exception('server failed to start')
                server_error.append(e)

        threading.Thread(target=_serve, daemon=True).start()
        _wait_port(host, port)
        import webview

        # Restore saved window placement/size/state; first run opens maximized.
        geo = load_state().get('window_geo') or {}

        # Heal an off-screen minimize sentinel (~-32000) or stale minimized flag saved by an
        # earlier build so the window reopens on a real monitor rather than nowhere.
        if (geo.get('x') or 0) <= -30000 or (geo.get('y') or 0) <= -30000:
            geo = dict(geo, x=None, y=None, minimized=False)
        geo = dict(x=geo.get('x'), y=geo.get('y'),
                   width=geo.get('width') or 800, height=geo.get('height') or 600,
                   maximized=bool(geo.get('maximized', True)),
                   minimized=bool(geo.get('minimized', False)))
        if server_error:
            page = (f'<h2>The app server failed to start</h2><pre>{server_error[0]}</pre>'
                    '<p>See data\\logs\\cra.log for the full error.</p>')
            window = webview.create_window('Physics Chart Review Assistant', html=page, **geo)
        else:
            window = webview.create_window(
                'Physics Chart Review Assistant', f'http://{host}:{port}', **geo)
        _bind_window_state(window, geo)
        icon = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'assets', 'icons', 'icon_blue_black_1024.ico')
        webview.start(icon=icon)


def _bind_window_state(window, geo):
    """Persist the pywebview window's placement, size, and max/min state across sessions.

    geo is seeded from the saved geometry (live window props are unavailable until
    webview.start) and updated in place. Size is only re-sampled while in the normal (restored)
    frame so the maximized/minimized frame never overwrites the saved normal size.

    Args:
        window (webview.Window): The board window.
        geo (dict[str, int | bool | None]): Normalised x, y, width, height, maximized, minimized.
    """

    def remember_frame(*a):
        """Sample the live window placement into geo.

        Position is sampled unless minimized or off-screen; size only when restored.
        """

        # Skip while minimized. Otherwise capture position so the monitor is remembered even while
        # maximized; capture size only in the normal frame so the restore-down size is preserved.
        if geo['minimized']:
            return
        x, y = window.x, window.y

        # A minimize can fire a moved event before the minimized flag is set, reporting the
        # ~(-32000, -32000) off-screen sentinel; never latch that or the remembered monitor is
        # lost on the next launch.
        if x <= -30000 or y <= -30000:
            return
        geo['x'], geo['y'] = x, y
        if not geo['maximized']:
            geo['width'], geo['height'] = window.width, window.height

    def on_maximized(*a):
        """Mark the frame maximized so remember_frame stops sampling the restore-down size."""
        geo['maximized'] = True

    def on_minimized(*a):
        """Mark the frame minimized so remember_frame skips sampling entirely."""
        geo['minimized'] = True

    def on_restored(*a):
        """Clear max/min state and re-sample the normal-frame placement."""
        geo['maximized'] = geo['minimized'] = False
        remember_frame()

    def on_closing(*a):
        """Persist the final window geometry to the state file on close."""
        remember_frame()
        save_state(dict(window_geo=geo))

    window.events.resized += remember_frame
    window.events.moved += remember_frame
    window.events.maximized += on_maximized
    window.events.minimized += on_minimized
    window.events.restored += on_restored
    window.events.closing += on_closing


def track_qcls_column_defs(column_defs, cfg):
    """Grid column defs without Pending QCLs when the general track_cc_qcls setting is off

    Args:
        column_defs (list[dict[str, object]]): Full AG Grid column defs.
        cfg (dict[str, object]): Merged config.

    Returns:
        list[dict[str, object]]: column_defs, minus the pending_qcls column when tracking is off.
    """
    if cfg['general'].get('track_cc_qcls', config.TRACK_CC_QCLS_DEFAULT):
        return column_defs
    return [d for d in column_defs if d.get('field') != 'pending_qcls']


def load_state():
    """Load saved UI state merged over defaults; return defaults on any error."""
    try:
        with open(STATE_FILE) as f:
            return dict(DEFAULT_STATE, **json.load(f))
    except Exception:
        return DEFAULT_STATE.copy()


def board_today():
    """The board's display-date 'today'

    In demo mode the pinned demo date; in deid mode the live date rewound by the session's stable
    deid delta, so the header window matches the shifted board; otherwise the real current date

    Returns:
        datetime.date: Display-date today.
    """
    if DEMO_MODE:
        return demo_data.DEMO_NOW.date()
    today = datetime.date.today()
    if DEID_MODE:
        today = (pd.Timestamp(today) + snapshot.session_delta()).date()
    return today


def machine_check_specs():
    """Ordered (id_suffix, cids) per settings checklist component for one machine

    ICC is split into one single-option checkbox per finding so each lines up with its threshold
    box; the other groups stay one checklist each. Drives the layout and every settings callback so
    they stay in sync.

    Returns:
        list[tuple[str, list[str]]]: (id_suffix, cids) pairs. The suffix is appended to
            machine_checks_{i}_ to form the component id; cids is the list of finding ids that
            component can hold.
    """
    specs = []
    for gid in GROUPS:
        if gid == 'icc':
            for cid in CHECKS[gid]:
                specs.append((f'icc_{cid}', [cid]))
        else:
            specs.append((gid, list(CHECKS[gid])))
    return specs


def baseline_values(m):
    """One location's Settings control values from a machine config dict

    Shared by the Settings layout seeding, the Restore Defaults and location callbacks and the
    Load Clinic Config refill.

    Args:
        m (dict[str, object]): Machine config dict.

    Returns:
        tuple[str, str, list[list[str]], int, int, int, list[str]]: (label, room, per-checklist
            check lists, icc_missed_fx, sbrt_fx_dose, sbrt_missed_fx, icc_wcc).
    """
    enabled = m.get('enabled_checks')
    checks = [[cid for cid in cids
               if enabled is None or cid in enabled]
              for _, cids in machine_check_specs()]
    th = config.machine_thresholds(m)
    return (m.get('label', ''), m.get('room', ''), checks,
            th['icc_missed_fx'], th['sbrt_fx_dose_cgy'], th['sbrt_missed_fx'],
            ['on'] if th['icc_counts_as_wcc'] else [])


def _box_vis(i, active, flags):
    """Location-box style: shown only for the selected tab when its slot is active"""
    return dict() if i == active and flags[i] else dict(display='none')


def _tab_label(m, i):
    """Tab caption: the location's column label, falling back to 'Location N' when blank"""
    return (m.get('label') or '').strip() or f'Location {i + 1}'


def _tab_style(i, active, flags):
    """Tab-button style: highlighted when selected, hidden when its slot is inactive"""
    s = dict(TAB_ACTIVE_STYLE if i == active else TAB_STYLE)
    if not flags[i]:
        s['display'] = 'none'
    return s


def fmt_time(ts):
    """Format a timestamp as 'h:mm AM/PM' with no leading zero."""
    return ts.strftime('%I:%M %p').lstrip('0')


def courses_from_pull(pull):
    """Build the Course list from an in-memory pull (no DB query).

    build_courses reads the current Settings thresholds from config each time.
    """
    pat_ids = sorted(pull['sites']['Pat_ID1'].dropna().unique().tolist())
    return build_courses(pull, pat_ids)


def clinic_config_problem():
    """Why clinic_config.toml cannot drive the board, or '' when it can.

    Checked before each live poll so a fresh install explains itself: the file is missing, cannot
    be parsed, or has no [[machines]] block.

    Returns:
        str: The problem for the status banner, or ''.
    """
    path = config.CLINIC_CONFIG_FILE
    if not os.path.exists(path):
        return (f'clinic_config.toml not found -- copy clinic_config.example.toml to {path} and '
                'fill in your treatment rooms')
    try:
        with open(path, 'rb') as f:
            cfg = tomllib.load(f)
    except (tomllib.TOMLDecodeError, OSError) as e:
        return f'clinic_config.toml could not be read ({e}) -- fix the file'
    if not cfg.get('machines'):
        return 'clinic_config.toml has no [[machines]] block -- add one per treatment room'
    return ''


def window_contains(dts, window):
    """True when any non-null timestamp in dts falls on a date inside the [start, end] window"""
    wstart = pd.Timestamp(window[0])
    wend = pd.Timestamp(window[1]) + pd.Timedelta(days=1)
    return any(pd.notna(dt) and wstart <= dt < wend for dt in dts)


def fmt_date(ts):
    """Format a timestamp as ISO 'YYYY-MM-DD', or None when missing."""
    return None if pd.isna(ts) else ts.strftime('%Y-%m-%d')


def save_state(state):
    """Merge the given keys into the state file (STATE_FILE); log and continue on any error."""
    try:
        s = load_state()
        s.update(state)
        with open(STATE_FILE, 'w') as f:
            json.dump(s, f, indent=2)
    except Exception:
        logger.exception('state save failed')


def _zip_uploads(contents, filenames):
    """Pair dcc.Upload contents with filenames into {filename, content} dicts for save_feedback."""
    contents = contents or []
    filenames = filenames or []
    return [dict(filename=n, content=c) for c, n in zip(contents, filenames) if c and n]


def feedback_file():
    """Path for the bug/suggestion feedback log

    Returns:
        str: The config general.feedback_file, else the CRA_FEEDBACK_FILE env var, else
            logs/feedback.log under the data dir.
    """
    path = config.load_config()['general'].get('feedback_file')
    return path or os.environ.get('CRA_FEEDBACK_FILE') or os.path.join(
        config.data_dir(), 'logs', 'feedback.log')


def _slot_flags(machines):
    """Per-slot active flags for the Settings location slots

    A machine is active when its active flag is true or absent; empty slots are inactive. Slot 0
    is forced active when none are, so the dialog always has a tab.

    Args:
        machines (list[dict[str, object]]): Machine config dicts, one per slot in order.

    Returns:
        list[bool]: One flag per slot, MAX_MACHINES long.
    """
    flags = [bool(machines[i].get('active', True)) if i < len(machines) else False
             for i in range(config.MAX_MACHINES)]
    if not any(flags):
        flags[0] = True
    return flags


def _ask_save_path():
    """Save Clinic Config destination from a native save dialog.

    Without a desktop window (browser or headless mode) there is no dialog and the default
    saved_clinic_config_path() is used.

    Returns:
        str | None: The chosen path, or None if the dialog was cancelled.
    """
    import webview
    default = config.saved_clinic_config_path()
    if not webview.windows:
        return default
    picked = webview.windows[0].create_file_dialog(
        webview.FileDialog.SAVE,
        directory=os.path.dirname(default),
        save_filename=os.path.basename(default),
        file_types=('TOML files (*.toml)',))
    return picked[0] if picked else None


def _ask_open_path():
    """Load DB/Clinic Config source from a native open dialog.

    Returns:
        str | None: The chosen path, or None if cancelled or there is no desktop window.
    """
    import webview
    if not webview.windows:
        return None
    picked = webview.windows[0].create_file_dialog(
        webview.FileDialog.OPEN,
        file_types=('TOML files (*.toml)',))
    return picked[0] if picked else None


def _confirm_replace(path):
    """Ask in a native dialog whether to replace an existing config file.

    Win32 MessageBoxW, owned by the foreground (app) window and topmost, so it does not open
    behind the app as pywebview's ownerless create_confirmation_dialog can.

    Args:
        path (str): The file that would be replaced.

    Returns:
        bool: True if the user chose OK.
    """
    import ctypes
    user32 = ctypes.windll.user32

    # MB_OKCANCEL | MB_SETFOREGROUND | MB_TOPMOST; IDOK = 1
    return user32.MessageBoxW(
        user32.GetForegroundWindow(),
        f'{os.path.basename(path)} already exists.\nDo you want to replace it?',
        'Confirm Replace',
        0x1 | 0x10000 | 0x40000) == 1


if __name__ == '__main__':
    run_app(create_app())
