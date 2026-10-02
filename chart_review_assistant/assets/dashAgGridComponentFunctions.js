var dagfuncs = (window.dashAgGridComponentFunctions = window.dashAgGridComponentFunctions || {});

// Renders a cell tooltip whose value is an HTML string (bold headings + bulleted lists),
// matching the report pane. The message text is app-generated.
dagfuncs.HtmlTooltip = function (props) {
    return React.createElement('div', {
        className: 'cr-tooltip',
        dangerouslySetInnerHTML: {__html: props.value || ''},
    });
};

// Installs the Hide external-filter functions on the grid: the filter is present while
// window.__craHide is on, and a row passes (stays visible) unless its Hide checkbox is checked.
// Shared by the header toggle and the rebuild re-apply clientside callback in app.py.
dagfuncs.installHideFilter = function (api) {
    api.setGridOption('isExternalFilterPresent',
        function () { return window.__craHide === true; });
    api.setGridOption('doesExternalFilterPass',
        function (node) { return !(node && node.data && node.data.hide); });
};

// Hide-column header: two stacked buttons over the checkboxes. 'Hide Rows'/'Unhide Rows' flips the
// external-filter flag (window.__craHide), (re)installs the filter functions, and re-runs the
// filter; its label follows the state. 'Clear Checks' unchecks every row (via the hidden hide_clear
// proxy button). Both are disabled while no row is checked, since nothing can then be hidden or
// cleared. Replaces the plain 'Hide' header text.
dagfuncs.HideHeader = function (props) {
    var st = React.useState({applied: window.__craHide === true, checked: false});
    var state = st[0], setState = st[1];

    // Track whether any row is checked so the buttons enable/disable. forEachNode visits every row
    // (including ones currently filtered out while hiding is on), so an Unhide stays available.
    React.useEffect(function () {
        var api;
        function recount() {
            var any = false;
            if (api) {
                api.forEachNode(function (n) { if (n.data && n.data.hide) { any = true; } });
            }
            setState(function (s) { return {applied: window.__craHide === true, checked: any}; });
        }
        window.dash_ag_grid.getApiAsync('table').then(function (a) {
            api = a;
            api.addEventListener('cellValueChanged', recount);
            api.addEventListener('modelUpdated', recount);
            recount();
        });
        return function () {
            if (api) {
                api.removeEventListener('cellValueChanged', recount);
                api.removeEventListener('modelUpdated', recount);
            }
        };
    }, []);
    async function toggle(e) {
        e.stopPropagation();
        var next = !(window.__craHide === true);
        window.__craHide = next;
        var api = await window.dash_ag_grid.getApiAsync('table');
        if (api) {
            dagfuncs.installHideFilter(api);
            api.onFilterChanged();
        }
        window.dash_clientside.set_props('hide_applied', {data: next});
        setState(function (s) { return {applied: next, checked: s.checked}; });
    }
    function clear(e) {
        e.stopPropagation();
        var b = document.getElementById('hide_clear');
        if (b) { b.click(); }
    }
    return React.createElement('div', {className: 'cr-hide-hdr'},
        React.createElement('button', {
            onClick: toggle, className: 'cr-hide-btn', disabled: !state.checked,
            title: 'Check rows, then toggle to remove them from view.',
        }, state.applied ? 'Unhide Rows' : 'Hide Rows'),
        React.createElement('button', {
            onClick: clear, className: 'cr-hide-btn', disabled: !state.checked,
            title: 'Uncheck every row.',
        }, 'Clear Checks')
    );
};

// Severity marker elements for a field's __sev code ('i'/'w'/'e' per present level), rendered in
// order info, warning, error; shared by CopyCell, SevCell and PendingCell
dagfuncs.sevMarks = function (sev) {
    var marks = [
        {k: 'i', glyph: 'ℹ', color: '#2563eb', bold: false, size: '1.5em'},
        {k: 'w', glyph: '⚠', color: '#e67e22', bold: false, size: '1.5em'},
        {k: 'e', glyph: '!', color: '#c0392b', bold: true, size: '1.5em'},
    ];
    var els = [];
    marks.forEach(function (m) {
        if (sev.indexOf(m.k) !== -1) {
            els.push(React.createElement('span', {
                key: m.k,
                style: {color: m.color, fontWeight: m.bold ? 700 : 400, marginLeft: '4px',
                        fontSize: m.size},
            }, m.glyph));
        }
    });
    return els;
};

