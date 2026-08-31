/* AI Rate Limit — GNOME Shell 42 用インジケーター
 *
 * ~/.cache/ai-usage/<provider>.json（systemd timer が定期更新）を読んで、
 * トップバーの時計の横に各 AI CLI の使用率を 1 つのパネルアイテムにまとめて表示する。
 * メニューは 2x2 グリッドのコンパクト表示。
 * 対応プロバイダ: Claude / Codex / Kimi / Antigravity (agy)
 */
'use strict';

const { Clutter, Gio, GLib, GObject, St } = imports.gi;

const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;

const ExtensionUtils = imports.misc.extensionUtils;
const Me = ExtensionUtils.getCurrentExtension();

// 表示するプロバイダ。name はメニュー表示名、short はパネルの短縮ラベル。
// kimi は複数アカウント（別環境の credentials ファイル）があると kimi2 / kimi3 に分かれて出る
const PROVIDERS = [
    { id: 'claude', name: 'Claude', short: 'Cl' },
    { id: 'codex', name: 'Codex', short: 'Cx' },
    { id: 'kimi', name: 'Kimi', short: 'Km' },
    { id: 'kimi2', name: 'Kimi (2)', short: 'K2' },
    { id: 'kimi3', name: 'Kimi (3)', short: 'K3' },
    { id: 'agy', name: 'Antigravity', short: 'Ag' },
];

// メニューのグリッド列数（2 = 2x2）
const GRID_COLUMNS = 2;

function cachePath(providerId) {
    return GLib.build_filenamev([
        GLib.get_user_cache_dir(), 'ai-usage', `${providerId}.json`,
    ]);
}

// install.sh がここへ取得スクリプトを置く（リポジトリの場所に依存させない）
const FETCH_SCRIPT = GLib.build_filenamev([
    GLib.get_home_dir(), '.local', 'bin', 'ai-usage-fetch',
]);

// 表示の再描画間隔（残り時間のカウントダウン用）
const TICK_SECONDS = 30;
// この秒数を超えて更新がないキャッシュは「古い」扱い
const STALE_SECONDS = 15 * 60;

const WARN_PCT = 60;
const CRIT_PCT = 85;

// セルの補足行・エラー行はこの文字数で切り詰める（メニューが伸びすぎないように）
const SUB_MAX_CHARS = 40;

function decodeBytes(bytes) {
    if (typeof TextDecoder !== 'undefined')
        return new TextDecoder('utf-8').decode(bytes);
    return imports.byteArray.toString(bytes);
}

function readCache(providerId) {
    try {
        const [ok, contents] = GLib.file_get_contents(cachePath(providerId));
        if (!ok)
            return null;
        return JSON.parse(decodeBytes(contents));
    } catch (e) {
        return null;
    }
}

function formatPct(pct) {
    if (typeof pct !== 'number' || !isFinite(pct))
        return '--%';
    return `${Math.round(pct)}%`;
}

/* パネル用の短い表記（% なし、-- はデータ無し） */
function formatPctShort(pct) {
    if (typeof pct !== 'number' || !isFinite(pct))
        return '--';
    return `${Math.round(pct)}`;
}

function severityClass(pct) {
    if (typeof pct !== 'number' || !isFinite(pct))
        return '';
    if (pct >= CRIT_PCT)
        return 'ai-usage-crit';
    if (pct >= WARN_PCT)
        return 'ai-usage-warn';
    return '';
}

function truncate(text, maxChars) {
    if (!text)
        return '';
    return text.length > maxChars ? `${text.slice(0, maxChars - 1)}…` : text;
}

