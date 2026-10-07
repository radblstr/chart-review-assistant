var dagf = (window.dashAgGridFunctions = window.dashAgGridFunctions || {});

// Column comparators. dash-ag-grid evaluates a column def's {'function': ...} string with its own
// restricted expression walker, not eval, so the comparators live here as real functions and
// app.py references them by name.

// Pending QCLs: ISO-date-prefixed strings sort chronologically; blanks (no pending QCL) last.
dagf.pendingCmp = function (a, b) {
    return (!a && !b) ? 0
        : !a ? 1
        : !b ? -1
        : a < b ? -1
        : (a > b ? 1 : 0);
};

// Count and 'x/y' columns: numerator then denominator numerically; blanks and N/A last in both
// directions (AG Grid negates the result when descending, so the blank side flips with it).
dagf.fracCmp = function (a, b, nodeA, nodeB, isDescending) {
    var pa = String(a == null ? '' : a).split('/').map(Number);
    var pb = String(b == null ? '' : b).split('/').map(Number);
    var na = a == null || a === '' || isNaN(pa[0]);
    var nb = b == null || b === '' || isNaN(pb[0]);
    if (na || nb) {
        if (na === nb) return 0;
        var last = isDescending ? -1 : 1;
        return na ? last : -last;
    }
    return (pa[0] - pb[0]) || ((pa[1] || 0) - (pb[1] || 0));
};

// ICC/FCC completion columns: rows whose check is due (info notification on the column's __sev,
// no date yet) first, then by completion date, blanks last. AG Grid passes the row nodes, so the
// sibling __sev field is read off node.data.
dagf.ccCmp = function (field) {
    function rank(v, node) {
        var sev = (node && node.data && node.data[field + '__sev']) || '';
        return sev.indexOf('i') >= 0 ? 0 : (v ? 1 : 2);
    }
    return function (a, b, nodeA, nodeB) {
        var ra = rank(a, nodeA);
        var rb = rank(b, nodeB);
        if (ra !== rb) return ra - rb;
        return a < b ? -1 : (a > b ? 1 : 0);
    };
};
