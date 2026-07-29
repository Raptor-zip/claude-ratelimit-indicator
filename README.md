# Claude Rate Limit — GNOME Shell indicator

Shows your Claude usage limits — the 5-hour session window and the weekly window —
right next to the clock in the GNOME top bar.

```
                    15:24   5h 11% · 7d 24%
```

Click it for a breakdown: usage bars, time until reset, and per-model weekly scopes.
The weekly bar is divided into 7 segments so you can see at a glance whether you are
ahead of or behind a one-day-per-segment pace.

## How it works

| Path | Role |
|---|---|
| `bin/claude-usage-fetch.py` | Calls `https://api.anthropic.com/api/oauth/usage` and writes the cache |
| `systemd/claude-usage.{service,timer}` | Runs the fetcher every 2 minutes |
| `extension/claude-ratelimit@raptor-zip.github.io/` | Reads the cache and draws the indicator |
| `~/.cache/claude-usage/usage.json` | The cache that connects the two |

The fetcher reads the OAuth access token that Claude Code already stores in
`~/.claude/.credentials.json` and calls the same endpoint that backs Claude Code's
`/usage` command. Keeping the network work in a separate process means the GNOME Shell
main loop is never blocked by an HTTP request.

**The fetcher never refreshes the token.** Rotating the refresh token could break
Claude Code's own session, so on an expired token or a 401 the panel dims and the menu
explains why. Starting Claude Code once refreshes the token and the indicator recovers.

## Requirements

- GNOME Shell 42 (tested on Ubuntu 22.04, X11).
  43 and 44 will most likely work — add the version to `shell-version` in
  `metadata.json` and try it. GNOME 45+ needs an ESM port and is not supported yet.
- Python 3.10+ (standard library only)
- Claude Code signed in with a Claude subscription (OAuth). API-key-only setups have no
  usage endpoint and are not supported.

## Install

```bash
git clone https://github.com/Raptor-zip/claude-ratelimit-indicator.git
cd claude-ratelimit-indicator
./install.sh
```

Then activate it:

1. Restart GNOME Shell — `Alt+F2` → `r` → `Enter` (X11 only; on Wayland, log out and back in)
2. `gnome-extensions enable claude-ratelimit@raptor-zip.github.io`

Uninstall with `./install.sh uninstall`.

## What you get

- **Panel**: `5h <session %> · 7d <weekly %>`
  - Yellow at 60%, bold red at 85%
  - Dimmed when the cache is more than 15 minutes old — your cue that the timer stopped
    or the network is down
- **Menu**:
  - Session (5 hours): usage bar, time remaining, reset time
  - Weekly: same, with 7 division marks (one per day)
  - Per-model weekly scopes, when the API reports any
  - Last-updated time and a **Refresh now** button (opening the menu also triggers a fetch)

## Configuration

Change the polling interval by editing `OnUnitInactiveSec` in
`~/.config/systemd/user/claude-usage.timer`, then:

```bash
systemctl --user daemon-reload && systemctl --user restart claude-usage.timer
```

Thresholds (`WARN_PCT` / `CRIT_PCT`) and the stale cutoff (`STALE_SECONDS`) are constants
at the top of `extension.js`.

## Troubleshooting

```bash
~/.local/bin/claude-usage-fetch                  # fetch once, print the result
cat ~/.cache/claude-usage/usage.json             # what the extension reads
journalctl --user -u claude-usage.service -n 20  # fetcher logs
journalctl --user -f -o cat /usr/bin/gnome-shell # extension errors
```

After editing the extension, re-run `./install.sh` and restart GNOME Shell.

## Privacy

The token is read locally and sent only to `api.anthropic.com` in the `Authorization`
header. The cache file holds nothing but percentages, reset timestamps and an error
string — no token, no prompts, no conversation data.

## License

MIT

---

# 日本語