/* リセットまでの残り時間を短く（あと 2日3時間 / あと 4時間10分 / あと 5分） */
function formatRemaining(epochSeconds) {
    if (!epochSeconds)
        return null;
    const diff = epochSeconds - Date.now() / 1000;
    if (diff <= 0)
        return 'まもなく';
    const totalMinutes = Math.floor(diff / 60);
    const days = Math.floor(totalMinutes / (60 * 24));
    const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
    const minutes = totalMinutes % 60;
    if (days > 0)
        return `${days}日${hours}時間`;
    if (hours > 0)
        return `${hours}時間${minutes}分`;
    return `${minutes}分`;
}

function formatClock(epochSeconds) {
    if (!epochSeconds)
        return '--:--';
    const d = new Date(epochSeconds * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
}

/* 小さな使用率バー（divisions > 1 で等分の目安線つき） */
class MiniBar {
    constructor(divisions) {
        this._track = new St.Widget({
            style_class: 'ai-mini-bar-track',
            layout_manager: new Clutter.BinLayout(),
            y_align: Clutter.ActorAlign.CENTER,
        });

        // 塗りは BoxLayout に入れて必ず左端から伸ばす
        const fillBox = new St.BoxLayout({
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });
        this._fill = new St.Widget({
            style_class: 'ai-mini-bar-fill',
            x_expand: false,
            y_expand: true,
        });
        fillBox.add_child(this._fill);
        this._track.add_child(fillBox);

        if (divisions > 1) {
            const ticks = new St.BoxLayout({
                x_expand: true,
                y_expand: true,
                x_align: Clutter.ActorAlign.FILL,
                y_align: Clutter.ActorAlign.FILL,
            });
            for (let i = 0; i < divisions; i++) {
                ticks.add_child(new St.Widget({ x_expand: true, y_expand: true }));
                if (i < divisions - 1) {
                    ticks.add_child(new St.Widget({
                        style_class: 'ai-usage-tick',
                        x_expand: false,
                        y_expand: true,
                    }));
                }
            }
            // 塗りより後に足して上に重ねる
            this._track.add_child(ticks);
        }
    }

    get actor() {
        return this._track;
    }

    setPct(pct) {
        const ratio = typeof pct === 'number' && isFinite(pct)
            ? Math.max(0, Math.min(100, pct)) : 0;
        // トラック幅は stylesheet.css で固定（110px）
        this._fill.style = `width: ${Math.max(ratio * 1.1, ratio > 0 ? 2 : 0)}px;`;
        this._fill.style_class = `ai-mini-bar-fill ${severityClass(pct)}`.trim();
    }
}

/* 1 プロバイダ分のグリッドセル（名前 + 5h/7d バー行 + スコープ行 + 補足行） */
class ProviderCell {
    constructor(provider) {
        this.provider = provider;
        this.box = new St.BoxLayout({
            vertical: true,
            style_class: 'ai-provider-cell',
        });

        this._nameLabel = new St.Label({
            text: provider.name,
            style_class: 'ai-provider-name',
            x_expand: true,
        });
        this.box.add_child(this._nameLabel);

        this._fiveRow = this._makeBarRow('5h', 0);
        this._sevenRow = this._makeBarRow('7d', 7);

        this._scopedRows = [];

        this._sub = new St.Label({ style_class: 'ai-cell-sub' });
        this.box.add_child(this._sub);
    }

    _makeBarRow(label, divisions) {
        const box = new St.BoxLayout({ style_class: 'ai-bar-row' });
        const lab = new St.Label({
            text: label,
            style_class: 'ai-bar-label',
            x_expand: true,
            y_align: Clutter.ActorAlign.CENTER,
        });
        const bar = new MiniBar(divisions);
        const pct = new St.Label({
            style_class: 'ai-bar-pct',
            y_align: Clutter.ActorAlign.CENTER,
        });
        box.add_child(lab);
        box.add_child(bar.actor);
        box.add_child(pct);
        this.box.add_child(box);
        return { box, lab, bar, pct };
    }

    _setBarRow(row, pct) {
        row.pct.text = formatPct(pct);
        row.pct.style_class = `ai-bar-pct ${severityClass(pct)}`.trim();
        row.bar.setPct(pct);
    }

    _updateScopedRows(scoped) {
        const items = (scoped || []).filter(s => s && typeof s.pct === 'number');

        while (this._scopedRows.length > items.length) {
            const row = this._scopedRows.pop();
            row.box.destroy();
        }
        while (this._scopedRows.length < items.length)
            this._scopedRows.push(this._makeBarRow('', 0));

        items.forEach((item, i) => {
            const row = this._scopedRows[i];
            row.lab.text = truncate(item.label, 16);
            this._setBarRow(row, item.pct);
        });
    }

    /* data はキャッシュ JSON。null = 未取得。戻り値はパネル描画用の状態 */
    render(data) {
        this.box.visible = !!data;
        if (!data)
            return null;

        // キャッシュ側のラベル（複数アカウントの Kimi など）があればそれを使う
        this._nameLabel.text = data.label || this.provider.name;

        const five = (data.five_hour || {}).pct;
        const seven = (data.seven_day || {}).pct;
        const stale = !data.fetched_at ||
            (Date.now() / 1000 - data.fetched_at) > STALE_SECONDS;

        this._setBarRow(this._fiveRow, five);
        this._setBarRow(this._sevenRow, seven);
        this._updateScopedRows(data.scoped);

        if (data.ok === false) {
            this._sub.text = truncate(
                `⚠ ${data.error_message || data.error || '取得に失敗しました'}`,
                SUB_MAX_CHARS
            );
            this._sub.style_class = 'ai-cell-sub ai-cell-error';
        } else {
            const fiveLeft = formatRemaining((data.five_hour || {}).resets_at);
            const sevenLeft = formatRemaining((data.seven_day || {}).resets_at);
            const parts = [];
            if (fiveLeft)
                parts.push(`5h あと${fiveLeft}`);
            if (sevenLeft)
                parts.push(`7d あと${sevenLeft}`);
            if (stale)
                parts.push('更新停止?');
            this._sub.text = parts.join(' · ');
            this._sub.style_class = 'ai-cell-sub';
        }

        return {
            five,
            seven,
            stale,
            failed: data.ok === false,
            hasData: !!data.fetched_at,
            fetchedAt: data.fetched_at || 0,
            short: data.short || null,
        };
    }
}

// 取得スクリプトの多重起動を防ぐ共通フラグ
let _fetching = false;

function runFetch(onDone) {
    if (_fetching)
        return;
    if (!GLib.file_test(FETCH_SCRIPT, GLib.FileTest.IS_EXECUTABLE))
        return;
    _fetching = true;
    try {
        const proc = Gio.Subprocess.new(
            [FETCH_SCRIPT],
            Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
        );
        proc.wait_async(null, () => {
            _fetching = false;
            if (onDone)
                onDone();
        });
    } catch (e) {
        _fetching = false;
        logError(e, 'ai-ratelimit: 取得スクリプトを起動できません');
    }
}

const AiIndicator = GObject.registerClass(
class AiIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'AI Rate Limit', false);

        this._label = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'ai-usage-panel-label',
        });
        this.add_child(this._label);

        // 2x2 グリッドに並べるプロバイダセル
        this._grid = new St.Widget({
            layout_manager: new Clutter.GridLayout({ orientation: Clutter.Orientation.VERTICAL }),
            style_class: 'ai-provider-grid',
        });
        const gridItem = new PopupMenu.PopupBaseMenuItem({
            reactive: false,
            can_focus: false,
            style_class: 'ai-provider-grid-item',
        });
        gridItem.add_child(this._grid);
        this.menu.addMenuItem(gridItem);

        this._cells = PROVIDERS.map((provider, i) => {
            const cell = new ProviderCell(provider);
            this._grid.layout_manager.attach(
                cell.box,
                i % GRID_COLUMNS, Math.floor(i / GRID_COLUMNS), 1, 1
            );
            return cell;
        });

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._statusItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
            can_focus: false,
            style_class: 'ai-usage-status',
        });
        this.menu.addMenuItem(this._statusItem);

        this._refreshItem = new PopupMenu.PopupMenuItem('今すぐ更新');
        this._refreshItem.connect('activate', () => runFetch(() => this._render()));
        this.menu.addMenuItem(this._refreshItem);

        this.menu.connect('open-state-changed', (menu, isOpen) => {
            if (isOpen) {
                this._render();
                runFetch(() => this._render());
            }
        });

        this._startMonitors();
        this._startTicker();
        this._render();
        runFetch(() => this._render());
    }

    _startMonitors() {
        this._monitors = [];
        for (const provider of PROVIDERS) {
            try {
                const file = Gio.File.new_for_path(cachePath(provider.id));
                const monitor = file.monitor_file(Gio.FileMonitorFlags.NONE, null);
                const monitorId = monitor.connect('changed', () => {
                    // 書き込み途中を拾わないよう少しだけ遅らせる
                    if (this._debounceId)
                        GLib.source_remove(this._debounceId);
                    this._debounceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
                        this._debounceId = 0;
                        this._render();
                        return GLib.SOURCE_REMOVE;
                    });
                });
                this._monitors.push({ monitor, monitorId });
            } catch (e) {
                logError(e, 'ai-ratelimit: ファイル監視を開始できません');
            }
        }
    }

    _startTicker() {
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            this._render();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _render() {
        // パネル: 取得できているプロバイダだけを `Cl 51/99`（5h/7d）形式で並べる
        const parts = [];
        let worst = 0;
        let anyStale = false;
        let anyFailed = false;
        let latestFetch = 0;

        for (const cell of this._cells) {
            const state = cell.render(readCache(cell.provider.id));
            if (!state)
                continue;

            const { five, seven } = state;
            worst = Math.max(
                worst,
                typeof five === 'number' ? five : 0,
                typeof seven === 'number' ? seven : 0
            );
            anyStale = anyStale || state.stale;
            anyFailed = anyFailed || (state.failed && !state.hasData);
            latestFetch = Math.max(latestFetch, state.fetchedAt);

            const short = state.short || cell.provider.short;
            if (state.failed && !state.hasData) {
                parts.push(`${short} ⚠`);
            } else {
                parts.push(`${short} ${formatPctShort(five)}/${formatPctShort(seven)}`);
            }
        }

        this._label.text = parts.length ? parts.join('  ') : 'AI —';

        const classes = ['ai-usage-panel-label'];
        const sev = severityClass(worst);
        if (sev)
            classes.push(sev);
        if (anyStale || anyFailed || !parts.length)
            classes.push('ai-usage-stale');
        this._label.style_class = classes.join(' ');

        this._statusItem.label.text = parts.length
            ? `最終更新 ${formatClock(latestFetch)}`
            : 'まだ取得されていません';
    }

    destroy() {
        if (this._tickId) {
            GLib.source_remove(this._tickId);
            this._tickId = 0;
        }
        if (this._debounceId) {
            GLib.source_remove(this._debounceId);
            this._debounceId = 0;
        }
        for (const { monitor, monitorId } of this._monitors || []) {
            if (monitorId)
                monitor.disconnect(monitorId);
            monitor.cancel();
        }
        this._monitors = [];
        super.destroy();
    }
});

class Extension {
    constructor(uuid) {
        this._uuid = uuid;
        this._indicator = null;
    }

    enable() {
        this._indicator = new AiIndicator();
        // 時計（dateMenu）のすぐ右に置く
        Main.panel.addToStatusArea(this._uuid, this._indicator, 1, 'center');
    }

    disable() {
        if (this._indicator) {
            this._indicator.destroy();
            this._indicator = null;
        }
    }
}

function init(meta) {
    return new Extension(meta.uuid);
}
