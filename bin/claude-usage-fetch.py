#!/usr/bin/env python3
"""Claude のレートリミット使用量を取得してキャッシュ JSON に書き出す。

Claude Code が保存している OAuth アクセストークンを使って
https://api.anthropic.com/api/oauth/usage を叩き、
GNOME Shell 拡張が読む ~/.cache/claude-usage/usage.json を更新する。

トークンのリフレッシュは行わない（Claude Code 本体の管理を壊さないため）。
期限切れ / 401 の場合はエラー状態を書き出し、直前の成功データは保持する。
"""

from __future__ import annotations

import json
import os
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

CRED_PATH = Path.home() / ".claude" / ".credentials.json"
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "claude-usage"
CACHE_PATH = CACHE_DIR / "usage.json"
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
TIMEOUT = 15


def load_previous() -> dict:
    try:
        with CACHE_PATH.open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_cache(payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, CACHE_PATH)


def fail(kind: str, message: str) -> None:
    """直前の成功データを残したままエラー状態を書き出して終了する。"""
    prev = load_previous()
    prev.update({
        "ts": time.time(),
        "ok": False,
        "error": kind,
        "error_message": message,
    })
    write_cache(prev)
    print(f"{kind}: {message}", file=sys.stderr)
    sys.exit(1)


def read_token() -> str:
    try:
        with CRED_PATH.open(encoding="utf-8") as f:
            creds = json.load(f)
    except FileNotFoundError:
        fail("no_credentials", f"{CRED_PATH} が見つかりません")
    except Exception as e:  # 壊れた JSON など
        fail("no_credentials", f"認証情報を読めません: {e}")

    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        fail("no_credentials", "claudeAiOauth.accessToken がありません（API キー運用中かもしれません）")

    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 < time.time():
        fail("token_expired", "アクセストークンの期限が切れています（Claude Code を一度起動すると更新されます）")

    return token


def to_epoch(value) -> float | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def fetch(token: str) -> dict:
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "claude-ratelimit-indicator/1.0",
        },
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            fail("unauthorized", f"認証が拒否されました (HTTP {e.code})。Claude Code で再ログインしてください")
        fail("http_error", f"HTTP {e.code}")
    except urllib.error.URLError as e:
        fail("network", f"接続できません: {e.reason}")
    except Exception as e:
        fail("network", str(e))


def pick_pct(block) -> float | None:
    if isinstance(block, dict):
        v = block.get("utilization")
        if isinstance(v, (int, float)):
            return float(v)
    return None


def normalize(raw: dict) -> dict:
    five = raw.get("five_hour") or {}
    seven = raw.get("seven_day") or {}
    limits = raw.get("limits") or []

    severity = "normal"
    rank = {"normal": 0, "warning": 1, "critical": 2, "exceeded": 3}
    scoped = []
    for item in limits:
        if not isinstance(item, dict):
            continue
        sev = item.get("severity")
        if isinstance(sev, str) and rank.get(sev, 0) > rank.get(severity, 0):
            severity = sev
        if item.get("kind") == "weekly_scoped":
            scope = item.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            surface = scope.get("surface")
            label = model or surface or "スコープ"
            pct = item.get("percent")
            if isinstance(pct, (int, float)):
                scoped.append({
                    "label": label,
                    "pct": float(pct),
                    "resets_at": to_epoch(item.get("resets_at")),
                    "active": bool(item.get("is_active")),
                })

    extra = raw.get("extra_usage") or {}
    now = time.time()
    return {
        "ts": now,
        "fetched_at": now,
        "ok": True,
        "error": None,
        "error_message": None,
        "severity": severity,
        "five_hour": {
            "pct": pick_pct(five),
            "resets_at": to_epoch(five.get("resets_at")),
        },
        "seven_day": {
            "pct": pick_pct(seven),
            "resets_at": to_epoch(seven.get("resets_at")),
        },
        "scoped": scoped,
        "extra_usage": {
            "enabled": bool(extra.get("is_enabled")),
            "pct": extra.get("utilization"),
        },
    }


def main() -> None:
    token = read_token()
    raw = fetch(token)
    data = normalize(raw)
    write_cache(data)
    five = data["five_hour"]["pct"]
    seven = data["seven_day"]["pct"]
    print(f"5h={five}% 7d={seven}% severity={data['severity']}")


if __name__ == "__main__":
    main()
