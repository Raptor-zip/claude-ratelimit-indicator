const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');
const {test} = require('node:test');

const now = 1_800_000_000;
const context = vm.createContext({
    Date: class extends Date { static now() { return now * 1000; } },
    imports: {
        gi: {
            GLib: {build_filenamev: parts => parts.join('/'), get_home_dir: () => '', get_user_cache_dir: () => ''},
            GObject: {registerClass: cls => cls},
        },
        ui: {main: {}, panelMenu: {Button: class {}}, popupMenu: {}},
        misc: {extensionUtils: {getCurrentExtension: () => ({})}},
    },
});
const source = fs.readFileSync(path.join(__dirname, '../extension/ai-ratelimit@raptor-zip.github.io/extension.js'), 'utf8');
vm.runInContext(source + '\nglobalThis.api = {idealUsagePct, paceDelta, formatPace, paceClass, ProviderCell, AiIndicator};', context);
const {idealUsagePct, paceDelta, formatPace, paceClass, ProviderCell, AiIndicator} = context.api;

test('equal usage has opposite pace meaning early and late in a window', () => {
    for (const window of [18000, 604800]) {
        const early = paceDelta(50, idealUsagePct(now + window * 0.75, window));
        const late = paceDelta(50, idealUsagePct(now + window * 0.25, window));
        assert.equal(formatPace(early), '+25');
        assert.equal(paceClass(early), 'ai-usage-crit');
        assert.equal(formatPace(late), '−25');
        assert.equal(paceClass(late), 'ai-usage-ahead');
    }
});

test('unknown and expired resets never imply spare capacity', () => {
    for (const reset of [null, NaN, now, now - 1]) {
        const delta = paceDelta(25, idealUsagePct(reset, 18000));
        assert.equal(delta, null);
        assert.equal(formatPace(delta), '--');
        assert.equal(paceClass(delta), '');
    }
    assert.equal(paceDelta(null, 50), null);
    assert.equal(formatPace(-0.1), '0');
    assert.equal(paceClass(-0.1), '');
    assert.equal(paceClass(1), 'ai-usage-warn');
});

test('providers are colored independently and hidden providers leave no panel entry', () => {
    const labels = new Map(['claude', 'codex', 'kimi'].map(id => [id, {}]));
    const state = delta => ({fiveDelta: null, sevenDelta: delta, fetchedAt: now});
    const indicator = {
        _cells: [
            {provider: {id: 'claude', short: 'Cl'}, render: () => state(25)},
            {provider: {id: 'codex', short: 'Cx'}, render: () => state(-25)},
            {provider: {id: 'kimi', short: 'Km'}, render: () => null},
        ],
        _panelLabels: labels, _label: {}, _statusLabel: {},
        _reflowGrid(cells) { this.visibleCount = cells.length; },
    };
    AiIndicator.prototype._render.call(indicator);
    assert.equal(labels.get('claude').text, 'Cl --/+25');
    assert.match(labels.get('claude').style_class, /ai-usage-crit/);
    assert.equal(labels.get('codex').text, 'Cx --/−25');
    assert.match(labels.get('codex').style_class, /ai-usage-ahead/);
    assert.equal(labels.get('kimi').visible, false);
    assert.equal(indicator.visibleCount, 2);
});

test('logging out hides retained successful data', () => {
    const cell = {box: {}};
    assert.equal(ProviderCell.prototype.render.call(cell, {
        ok: false, error: 'no_credentials', fetched_at: now,
        five_hour: {pct: 25, resets_at: now + 9000},
    }), null);
    assert.equal(cell.box.visible, false);
});
