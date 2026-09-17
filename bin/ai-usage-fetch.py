#!/usr/bin/env python3
"""Claude / Codex / Kimi / Antigravity(agy) のレートリミット使用率を取得して
キャッシュ JSON (~/.cache/ai-usage/<provider>.json) に書き出す。

GNOME Shell 拡張はこのキャッシュを読むだけで、ネットワークアクセスはこちらに閉じている。

トークンの扱い:
- Claude / Codex / Antigravity: リフレッシュはしない。ローテーションで本体 CLI の
  認証を壊す恐れがあるため。期限切れや 401 のときはエラー状態を書き出し、
  本体 CLI を一度起動してもらうことで復帰する。
- Kimi: アクセストークンの寿命が 15 分しかないため、期限切れ時は refresh token で
  更新し、ローテートされた新しい refresh token を credentials JSON に書き戻す
  (書き戻さないと CLI 側の保存値と不整合になり両方ともログインが壊れる)。

直前の成功データはエラー時も保持する。
"""

from __future__ import annotations

import base64
import json
import os
import re
import ssl
import stat
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "ai-usage"
TIMEOUT = 15
USER_AGENT = "ai-usage-fetch/1.0"

CLAUDE_CRED_PATH = Path.home() / ".claude" / ".credentials.json"
CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"

CODEX_CRED_PATH = Path.home() / ".codex" / "auth.json"
CODEX_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"

KIMI_CRED_PATH = Path.home() / ".kimi-code" / "credentials" / "kimi-code.json"
KIMI_USAGE_URL = os.environ.get("KIMI_CODE_BASE_URL", "https://api.kimi.com/coding/v1").rstrip("/") + "/usages"
KIMI_TOKEN_URL = "https://auth.kimi.com/api/oauth/token"
KIMI_CLIENT_ID = "17e5f671-d194-4dfb-9706-5516cb48c098"  # public client (secret なし)
KIMI_REFRESH_MARGIN = 120  # 期限切れの何秒前から更新するか

AGY_QUOTA_URLS = [
    "https://daily-cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
    "https://cloudcode-pa.googleapis.com/v1internal:retrieveUserQuotaSummary",
]
AGY_KEYRING_ATTRS = {"service": "gemini", "username": "antigravity"}


