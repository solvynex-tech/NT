/* View shortcuts only: never connect accounts, start strategies or submit orders. */
document.addEventListener('keydown', function (event) {
    if (document.querySelector('.terminal-modal[style*="flex"]')) return;
    if (event.key === 'F1') {
        event.preventDefault();
        const help = document.getElementById('t-help-menu');
        if (help) help.open = !help.open;
        return;
    }
    if (!event.ctrlKey || event.altKey || event.shiftKey || event.metaKey) return;
    const targets = {KeyM:'t-view-market',KeyN:'t-view-navigator',KeyT:'t-view-toolbox',KeyR:'t-view-backtest'};
    const button = document.getElementById(targets[event.code]);
    if (button) {
        event.preventDefault();
        button.click();
    }
});
document.addEventListener('pointerdown', function (event) {
    const parent = event.target.closest('.terminal-menu');
    document.querySelectorAll('.terminal-menu[open]').forEach(function(menu) {
        if (menu !== parent) menu.open = false;
    });
});
document.addEventListener('keydown', function (event) {
    if (event.key === 'Escape') document.querySelectorAll('.terminal-menu[open]').forEach(menu => menu.open=false);
});
document.addEventListener('click', function (event) {
    if (event.target.closest('.terminal-menu-popup button'))
        document.querySelectorAll('.terminal-menu[open]').forEach(menu => menu.open=false);
});