// A cell's severity code (its field's __sev, '' when absent) and display text (the formatted
// value, else the raw value, as a string); shared by CopyCell, SevCell and PendingCell
dagfuncs.cellParts = function (props) {
    var field = props.colDef && props.colDef.field;
    var text = props.valueFormatted != null ? props.valueFormatted : props.value;
    return {
        sev: (props.data && field) ? (props.data[field + '__sev'] || '') : '',
        text: text == null ? '' : String(text),
    };
};

// Click-to-copy cell (used for the MRN column). Renders the value plus any severity markers
// exactly like SevCell, but a single click copies the raw value to the clipboard and briefly
// flashes 'Copied' -- the crypto-wallet address pattern. Falls back to execCommand where the async
// clipboard API is unavailable.
dagfuncs.CopyCell = function (props) {
    var st = React.useState(false);
    var copied = st[0], setCopied = st[1];
    var parts = dagfuncs.cellParts(props);
    var raw = props.value == null ? '' : String(props.value);
    function flash() {
        setCopied(true);
        window.setTimeout(function () { setCopied(false); }, 900);
    }
    function copy() {
        if (!raw) { return; }
        if (navigator.clipboard && navigator.clipboard.writeText) {
            navigator.clipboard.writeText(raw).then(flash, function () {});
        } else {
            var ta = document.createElement('textarea');
            ta.value = raw;
            document.body.appendChild(ta);
            ta.select();
            try { document.execCommand('copy'); flash(); } catch (e) {}
            document.body.removeChild(ta);
        }
    }
    var kids = [parts.text].concat(dagfuncs.sevMarks(parts.sev));
    if (copied) {
        kids.push(React.createElement('span', {
            key: '_copied',
            style: {marginLeft: '6px', color: '#16a34a', fontSize: '0.85em'},
        }, 'Copied'));
    }
    return React.createElement('span', {
        onClick: copy,
        title: 'Click to copy',
        style: {cursor: 'pointer'},
    }, kids);
};

// Renders the cell value followed by any severity markers for that field, in order
// info, warning, error. The field's __sev code holds the present levels as 'i'/'w'/'e'.
dagfuncs.SevCell = function (props) {
    var parts = dagfuncs.cellParts(props);
    var kids = [parts.text].concat(dagfuncs.sevMarks(parts.sev));
    return React.createElement('span', null, kids);
};

// Pending QCLs cell. Splits the "<date> - <label>" value on its ' - ' and lays the two parts in
// equal flex halves so the separator sits at the cell's horizontal center on every row: the date
// right-aligns into the axis, the label left-aligns out of it, and the dashes form a centered
// vertical line down the column. Severity markers ride inside the right half so they do not shift
// the axis. Falls back to a plain span when the value has no separator.
dagfuncs.PendingCell = function (props) {
    var parts = dagfuncs.cellParts(props);
    var text = parts.text;
    var markEls = dagfuncs.sevMarks(parts.sev);
    var i = text.indexOf(' - ');
    if (i < 0) {
        return React.createElement('span', null, [text].concat(markEls));
    }
    return React.createElement('div',
        {style: {display: 'flex', width: '100%', alignItems: 'center'}},
        React.createElement('span', {style: {flex: '1 1 0', textAlign: 'right'}},
            text.substring(0, i)),
        React.createElement('span', {style: {flex: '0 0 auto', padding: '0 4px'}}, '-'),
        React.createElement('span', {style: {flex: '1 1 0', textAlign: 'left'}},
            [text.substring(i + 3)].concat(markEls))
    );
};