class FetchError(Exception):
    """provider, kind, message を保持する取得失敗。"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind
        self.message = message


# ---------------------------------------------------------------- 共通ヘルパー

def cache_path(provider: str) -> Path:
    return CACHE_DIR / f"{provider}.json"


def load_previous(provider: str) -> dict:
    try:
        with cache_path(provider).open(encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def write_cache(provider: str, payload: dict) -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = cache_path(provider).with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, cache_path(provider))


def record_error(provider: str, kind: str, message: str) -> None:
    """直前の成功データを残したままエラー状態を書き出す。"""
    prev = load_previous(provider)
    prev.update({
        "ts": time.time(),
        "ok": False,
        "error": kind,
        "error_message": message,
    })
    write_cache(provider, prev)
    print(f"{provider}: {kind}: {message}", file=sys.stderr)


def http_request(url: str, *, headers: dict | None = None,
                 data: bytes | None = None, method: str | None = None) -> dict:
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": USER_AGENT, **(headers or {})},
        data=data,
        method=method,
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context()) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise FetchError("unauthorized", f"認証が拒否されました (HTTP {e.code})")
        raise FetchError("http_error", f"HTTP {e.code}")
    except urllib.error.URLError as e:
        raise FetchError("network", f"接続できません: {e.reason}")
    except FetchError:
        raise
    except Exception as e:
        raise FetchError("network", str(e))


def to_epoch(value) -> float | None:
    """ISO8601 文字列を epoch 秒に変換する。ナノ秒(小数 7 桁以上)も許容。"""
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        # Python 3.10 の fromisoformat は小数 6 桁までしか読めないので切り詰める
        text = re.sub(r"\.(\d{6})\d+", r".\1", text)
        return datetime.fromisoformat(text).timestamp()
    except Exception:
        return None


def pct_of(used, limit) -> float | None:
    """used / limit (文字列数値を含む) から使用率 % を計算する。"""
    try:
        used_f, limit_f = float(used), float(limit)
    except (TypeError, ValueError):
        return None
    if limit_f <= 0:
        return None
    return used_f / limit_f * 100.0


def base_result() -> dict:
    return {
        "severity": "normal",
        "five_hour": {"pct": None, "resets_at": None},
        "seven_day": {"pct": None, "resets_at": None},
        "scoped": [],
    }


# ---------------------------------------------------------------- Claude

def fetch_claude() -> dict:
    try:
        with CLAUDE_CRED_PATH.open(encoding="utf-8") as f:
            creds = json.load(f)
    except FileNotFoundError:
        raise FetchError("no_credentials", f"{CLAUDE_CRED_PATH} が見つかりません")
    except Exception as e:
        raise FetchError("no_credentials", f"認証情報を読めません: {e}")

    oauth = creds.get("claudeAiOauth") or {}
    token = oauth.get("accessToken")
    if not token:
        raise FetchError("no_credentials", "claudeAiOauth.accessToken がありません（API キー運用中かもしれません）")

    expires_at = oauth.get("expiresAt")
    if isinstance(expires_at, (int, float)) and expires_at / 1000.0 < time.time():
        raise FetchError("token_expired", "アクセストークンの期限が切れています（Claude Code を一度起動すると更新されます）")

    raw = http_request(CLAUDE_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "anthropic-beta": "oauth-2025-04-20",
    })

    result = base_result()
    five = raw.get("five_hour") or {}
    seven = raw.get("seven_day") or {}
    result["five_hour"] = {
        "pct": _pick_utilization(five),
        "resets_at": to_epoch(five.get("resets_at")),
    }
    result["seven_day"] = {
        "pct": _pick_utilization(seven),
        "resets_at": to_epoch(seven.get("resets_at")),
    }

    rank = {"normal": 0, "warning": 1, "critical": 2, "exceeded": 3}
    for item in raw.get("limits") or []:
        if not isinstance(item, dict):
            continue
        sev = item.get("severity")
        if isinstance(sev, str) and rank.get(sev, 0) > rank.get(result["severity"], 0):
            result["severity"] = sev
        if item.get("kind") == "weekly_scoped":
            scope = item.get("scope") or {}
            model = (scope.get("model") or {}).get("display_name")
            label = model or scope.get("surface") or "スコープ"
            pct = item.get("percent")
            if isinstance(pct, (int, float)):
                result["scoped"].append({
                    "label": label,
                    "pct": float(pct),
                    "resets_at": to_epoch(item.get("resets_at")),
                    "window_seconds": 7 * 24 * 60 * 60,
                })

    extra = raw.get("extra_usage") or {}
    result["extra_usage"] = {
        "enabled": bool(extra.get("is_enabled")),
        "pct": extra.get("utilization"),
    }
    return result


def _pick_utilization(block) -> float | None:
    if isinstance(block, dict):
        v = block.get("utilization")
        if isinstance(v, (int, float)):
            return float(v)
    return None


# ---------------------------------------------------------------- Codex

def jwt_payload(token: str) -> dict:
    """JWT の payload を署名検証なしで読む（期限・識別子の参照専用）。"""
    try:
        part = token.split(".")[1]
        part += "=" * (-len(part) % 4)
        payload = json.loads(base64.urlsafe_b64decode(part))
        return payload if isinstance(payload, dict) else {}
    except Exception:
        return {}


def jwt_exp(token: str) -> float | None:
    """JWT の exp クレームを署名検証なしで読む。"""
    exp = jwt_payload(token).get("exp")
    return float(exp) if isinstance(exp, (int, float)) else None


def fetch_codex() -> dict:
    try:
        with CODEX_CRED_PATH.open(encoding="utf-8") as f:
            creds = json.load(f)
    except FileNotFoundError:
        raise FetchError("no_credentials", f"{CODEX_CRED_PATH} が見つかりません")
    except Exception as e:
        raise FetchError("no_credentials", f"認証情報を読めません: {e}")

    tokens = creds.get("tokens") or {}
    token = tokens.get("access_token")
    account_id = tokens.get("account_id")
    if not token or not account_id:
        raise FetchError("no_credentials", "tokens.access_token / account_id がありません（ChatGPT ログインが必要です）")

    exp = jwt_exp(token)
    if exp is not None and exp < time.time():
        raise FetchError("token_expired", "アクセストークンの期限が切れています（codex を一度起動すると更新されます）")

    raw = http_request(CODEX_USAGE_URL, headers={
        "Authorization": f"Bearer {token}",
        "ChatGPT-Account-Id": account_id,
    })

    return parse_codex_usage(raw)


def parse_codex_usage(raw: dict) -> dict:
    """Codex usage 応答を共通キャッシュ形式へ変換する。

    primary_window / secondary_window は時間枠の種類を表す名前ではない。
    契約によって primary だけが週間枠になることもあるため、応答に含まれる
    limit_window_seconds で 5 時間枠と週間枠を判別する。
    """
    rate = raw.get("rate_limit") or {}
    result = base_result()
    result.update(_codex_rate_windows(rate))
    if rate.get("limit_reached"):
        result["severity"] = "exceeded"

    for item in raw.get("additional_rate_limits") or []:
        if not isinstance(item, dict):
            continue
        label = item.get("limit_name") or item.get("metered_feature") or "追加枠"
        extra_rate = item.get("rate_limit") or {}
        windows = _codex_rate_windows(extra_rate)
        for key, suffix, seconds in (
            ("five_hour", "5h", 5 * 60 * 60),
            ("seven_day", "7d", 7 * 24 * 60 * 60),
        ):
            window = windows[key]
            if window["pct"] is not None:
                result["scoped"].append({
                    "label": f"{label} {suffix}",
                    "pct": window["pct"],
                    "resets_at": window["resets_at"],
                    "window_seconds": seconds,
                })
        if extra_rate.get("limit_reached"):
            result["severity"] = "exceeded"
    return result


def _codex_rate_windows(rate: dict) -> dict:
    found = {"five_hour": None, "seven_day": None}
    named = (
        ("primary_window", rate.get("primary_window")),
        ("secondary_window", rate.get("secondary_window")),
    )
    for _name, block in named:
        if not isinstance(block, dict):
            continue
        seconds = block.get("limit_window_seconds")
        if isinstance(seconds, (int, float)):
            if seconds == 5 * 60 * 60:
                found["five_hour"] = block
            elif seconds == 7 * 24 * 60 * 60:
                found["seven_day"] = block

    # 古い応答やモックなど duration を持たない形式との後方互換。
    if all(block is None for block in found.values()):
        found["five_hour"] = rate.get("primary_window")
        found["seven_day"] = rate.get("secondary_window")

    return {
        key: {
            "pct": _pick_used_percent(block or {}),
            "resets_at": _pick_reset_at(block or {}),
        }
        for key, block in found.items()
    }


def _pick_used_percent(block: dict) -> float | None:
    v = block.get("used_percent")
    return float(v) if isinstance(v, (int, float)) else None


def _pick_reset_at(block: dict) -> float | None:
    v = block.get("reset_at")
    return float(v) if isinstance(v, (int, float)) else None


# ---------------------------------------------------------------- Kimi

KIMI_MAX_ACCOUNTS = 3  # 表示スロットは kimi / kimi2 / kimi3


def kimi_cred_paths() -> list[Path]:
    """認証情報ファイルを全部列挙する。

    CLI は (oauthHost, baseUrl) の環境ごとに kimi-code-env-<hash>.json を分けるので、
    複数アカウントを別環境で使い分けていると複数ファイルが存在しうる。
    """
    try:
        paths = sorted(KIMI_CRED_PATH.parent.glob("kimi-code*.json"))
    except Exception:
        paths = []
    if not paths:
        raise FetchError("no_credentials", f"{KIMI_CRED_PATH.parent} に認証情報がありません")
    return paths


def kimi_load_creds(path: Path) -> dict:
    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        raise FetchError("no_credentials", f"{path} が見つかりません")
    except Exception as e:
        raise FetchError("no_credentials", f"認証情報を読めません: {e}")


def kimi_write_creds(path: Path, creds: dict) -> None:
    """ローテートされた refresh token を含む認証情報を原子的に書き戻す。"""
    tmp = path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(creds, f, ensure_ascii=False)
        f.write("\n")
    os.chmod(tmp, stat.S_IRUSR | stat.S_IWUSR)
    os.replace(tmp, path)


def kimi_refresh(path: Path, creds: dict) -> dict:
    refresh_token = creds.get("refresh_token")
    if not refresh_token:
        raise FetchError("no_credentials", "refresh_token がありません（kimi で /login してください）")
    body = urllib.parse.urlencode({
        "client_id": KIMI_CLIENT_ID,
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
    }).encode()
    try:
        raw = http_request(KIMI_TOKEN_URL, data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded",
        })
    except FetchError as e:
        if e.kind == "unauthorized":
            raise FetchError("unauthorized", "refresh token が無効です（kimi で /login し直してください）")
        raise

    creds.update({
        "access_token": raw.get("access_token"),
        "refresh_token": raw.get("refresh_token") or refresh_token,
        "token_type": raw.get("token_type") or creds.get("token_type"),
        "expires_in": raw.get("expires_in"),
        "expires_at": time.time() + float(raw.get("expires_in") or 900),
    })
    if not creds["access_token"]:
        raise FetchError("no_credentials", "リフレッシュ応答に access_token がありません")
    kimi_write_creds(path, creds)
    return creds


def fetch_kimi(path: Path) -> dict:
    creds = kimi_load_creds(path)
    expires_at = creds.get("expires_at")
    if not isinstance(expires_at, (int, float)):
        raise FetchError("no_credentials", "expires_at がありません")
    if expires_at - KIMI_REFRESH_MARGIN < time.time():
        creds = kimi_refresh(path, creds)

    raw = http_request(KIMI_USAGE_URL, headers={
        "Authorization": f"Bearer {creds['access_token']}",
    })

    result = base_result()

    weekly = raw.get("usage") or {}
    result["seven_day"] = {
        "pct": pct_of(weekly.get("used"), weekly.get("limit")),
        "resets_at": to_epoch(weekly.get("resetTime")),
    }

    # ローリング 5 時間枠は window.duration=300 分の limits エントリ
    five = None
    for item in raw.get("limits") or []:
        if not isinstance(item, dict):
            continue
        window = item.get("window") or {}
        if window.get("duration") == 300 and window.get("timeUnit") == "TIME_UNIT_MINUTE":
            five = item.get("detail") or {}
            break
    if five is None and isinstance(raw.get("limits"), list) and raw["limits"]:
        five = (raw["limits"][0] or {}).get("detail") or {}
    if five is not None:
        result["five_hour"] = {
            "pct": pct_of(five.get("used"), five.get("limit")),
            "resets_at": to_epoch(five.get("resetTime")),
        }
    # アカウント識別用（複数アカウント時のスロット割り当てと表示ラベルに使う）
    user = raw.get("user") or {}
    claims = jwt_payload(creds["access_token"])
    # usage 応答が user/userId を返さない場合がある。同じアカウントの認証ファイルを
    # 二重表示しないよう、Kimi の JWT に含まれる安定したユーザー識別子へフォールバックする。
    result["account"] = str(
        user.get("userId") or claims.get("user_id") or claims.get("sub") or ""
    )
    result["cred_file"] = path.name
    return result


# ---------------------------------------------------------------- Antigravity (agy)

def agy_read_token() -> dict:
    """gnome-keyring (Secret Service) にある agy のトークンを読む。"""
    try:
        import secretstorage
    except ImportError:
        raise FetchError("no_credentials", "python3-secretstorage が必要です（agy のトークンはキーリングにあります）")
    try:
        bus = secretstorage.dbus_init()
        collection = secretstorage.get_default_collection(bus)
        if collection.is_locked():
            collection.unlock()
        items = list(collection.search_items(AGY_KEYRING_ATTRS))
    except Exception as e:
        raise FetchError("no_credentials", f"キーリングにアクセスできません: {e}")
    if not items:
        raise FetchError("no_credentials", "キーリングに agy のトークンがありません（agy でログインしてください）")
    try:
        stored = json.loads(items[0].get_secret().decode("utf-8"))
    except Exception as e:
        raise FetchError("no_credentials", f"キーリングのトークンを読めません: {e}")

    token = stored.get("token") or {}
    access_token = token.get("access_token")
    if not access_token:
        raise FetchError("no_credentials", "キーリングのトークンに access_token がありません")
    expiry = to_epoch(token.get("expiry"))
    if expiry is not None and expiry < time.time():
        raise FetchError("token_expired", "アクセストークンの期限が切れています（agy を一度起動すると更新されます）")
    return {"access_token": access_token}


def fetch_agy() -> dict:
    token = agy_read_token()["access_token"]
    body = b"{}"

    raw = None
    last_error = None
    for url in AGY_QUOTA_URLS:
        try:
            raw = http_request(url, data=body, method="POST", headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "antigravity",
            })
            break
        except FetchError as e:
            last_error = e
            if e.kind == "unauthorized":
                e.message += "（agy を一度起動するとトークンが更新されます）"
                raise
    if raw is None:
        raise last_error

    result = base_result()
    groups = [g for g in (raw.get("groups") or []) if isinstance(g, dict)]

    def bucket(group: dict, window: str) -> dict | None:
        for b in group.get("buckets") or []:
            if isinstance(b, dict) and b.get("window") == window:
                return b
        return None

    def bucket_usage(b: dict | None) -> dict:
        if not b:
            return {"pct": None, "resets_at": None}
        frac = b.get("remainingFraction")
        pct = (1.0 - float(frac)) * 100.0 if isinstance(frac, (int, float)) else None
        return {"pct": pct, "resets_at": to_epoch(b.get("resetTime"))}

    # Gemini 系グループをメインの 5h/週間枠、それ以外(Claude/GPT 等)をスコープ枠として出す
    primary_idx = next(
        (i for i, g in enumerate(groups) if "gemini" in str(g.get("displayName", "")).lower()),
        0 if groups else None,
    )
    for i, group in enumerate(groups):
        name = group.get("displayName") or "モデル"
        # セル表示用に短い名前に畳む
        if "claude" in name.lower():
            name = "Claude/GPT"
        five = bucket_usage(bucket(group, "5h"))
        seven = bucket_usage(bucket(group, "weekly"))
        if i == primary_idx:
            result["five_hour"] = five
            result["seven_day"] = seven
        else:
            if five["pct"] is not None:
                result["scoped"].append({
                    "label": f"{name} 5h",
                    "pct": five["pct"],
                    "resets_at": five["resets_at"],
                    "window_seconds": 5 * 60 * 60,
                })
            if seven["pct"] is not None:
                result["scoped"].append({
                    "label": f"{name} 7d",
                    "pct": seven["pct"],
                    "resets_at": seven["resets_at"],
                    "window_seconds": 7 * 24 * 60 * 60,
                })
    return result


# ---------------------------------------------------------------- main

PROVIDERS = {
    "claude": fetch_claude,
    "codex": fetch_codex,
    "agy": fetch_agy,
}

ALL_PROVIDER_NAMES = ["claude", "codex", "kimi", "agy"]


def finish_success(name: str, data: dict) -> None:
    now = time.time()
    data.update({
        "ts": now,
        "fetched_at": now,
        "ok": True,
        "error": None,
        "error_message": None,
    })
    write_cache(name, data)
    five = data["five_hour"]["pct"]
    seven = data["seven_day"]["pct"]
    print(f"{name}: 5h={five}% 7d={seven}%")


def run_provider(name: str, fetch) -> bool:
    try:
        data = fetch()
    except FetchError as e:
        record_error(name, e.kind, e.message)
        return False
    except Exception as e:
        record_error(name, "error", str(e))
        return False
    finish_success(name, data)
    return True


def kimi_cache_ids() -> list[str]:
    return ["kimi"] + [f"kimi{i}" for i in range(2, KIMI_MAX_ACCOUNTS + 1)]


def kimi_slot_for_cred(cred_file: str) -> str | None:
    """前回キャッシュの cred_file と照合して、同じ表示スロットを引き継ぐ。"""
    for pid in kimi_cache_ids():
        if load_previous(pid).get("cred_file") == cred_file:
            return pid
    return None


def run_kimi() -> bool:
    try:
        paths = kimi_cred_paths()
    except FetchError as e:
        record_error("kimi", e.kind, e.message)
        return False

    ok = True
    used: set[str] = set()
    successes = []
    for path in paths[:KIMI_MAX_ACCOUNTS]:
        try:
            successes.append(fetch_kimi(path))
        except FetchError as e:
            ok = False
            slot = kimi_slot_for_cred(path.name) or "kimi"
            record_error(slot, e.kind, e.message)
            used.add(slot)
        except Exception as e:
            ok = False
            slot = kimi_slot_for_cred(path.name) or "kimi"
            record_error(slot, "error", str(e))
            used.add(slot)

    # アカウント ID 順にスロットを固定して、表示の入れ替わりを防ぐ
    successes.sort(key=lambda d: d.get("account") or "")
    # 同一アカウントのファイルが複数あっても表示は 1 つにする
    seen: set[str] = set()
    deduped = []
    for d in successes:
        uid = d.get("account") or d.get("cred_file") or ""
        if uid in seen:
            continue
        seen.add(uid)
        deduped.append(d)
    successes = deduped
    # 認証ファイル数ではなく、重複排除後の実アカウント数で表示名を決める。
    multi = len(successes) > 1
    ids = kimi_cache_ids()
    i = 0
    for data in successes:
        while i < len(ids) and ids[i] in used:
            i += 1
        if i >= len(ids):
            break
        slot = ids[i]
        i += 1
        used.add(slot)
        if multi:
            suffix = (data.get("account") or data.get("cred_file") or "")[-4:]
            data["label"] = f"Kimi (…{suffix})"
            data["short"] = f"K{ids.index(slot) + 1}"
        finish_success(slot, data)

    # 消えたアカウントのキャッシュは掃除する
    for pid in ids:
        if pid not in used:
            try:
                cache_path(pid).unlink(missing_ok=True)
            except Exception:
                pass
    return ok


def main() -> None:
    names = sys.argv[1:] or ALL_PROVIDER_NAMES
    unknown = [n for n in names if n not in ALL_PROVIDER_NAMES]
    if unknown:
        print(f"unknown provider(s): {', '.join(unknown)} (known: {', '.join(ALL_PROVIDER_NAMES)})", file=sys.stderr)
        sys.exit(2)
    results = []
    for name in names:
        if name == "kimi":
            results.append(run_kimi())
        else:
            results.append(run_provider(name, PROVIDERS[name]))
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