Claude のレートリミット（5 時間セッション枠 / 週間枠）の使用率を、GNOME トップバーの
時計の横に表示する GNOME Shell 42 拡張機能です。

```
                    15:24   5h 11% · 7d 24%
```

クリックすると、使用率バー・リセットまでの残り時間・モデル別の週間スコープ枠が開きます。
週間バーは 7 分割の目安線入りで、1 日あたりのペースに対して先行しているか遅れているかが
ひと目で分かります。

## 仕組み

Claude Code が `~/.claude/.credentials.json` に保存している OAuth アクセストークンを読み、
Claude Code の `/usage` と同じエンドポイント（`https://api.anthropic.com/api/oauth/usage`）を
叩きます。取得は systemd user timer で 2 分ごとに別プロセスとして走り、結果を
`~/.cache/claude-usage/usage.json` に書きます。拡張はそのキャッシュを読むだけなので、
GNOME Shell のメインループが HTTP でブロックされることはありません。

**トークンのリフレッシュはしません。** refresh token をローテートすると Claude Code 本体の
認証を壊す恐れがあるためです。期限切れや 401 のときはパネルが淡色になり、メニューに理由が
表示されます。Claude Code を一度起動すればトークンが更新されて復帰します。

## 動作要件

- GNOME Shell 42（Ubuntu 22.04 / X11 で動作確認）。
  43・44 でもおそらく動きますが未検証です（`metadata.json` の `shell-version` に追記すれば
  試せます）。GNOME 45 以降は ESM への移植が必要で未対応です。
- Python 3.10 以降（標準ライブラリのみ）
- Claude サブスクリプションで Claude Code にログイン済みであること（OAuth）。
  API キーのみの構成では usage エンドポイントが無いため利用できません。

## インストール

```bash
git clone https://github.com/Raptor-zip/claude-ratelimit-indicator.git
cd claude-ratelimit-indicator
./install.sh
```

そのあと有効化します:

1. `Alt+F2` → `r` → `Enter` で GNOME Shell を再起動（X11 のみ。Wayland は再ログイン）
2. `gnome-extensions enable claude-ratelimit@raptor-zip.github.io`

アンインストールは `./install.sh uninstall` です。

## 表示

- **パネル**: `5h <5時間枠の%> · 7d <週間枠の%>`
  - 60% 以上で黄色、85% 以上で赤太字
  - キャッシュが 15 分以上更新されないと淡色（タイマー停止・ネットワーク断の目印）
- **メニュー**:
  - セッション（5 時間）: 使用率バー、残り時間、リセット時刻
  - 週間: 同上（7 分割の目安線つき）
  - 週間のモデル別スコープ枠（API が返す場合のみ）
  - 最終更新時刻と「今すぐ更新」ボタン（メニューを開いた時点でも取得が走ります）

## 設定

更新間隔は `~/.config/systemd/user/claude-usage.timer` の `OnUnitInactiveSec` を編集して:

```bash
systemctl --user daemon-reload && systemctl --user restart claude-usage.timer
```

色のしきい値（`WARN_PCT` / `CRIT_PCT`）と「古い」と見なす秒数（`STALE_SECONDS`）は
`extension.js` 冒頭の定数です。

## 困ったとき

```bash
~/.local/bin/claude-usage-fetch                  # 手動で取得して結果を表示
cat ~/.cache/claude-usage/usage.json             # 拡張が読んでいる中身
journalctl --user -u claude-usage.service -n 20  # 取得スクリプトのログ
journalctl --user -f -o cat /usr/bin/gnome-shell # 拡張のエラー
```

拡張を編集したら `./install.sh` を再実行して GNOME Shell を再起動してください。

## プライバシー

トークンはローカルで読まれ、`api.anthropic.com` への `Authorization` ヘッダー以外には
送られません。キャッシュファイルに入るのは使用率・リセット時刻・エラー文字列だけで、
トークンやプロンプト、会話の内容は含まれません。

## ライセンス

MIT
