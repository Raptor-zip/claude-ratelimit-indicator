/* Claude Rate Limit — GNOME Shell 42 用インジケーター
 *
 * ~/.cache/claude-usage/usage.json（systemd timer が定期更新）を読んで、
 * トップバーの時計の横に Claude の使用率を表示する。
 */
'use strict';

const { Clutter, Gio, GLib, GObject, St } = imports.gi;

const Main = imports.ui.main;
const PanelMenu = imports.ui.panelMenu;
const PopupMenu = imports.ui.popupMenu;

const ExtensionUtils = imports.misc.extensionUtils;
const Me = ExtensionUtils.getCurrentExtension();

const CACHE_PATH = GLib.build_filenamev([
    GLib.get_user_cache_dir(), 'claude-usage', 'usage.json',
]);
// install.sh がここへ取得スクリプトを置く（リポジトリの場所に依存させない）
const FETCH_SCRIPT = GLib.build_filenamev([
    GLib.get_home_dir(), '.local', 'bin', 'claude-usage-fetch',
]);

// 表示の再描画間隔（残り時間のカウントダウン用）
const TICK_SECONDS = 30;
// この秒数を超えて更新がないキャッシュは「古い」扱い
const STALE_SECONDS = 15 * 60;

const WARN_PCT = 60;
const CRIT_PCT = 85;

const WEEKDAYS = ['日', '月', '火', '水', '木', '金', '土'];

function decodeBytes(bytes) {
    if (typeof TextDecoder !== 'undefined')
        return new TextDecoder('utf-8').decode(bytes);
    return imports.byteArray.toString(bytes);
}

