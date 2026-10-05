/* Observe dock geometry only. Native data, orders and replay clocks stay server-side. */
(() => {
    const graphIds = ['t-chart', 't-equity', 't-surface', 't-term', 't-combo-pnl'];
    const sizes = new WeakMap();
    let timer, visibleKey, dock, resizeObserver, mutationObserver;
    const visible = node => node && node.getClientRects().length &&
        getComputedStyle(node).visibility !== 'hidden' && node.clientWidth > 0 && node.clientHeight > 0;

    function settle() {
        const active = [];
        for (const id of graphIds) {
            const graph = document.getElementById(id);
            if (!visible(graph)) continue;
            active.push(id);
            const plot = graph.querySelector('.js-plotly-plot');
            if (!plot || !plot._fullLayout || !window.Plotly) continue;
            const rect = plot.getBoundingClientRect();
            const size = Math.round(rect.width) + ':' + Math.round(rect.height);
            const wrongSize = Math.abs(plot._fullLayout.width - rect.width) > 2 ||
                Math.abs(plot._fullLayout.height - rect.height) > 2;
            if (sizes.get(plot) !== size || wrongSize) {
                sizes.set(plot, size);
                // Plotly's public resize API redraws axes/canvas at the actual dock size.
                window.Plotly.Fx.unhover(plot);
                window.Plotly.Plots.resize(plot).catch(() => {});
            }
        }
        const key = active.join('|');
        if (key !== visibleKey && window.dash_clientside?.set_props) {
            visibleKey = key;
            window.dash_clientside.set_props('t-visible-graphs', {data:active});
        }
    }

    function schedule() {
        clearTimeout(timer);
        timer = setTimeout(() => requestAnimationFrame(settle), 160);
    }

    function attach() {
        const next = document.getElementById('t-dock');
        if (!next || next === dock) return;
        resizeObserver?.disconnect();
        mutationObserver?.disconnect();
        dock = next;
        resizeObserver = new ResizeObserver(schedule);
        resizeObserver.observe(dock);
        graphIds.forEach(id => {
            const graph = document.getElementById(id);
            if (graph) resizeObserver.observe(graph);
        });
        // Only FlexLayout container styles indicate visibility/geometry changes.
        // Ignore Plotly hover/axis mutations to avoid a redraw feedback loop.
        mutationObserver = new MutationObserver(records => {
            if (records.some(record => record.target.classList?.contains('flexlayout__tab') ||
                record.target === dock || record.type === 'childList' &&
                [...record.addedNodes].some(node => node.classList?.contains('flexlayout__tab')))) schedule();
        });
        mutationObserver.observe(dock, {subtree:true, attributes:true, attributeFilter:['style'], childList:true});
        schedule();
    }

    const mounting = new MutationObserver(attach);
    mounting.observe(document.documentElement, {childList:true, subtree:true});
    window.addEventListener('resize', schedule);
    window.addEventListener('pagehide', () => {
        clearTimeout(timer);
        mounting.disconnect();
        resizeObserver?.disconnect();
        mutationObserver?.disconnect();
    }, {once:true});
    attach();
})();