function readCache() {
    try {
        const [ok, contents] = GLib.file_get_contents(CACHE_PATH);
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

function severityClass(pct) {
    if (typeof pct !== 'number' || !isFinite(pct))
        return '';
    if (pct >= CRIT_PCT)
        return 'claude-usage-crit';
    if (pct >= WARN_PCT)
        return 'claude-usage-warn';
    return '';
}

function formatRemaining(epochSeconds) {
    if (!epochSeconds)
        return null;
    const diff = epochSeconds - Date.now() / 1000;
    if (diff <= 0)
        return 'まもなくリセット';
    const totalMinutes = Math.floor(diff / 60);
    const days = Math.floor(totalMinutes / (60 * 24));
    const hours = Math.floor((totalMinutes % (60 * 24)) / 60);
    const minutes = totalMinutes % 60;
    if (days > 0)
        return `あと ${days}日${hours}時間`;
    if (hours > 0)
        return `あと ${hours}時間${minutes}分`;
    return `あと ${minutes}分`;
}

function formatResetAt(epochSeconds) {
    if (!epochSeconds)
        return null;
    const d = new Date(epochSeconds * 1000);
    const now = new Date();
    const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`;
    const sameDay = d.getFullYear() === now.getFullYear() &&
        d.getMonth() === now.getMonth() &&
        d.getDate() === now.getDate();
    if (sameDay)
        return hhmm;
    return `${d.getMonth() + 1}/${d.getDate()}(${WEEKDAYS[d.getDay()]}) ${hhmm}`;
}

function formatClock(epochSeconds) {
    if (!epochSeconds)
        return '--:--';
    const d = new Date(epochSeconds * 1000);
    return `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}:${String(d.getSeconds()).padStart(2, '0')}`;
}

/* メニュー内の 1 行：ラベル + パーセント + バー + 補足 */
const UsageRow = GObject.registerClass(
class UsageRow extends PopupMenu.PopupBaseMenuItem {
    _init(title) {
        super._init({ reactive: false, can_focus: false });

        const box = new St.BoxLayout({
            vertical: true,
            x_expand: true,
            style_class: 'claude-usage-row',
        });

        const header = new St.BoxLayout({ x_expand: true });
        this._title = new St.Label({
            text: title,
            x_expand: true,
            style_class: 'claude-usage-row-title',
        });
        this._pct = new St.Label({ style_class: 'claude-usage-row-pct' });
        header.add_child(this._title);
        header.add_child(this._pct);

        this._track = new St.Widget({
            style_class: 'claude-usage-bar-track',
            layout_manager: new Clutter.BinLayout(),
            x_expand: true,
        });

        // 塗りは BoxLayout に入れて必ず左端から伸ばす
        // （BinLayout に直接入れると幅指定した子が中央に寄る）
        this._fillBox = new St.BoxLayout({
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });
        this._fill = new St.Widget({
            style_class: 'claude-usage-bar-fill',
            x_expand: false,
            y_expand: true,
        });
        this._fillBox.add_child(this._fill);
        this._track.add_child(this._fillBox);

        this._ticks = null;

        this._sub = new St.Label({ style_class: 'claude-usage-row-sub' });

        box.add_child(header);
        box.add_child(this._track);
        box.add_child(this._sub);
        this.add_child(box);
    }

    setTitle(text) {
        this._title.text = text;
    }

    /* バーを count 等分する区切り線を重ねる（週間枠の 1 日分の目安用） */
    setDivisions(count) {
        if (this._ticks) {
            this._ticks.destroy();
            this._ticks = null;
        }
        if (!count || count < 2)
            return;

        this._ticks = new St.BoxLayout({
            x_expand: true,
            y_expand: true,
            x_align: Clutter.ActorAlign.FILL,
            y_align: Clutter.ActorAlign.FILL,
        });
        for (let i = 0; i < count; i++) {
            this._ticks.add_child(new St.Widget({
                x_expand: true,
                y_expand: true,
                // 最後の区画の右端はトラックの縁なので線を引かない
                style_class: i < count - 1 ? 'claude-usage-tick' : '',
            }));
        }
        // 塗りより後に足して上に重ねる
        this._track.add_child(this._ticks);
    }

    update(pct, subtitle) {
        this._pct.text = formatPct(pct);

        const cls = severityClass(pct);
        this._pct.style_class = `claude-usage-row-pct ${cls}`.trim();

        const ratio = typeof pct === 'number' && isFinite(pct)
            ? Math.max(0, Math.min(100, pct)) : 0;
        // トラック幅は stylesheet.css で固定しているので割合をそのまま px に換算
        this._fill.style = `width: ${Math.max(ratio * 2.4, ratio > 0 ? 3 : 0)}px;`;
        this._fill.style_class = `claude-usage-bar-fill ${cls}`.trim();

        this._sub.text = subtitle || '';
        this._sub.visible = !!subtitle;
    }
});

const ClaudeIndicator = GObject.registerClass(
class ClaudeIndicator extends PanelMenu.Button {
    _init() {
        super._init(0.5, 'Claude Rate Limit', false);

        this._label = new St.Label({
            y_align: Clutter.ActorAlign.CENTER,
            style_class: 'claude-usage-panel-label',
        });
        this.add_child(this._label);

        this._sessionRow = new UsageRow('セッション（5時間）');
        this._weeklyRow = new UsageRow('週間');
        // 週間枠は 7 等分＝1 日分の目安が分かるように区切る
        this._weeklyRow.setDivisions(7);
        this.menu.addMenuItem(this._sessionRow);
        this.menu.addMenuItem(this._weeklyRow);

        this._scopedRows = [];
        this._scopedSection = new PopupMenu.PopupMenuSection();
        this.menu.addMenuItem(this._scopedSection);

        this.menu.addMenuItem(new PopupMenu.PopupSeparatorMenuItem());

        this._statusItem = new PopupMenu.PopupMenuItem('', {
            reactive: false,
            can_focus: false,
            style_class: 'claude-usage-status',
        });
        this.menu.addMenuItem(this._statusItem);

        this._refreshItem = new PopupMenu.PopupMenuItem('今すぐ更新');
        this._refreshItem.connect('activate', () => this._runFetch());
        this.menu.addMenuItem(this._refreshItem);

        this.menu.connect('open-state-changed', (menu, isOpen) => {
            if (isOpen) {
                this._render();
                this._runFetch();
            }
        });

        this._startMonitor();
        this._startTicker();
        this._render();
        this._runFetch();
    }

    _startMonitor() {
        try {
            const file = Gio.File.new_for_path(CACHE_PATH);
            this._monitor = file.monitor_file(Gio.FileMonitorFlags.NONE, null);
            this._monitorId = this._monitor.connect('changed', () => {
                // 書き込み途中を拾わないよう少しだけ遅らせる
                if (this._debounceId)
                    GLib.source_remove(this._debounceId);
                this._debounceId = GLib.timeout_add(GLib.PRIORITY_DEFAULT, 300, () => {
                    this._debounceId = 0;
                    this._render();
                    return GLib.SOURCE_REMOVE;
                });
            });
        } catch (e) {
            logError(e, 'claude-ratelimit: ファイル監視を開始できません');
        }
    }

    _startTicker() {
        this._tickId = GLib.timeout_add_seconds(GLib.PRIORITY_DEFAULT, TICK_SECONDS, () => {
            this._render();
            return GLib.SOURCE_CONTINUE;
        });
    }

    _runFetch() {
        if (this._fetching)
            return;
        if (!GLib.file_test(FETCH_SCRIPT, GLib.FileTest.IS_EXECUTABLE))
            return;
        this._fetching = true;
        try {
            const proc = Gio.Subprocess.new(
                [FETCH_SCRIPT],
                Gio.SubprocessFlags.STDOUT_SILENCE | Gio.SubprocessFlags.STDERR_SILENCE
            );
            proc.wait_async(null, () => {
                this._fetching = false;
                this._render();
            });
        } catch (e) {
            this._fetching = false;
            logError(e, 'claude-ratelimit: 取得スクリプトを起動できません');
        }
    }

    _updateScopedRows(scoped) {
        const items = (scoped || []).filter(s => s && typeof s.pct === 'number');

        while (this._scopedRows.length > items.length) {
            const row = this._scopedRows.pop();
            row.destroy();
        }
        while (this._scopedRows.length < items.length) {
            const row = new UsageRow('');
            row.setDivisions(7);
            this._scopedSection.addMenuItem(row);
            this._scopedRows.push(row);
        }

        items.forEach((item, i) => {
            const row = this._scopedRows[i];
            row.setTitle(`週間（${item.label}）`);
            const reset = formatResetAt(item.resets_at);
            row.update(item.pct, reset ? `リセット ${reset}` : '');
        });
    }

    _render() {
        const data = readCache();

        if (!data) {
            this._label.text = 'Claude —';
            this._label.style_class = 'claude-usage-panel-label claude-usage-stale';
            this._sessionRow.update(null, '');
            this._weeklyRow.update(null, '');
            this._updateScopedRows([]);
            this._statusItem.label.text = 'まだ取得されていません';
            return;
        }

        const five = (data.five_hour || {}).pct;
        const seven = (data.seven_day || {}).pct;
        const worst = Math.max(
            typeof five === 'number' ? five : 0,
            typeof seven === 'number' ? seven : 0
        );

        const stale = !data.fetched_at ||
            (Date.now() / 1000 - data.fetched_at) > STALE_SECONDS;
        const classes = ['claude-usage-panel-label'];
        const sev = severityClass(worst);
        if (sev)
            classes.push(sev);
        if (stale || data.ok === false)
            classes.push('claude-usage-stale');

        if (data.ok === false && !data.fetched_at) {
            this._label.text = 'Claude ⚠';
        } else {
            this._label.text = `5h ${formatPct(five)} · 7d ${formatPct(seven)}`;
        }
        this._label.style_class = classes.join(' ');

        const fiveReset = formatResetAt((data.five_hour || {}).resets_at);
        const fiveLeft = formatRemaining((data.five_hour || {}).resets_at);
        this._sessionRow.update(
            five,
            [fiveLeft, fiveReset ? `リセット ${fiveReset}` : null].filter(x => x).join(' · ')
        );

        const sevenReset = formatResetAt((data.seven_day || {}).resets_at);
        const sevenLeft = formatRemaining((data.seven_day || {}).resets_at);
        this._weeklyRow.update(
            seven,
            [sevenLeft, sevenReset ? `リセット ${sevenReset}` : null].filter(x => x).join(' · ')
        );

        this._updateScopedRows(data.scoped);

        const parts = [];
        if (data.ok === false)
            parts.push(`⚠ ${data.error_message || data.error || '取得に失敗しました'}`);
        parts.push(`最終更新 ${formatClock(data.fetched_at)}`);
        if (stale && data.ok !== false)
            parts.push('（更新が停止しているかもしれません）');
        this._statusItem.label.text = parts.join('　');
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
        if (this._monitor) {
            if (this._monitorId)
                this._monitor.disconnect(this._monitorId);
            this._monitor.cancel();
            this._monitor = null;
        }
        super.destroy();
    }
});

class Extension {
    constructor(uuid) {
        this._uuid = uuid;
        this._indicator = null;
    }

    enable() {
        this._indicator = new ClaudeIndicator();
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
