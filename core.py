"""聚合：搬瓦工 KiwiVM API + SSH 读取 vnstat JSON（供 Windows GUI / exe 使用）。"""

from __future__ import annotations

import json
import os
import shlex
from calendar import monthrange
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import paramiko
import yaml

BWG_API = "https://api.64clouds.com/v1/getServiceInfo"

# GB 换算基数：1024³（常见「GiB」）或 1000³（SI 十进制 GB），每台服务器可在配置里单独指定 gb_base
GB_BASE_BINARY = 1024
GB_BASE_DECIMAL = 1000


def billing_cycle_start_date(today: date, reset_day: int) -> date | None:
    """当前计费周期起始日（重置日当天），与界面「距离重置日 / 已用天数」逻辑一致。"""
    if reset_day < 1:
        return None
    if today.day >= reset_day:
        start_year, start_month = today.year, today.month
    else:
        if today.month == 1:
            start_year, start_month = today.year - 1, 12
        else:
            start_year, start_month = today.year, today.month - 1
    start_day = min(reset_day, monthrange(start_year, start_month)[1])
    return date(start_year, start_month, start_day)


def billing_period_id(today: date, reset_day: int) -> str | None:
    """用于检测计费周期是否已切换（新周期需丢弃上一周期的面板对齐数据）。"""
    start = billing_cycle_start_date(today, reset_day)
    return start.isoformat() if start else None


def apply_billing_period_anchor_resets(
    entries: list[dict[str, Any]],
    *,
    today: date | None = None,
) -> bool:
    """在计费周期切换时清除对齐字段，使「已用」随新周期从 vnstat/API 原始值重新累计。

    依赖每台机器的 ``billing_reset_day``（与 /etc/vnstat.conf 的 MonthRotate 一致为佳）。
    首次写入 ``billing_period_id`` 时不会清空已有 ``used_offset_bytes`` / ``panel_anchor_*``；
    仅当 ``billing_period_id`` 与当前周期不一致时（跨重置日）才清除。

    跨周期时还会清除 ``billing_cycle_baseline_used_bytes`` 并置 ``billing_cycle_needs_baseline``，
    以便在 vnstat 仍显示「整月累计」时，用首次成功拉取后的逻辑已用作为基线，从 0 起算本周期
    （避免「已用天数」已进新周期而「已用流量」仍是旧累计导致日均/预计爆炸）。

    返回是否修改了任意条目（调用方应写回 ``servers.yaml``）。
    """
    if today is None:
        today = date.today()
    dirty = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            reset_day = int(entry.get("billing_reset_day"))
        except (TypeError, ValueError):
            entry.pop("billing_period_id", None)
            entry.pop("billing_cycle_baseline_used_bytes", None)
            entry.pop("billing_cycle_needs_baseline", None)
            continue
        if reset_day < 1:
            entry.pop("billing_period_id", None)
            entry.pop("billing_cycle_baseline_used_bytes", None)
            entry.pop("billing_cycle_needs_baseline", None)
            continue

        key = billing_period_id(today, reset_day)
        if key is None:
            continue

        old = entry.get("billing_period_id")
        if old == key:
            continue

        if old is None or old == "":
            entry["billing_period_id"] = key
            dirty = True
            continue

        for k in (
            "used_offset_bytes",
            "panel_anchor_used_bytes",
            "panel_anchor_raw_bytes",
        ):
            entry.pop(k, None)
        entry.pop("billing_cycle_baseline_used_bytes", None)
        entry["billing_cycle_needs_baseline"] = True
        entry["billing_period_id"] = key
        dirty = True
    return dirty


def _billing_reset_day_int(entry: dict[str, Any]) -> int | None:
    try:
        d = int(entry.get("billing_reset_day"))
    except (TypeError, ValueError):
        return None
    return d if d >= 1 else None


def apply_ssh_billing_cycle_to_row(
    entry: dict[str, Any],
    row: ServerRow,
    raw_vnstat: int | None,
) -> tuple[ServerRow, bool]:
    """在 ``apply_used_offset`` 之后，按计费周期基线压缩 ssh 行的 ``used_bytes`` / ``used_percent``。

    返回 ``(新行, 是否写入了基线等配置)``。
    """
    if not row.ok or row.used_bytes is None:
        return row, False

    reset_day = _billing_reset_day_int(entry)
    if reset_day is None:
        return row, False

    logical = int(row.used_bytes)
    dirty = False

    if entry.get("billing_cycle_needs_baseline"):
        entry.pop("billing_cycle_needs_baseline", None)
        quota_b = row.quota_bytes
        # 晚几天才刷新时：若 vnstat 已与计费月对齐，logical 通常已是「本周期累计」且 ≤ 套餐；
        # 此时不写基线，避免下一次刷新变成 (logical−baseline) 的「增量」而严重低估。
        # 若 logical 仍异常大（常见为 vnstat 仍卡在上一自然月），则写基线并从 0 起算增量。
        # 未配置套餐字节时：一律按「可信累计」展示，勿走基线置零分支。
        if not quota_b or quota_b <= 0 or logical <= quota_b:
            logical_adj = logical
        else:
            entry["billing_cycle_baseline_used_bytes"] = logical
            logical_adj = 0
        dirty = True
    else:
        br = entry.get("billing_cycle_baseline_used_bytes")
        try:
            br_i = int(br) if br not in (None, "") else None
        except (TypeError, ValueError):
            br_i = None
        if br_i is None:
            logical_adj = logical
        else:
            logical_adj = max(0, logical - br_i)

    quota = row.quota_bytes
    pct: float | None = None
    if quota is not None and quota > 0:
        pct = round(logical_adj / quota * 100.0, 2)

    out = replace(
        row,
        used_bytes=logical_adj,
        used_percent=pct,
        raw_used_bytes=raw_vnstat,
    )
    return out, dirty


def entry_gb_base(entry: dict[str, Any]) -> int:
    raw = entry.get("gb_base", GB_BASE_BINARY)
    try:
        b = int(raw)
    except (TypeError, ValueError):
        return GB_BASE_BINARY
    if b not in (GB_BASE_BINARY, GB_BASE_DECIMAL):
        return GB_BASE_BINARY
    return b


@dataclass
class ServerRow:
    id: str
    name: str
    type: str
    ok: bool
    error: str | None
    used_bytes: int | None
    quota_bytes: int | None
    used_percent: float | None
    reset_unix: int | None
    detail: str | None
    # 每台机在编辑里单独配置：界面 GB 用 1000³ 还是 1024³
    gb_base: int = GB_BASE_BINARY  # 与 GB_BASE_BINARY 相同默认值
    # 未加 used_offset_bytes 前的 vnstat/API 原始已用（字节）；供「面板已用」对齐保存时计算偏移
    raw_used_bytes: int | None = None


def load_servers(config_path: Path) -> list[dict[str, Any]]:
    if not config_path.is_file():
        raise FileNotFoundError(f"未找到配置文件: {config_path}")
    with config_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise ValueError("配置根下需要 servers: 列表")
    return servers


def save_servers(config_path: Path, entries: list[dict[str, Any]]) -> None:
    payload = {"servers": entries}
    with config_path.open("w", encoding="utf-8") as f:
        yaml.safe_dump(payload, f, sort_keys=False, allow_unicode=True)


def fetch_bandwagon(veid: str, api_key: str) -> ServerRow:
    params = {"veid": veid, "api_key": api_key}
    try:
        r = httpx.get(BWG_API, params=params, timeout=30.0)
        r.raise_for_status()
        j = r.json()
    except Exception as e:
        return ServerRow(
            id=veid,
            name=veid,
            type="bandwagon",
            ok=False,
            error=str(e),
            used_bytes=None,
            quota_bytes=None,
            used_percent=None,
            reset_unix=None,
            detail=None,
        )

    err = j.get("error")
    if err not in (0, "0", None):
        msg = j.get("message") or j.get("error_message") or str(j)
        return ServerRow(
            id=veid,
            name=str(j.get("hostname") or veid),
            type="bandwagon",
            ok=False,
            error=f"KiwiVM API: {msg}",
            used_bytes=None,
            quota_bytes=None,
            used_percent=None,
            reset_unix=None,
            detail=None,
        )

    mult = float(j.get("monthly_data_multiplier") or 1)
    used = int(float(j.get("data_counter") or 0) * mult)
    cap = int(float(j.get("plan_monthly_data") or 0) * mult)
    reset = j.get("data_next_reset")
    reset_i = int(reset) if reset is not None else None
    pct = (used / cap * 100.0) if cap > 0 else None
    loc = j.get("node_location") or ""
    plan = j.get("plan") or ""
    detail = f"{plan} @ {loc}".strip(" @")

    return ServerRow(
        id=veid,
        name=str(j.get("hostname") or veid),
        type="bandwagon",
        ok=True,
        error=None,
        used_bytes=used,
        quota_bytes=cap if cap > 0 else None,
        used_percent=round(pct, 2) if pct is not None else None,
        reset_unix=reset_i,
        detail=detail or None,
    )


def _vnstat_pick_current_month_row(months: list[Any]) -> dict[str, Any] | None:
    """在 --json m 的月份数组里选出「当前计费月」那一项。

    vnStat 1.x 按槽位 i=0..11 输出，**months[0] 为当前月**，最后一项为最旧月。
    vnStat 2.x 的 ``month`` 数组通常**较新在前**（与官方示例 month[0] 一致）。
    旧实现误用 ``months[-1]``，在 1.x 上会读到**早已结帐的旧月**，字节数几乎不随刷新变化。
    优先用每条里的 ``date.{year,month}`` 取**日历序最大**的一档；无日期时回退 **months[0]**。
    """
    candidates = [m for m in months if isinstance(m, dict)]
    if not candidates:
        return None
    scored: list[tuple[tuple[int, int], dict[str, Any]]] = []
    for m in candidates:
        d = m.get("date")
        if isinstance(d, dict):
            try:
                y = int(d.get("year") or 0)
                mo = int(d.get("month") or 0)
            except (TypeError, ValueError):
                continue
            if y > 0 and mo > 0:
                scored.append(((y, mo), m))
    if scored:
        scored.sort(key=lambda x: x[0], reverse=True)
        return scored[0][1]
    return candidates[0]


def _vnstat_select_interface(payload: dict[str, Any], iface: str) -> tuple[dict[str, Any] | None, str | None]:
    ifaces = payload.get("interfaces")
    if not isinstance(ifaces, list) or not ifaces:
        return None, "JSON 中无 interfaces"
    selected = None
    for it in ifaces:
        if not isinstance(it, dict):
            continue
        name = it.get("name") or it.get("id")
        if name == iface:
            selected = it
            break
    if selected is None:
        selected = ifaces[0] if isinstance(ifaces[0], dict) else None
    if selected is None:
        return None, "无法选择网卡"
    return selected, None


def _vnstat_sum_daily_bytes_in_range(
    payload: dict[str, Any],
    iface: str,
    start: date,
    end: date,
) -> int | None:
    """解析 ``vnstat --json d`` 结果，汇总 ``[start, end]``（含端点）各天的 rx+tx。

    若无法解析日列表则返回 ``None``（由调用方回退到按月统计）。
    """
    selected, _ = _vnstat_select_interface(payload, iface)
    if selected is None:
        return None
    traffic = selected.get("traffic") or {}
    days = traffic.get("days") or traffic.get("day")
    if not isinstance(days, list) or not days:
        return None
    ver = str(payload.get("vnstatversion") or "").strip()
    v1 = ver.startswith("1.")
    total = 0
    matched = False
    for row in days:
        if not isinstance(row, dict):
            continue
        d = row.get("date")
        if not isinstance(d, dict):
            continue
        try:
            y = int(d.get("year") or 0)
            mo = int(d.get("month") or 0)
            da = int(d.get("day") or 0)
        except (TypeError, ValueError):
            continue
        if y <= 0 or mo <= 0 or da <= 0:
            continue
        try:
            di = date(y, mo, da)
        except ValueError:
            continue
        if di < start or di > end:
            continue
        matched = True
        rx = int(row.get("rx") or 0)
        tx = int(row.get("tx") or 0)
        if v1:
            rx *= 1024
            tx *= 1024
        total += rx + tx
    if not matched:
        # 有日表但无一行落入区间：不按 0 覆盖（可能为版本/区间问题），回退按月统计
        return None
    return total


def _vnstat_month_bytes(payload: dict[str, Any], iface: str) -> tuple[int | None, str | None]:
    selected, perr = _vnstat_select_interface(payload, iface)
    if selected is None:
        return None, perr or "无法选择网卡"

    traffic = selected.get("traffic") or {}
    months = traffic.get("months") or traffic.get("month")
    if not isinstance(months, list) or not months:
        return None, "尚无按月数据"

    row = _vnstat_pick_current_month_row(months)
    if row is None:
        return None, "月份数据格式异常"

    rx = int(row.get("rx") or 0)
    tx = int(row.get("tx") or 0)
    # vnStat 1.x 的 JSON 月流量字段是 KiB；2.x 为 bytes。
    # 你的 CentOS 7 常见是 1.15，不换算会显示约小 1024 倍（例如 65.54GiB -> 0.064GB）。
    ver = str(payload.get("vnstatversion") or "").strip()
    if ver.startswith("1."):
        rx *= 1024
        tx *= 1024
    return rx + tx, None


def _vnstat_updated_text(payload: dict[str, Any], iface: str) -> str | None:
    selected, _ = _vnstat_select_interface(payload, iface)
    if not isinstance(selected, dict):
        return None

    updated = selected.get("updated")
    if not isinstance(updated, dict):
        return None
    date = updated.get("date")
    if not isinstance(date, dict):
        return None
    time = updated.get("time")
    if not isinstance(time, dict):
        return None

    try:
        y = int(date.get("year") or 0)
        m = int(date.get("month") or 0)
        d = int(date.get("day") or 0)
        hh = int(time.get("hour") or 0)
        mm = int(time.get("minute") or time.get("minutes") or 0)
    except (TypeError, ValueError):
        return None
    if y <= 0 or m <= 0 or d <= 0:
        return None
    return f"{y:04d}-{m:02d}-{d:02d} {hh:02d}:{mm:02d}"


def fetch_ssh_vnstat(entry: dict[str, Any]) -> ServerRow:
    sid = str(entry.get("id") or entry.get("host"))
    name = str(entry.get("name") or sid)
    host = str(entry.get("host") or "")
    port = int(entry.get("port") or 22)
    user = str(entry.get("username") or "root")
    iface = str(entry.get("interface") or "eth0")
    key_path = entry.get("private_key_path") or entry.get("key_path")
    key_path = str(key_path) if key_path else ""
    password_env = entry.get("password_env")
    password = os.environ.get(str(password_env), "") if password_env else None
    if entry.get("password") and not password:
        password = str(entry.get("password"))

    quota = entry.get("monthly_quota_bytes")
    quota_i = int(quota) if quota is not None else None

    if not host:
        return ServerRow(
            id=sid,
            name=name,
            type="ssh_vnstat",
            ok=False,
            error="缺少 host",
            used_bytes=None,
            quota_bytes=quota_i,
            used_percent=None,
            reset_unix=None,
            detail=f"iface={iface}",
        )

    cmd_m = f"vnstat -i {shlex.quote(iface)} --json m"
    key_file = (
        str(Path(key_path).expanduser())
        if key_path and Path(key_path).expanduser().is_file()
        else None
    )
    kwargs: dict[str, Any] = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": 25,
        "banner_timeout": 20,
    }
    if key_file:
        kwargs["key_filename"] = key_file
    if password:
        kwargs["password"] = password
    if not key_file and not password:
        return ServerRow(
            id=sid,
            name=name,
            type="ssh_vnstat",
            ok=False,
            error="需配置 private_key_path 或 password",
            used_bytes=None,
            quota_bytes=quota_i,
            used_percent=None,
            reset_unix=None,
            detail=f"iface={iface}",
        )

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(**kwargs)
        _, stdout, stderr = client.exec_command(cmd_m)
        out = stdout.read().decode("utf-8", errors="replace")
        err = stderr.read().decode("utf-8", errors="replace")
        if err.strip() and not out.strip():
            return ServerRow(
                id=sid,
                name=name,
                type="ssh_vnstat",
                ok=False,
                error=err.strip()[:500],
                used_bytes=None,
                quota_bytes=quota_i,
                used_percent=None,
                reset_unix=None,
                detail=f"iface={iface}",
            )
        payload = json.loads(out)
        used, perr = _vnstat_month_bytes(payload, iface)
        if used is None:
            return ServerRow(
                id=sid,
                name=name,
                type="ssh_vnstat",
                ok=False,
                error=perr or "解析失败",
                used_bytes=None,
                quota_bytes=quota_i,
                used_percent=None,
                reset_unix=None,
                detail=f"iface={iface}",
            )

        traffic_src = "月"
        rd = _billing_reset_day_int(entry)
        if rd is not None:
            period_start = billing_cycle_start_date(date.today(), rd)
            if period_start is not None:
                cmd_d = (
                    f"vnstat -i {shlex.quote(iface)} --json d "
                    f"--begin {period_start.isoformat()} --end {date.today().isoformat()}"
                )
                _, so2, se2 = client.exec_command(cmd_d)
                o2 = so2.read().decode("utf-8", errors="replace")
                e2 = se2.read().decode("utf-8", errors="replace")
                if o2.strip() and not (e2.strip() and not o2.strip()):
                    try:
                        p2 = json.loads(o2)
                        day_sum = _vnstat_sum_daily_bytes_in_range(
                            p2, iface, period_start, date.today()
                        )
                        if day_sum is not None:
                            used = day_sum
                            traffic_src = "日账期"
                    except (json.JSONDecodeError, ValueError, TypeError, KeyError):
                        pass

        pct = (used / quota_i * 100.0) if quota_i and quota_i > 0 else None
        reset_day = entry.get("billing_reset_day")
        detail = f"iface={iface}({traffic_src})"
        updated = _vnstat_updated_text(payload, iface)
        if updated:
            detail += f", 更新={updated}"
        if reset_day is not None:
            detail += f", 重置≈{reset_day}号"

        return ServerRow(
            id=sid,
            name=name,
            type="ssh_vnstat",
            ok=True,
            error=None,
            used_bytes=used,
            quota_bytes=quota_i,
            used_percent=round(pct, 2) if pct is not None else None,
            reset_unix=None,
            detail=detail,
        )
    except Exception as e:
        return ServerRow(
            id=sid,
            name=name,
            type="ssh_vnstat",
            ok=False,
            error=str(e),
            used_bytes=None,
            quota_bytes=quota_i,
            used_percent=None,
            reset_unix=None,
            detail=f"iface={iface}",
        )
    finally:
        try:
            client.close()
        except Exception:
            pass


def apply_used_offset(row: ServerRow, entry: dict[str, Any]) -> ServerRow:
    """把对齐策略应用到已用字节，并重算占比。

    优先使用「面板锚点」：
      panel_anchor_used_bytes + (raw_now - panel_anchor_raw_bytes)
    兼容旧配置 used_offset_bytes 作为兜底。
    """
    if not row.ok or row.used_bytes is None:
        return row

    new_used: int | None = None
    panel_used = entry.get("panel_anchor_used_bytes")
    panel_raw = entry.get("panel_anchor_raw_bytes")
    if panel_used not in (None, "") and panel_raw not in (None, ""):
        try:
            anchor_used = int(panel_used)
            anchor_raw = int(panel_raw)
            delta = int(row.used_bytes) - anchor_raw
            new_used = anchor_used + delta
        except (TypeError, ValueError):
            new_used = None

    if new_used is None:
        raw = entry.get("used_offset_bytes")
        if raw is None or raw == "":
            return row
        try:
            off = int(raw)
        except (TypeError, ValueError):
            return row
        new_used = row.used_bytes + off

    if new_used < 0:
        new_used = 0
    quota = row.quota_bytes
    pct: float | None = None
    if quota is not None and quota > 0:
        pct = round(new_used / quota * 100.0, 2)
    return replace(row, used_bytes=new_used, used_percent=pct)


def collect_rows(entries: list[dict[str, Any]]) -> tuple[list[ServerRow], bool]:
    rows: list[ServerRow] = []
    entry_dirty = False
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        sid = str(entry.get("id") or "unknown")
        name = str(entry.get("name") or sid)
        stype = str(entry.get("type") or "").lower()

        if stype == "bandwagon":
            veid = str(entry.get("veid") or "")
            api_key = entry.get("api_key")
            api_key_env = entry.get("api_key_env")
            if api_key_env:
                api_key = os.environ.get(str(api_key_env), "")
            else:
                api_key = str(api_key or "")
            if not veid or not api_key:
                rows.append(
                    ServerRow(
                        id=sid,
                        name=name,
                        type="bandwagon",
                        ok=False,
                        error="缺少 veid 或 api_key",
                        used_bytes=None,
                        quota_bytes=None,
                        used_percent=None,
                        reset_unix=None,
                        detail=None,
                        gb_base=entry_gb_base(entry),
                    )
                )
                continue
            r = fetch_bandwagon(veid, api_key)
            r = replace(r, id=sid, name=name, gb_base=entry_gb_base(entry))
            raw = r.used_bytes if r.ok else None
            r = apply_used_offset(r, entry)
            rows.append(replace(r, raw_used_bytes=raw))

        elif stype in ("ssh_vnstat", "vnstat_ssh"):
            r = fetch_ssh_vnstat(entry)
            r = replace(r, gb_base=entry_gb_base(entry))
            raw = r.used_bytes if r.ok else None
            r = apply_used_offset(r, entry)
            r, sub_dirty = apply_ssh_billing_cycle_to_row(entry, r, raw)
            entry_dirty = entry_dirty or sub_dirty
            rows.append(r)

        else:
            rows.append(
                ServerRow(
                    id=sid,
                    name=name,
                    type=stype or "unknown",
                    ok=False,
                    error=f"未知 type: {stype}",
                    used_bytes=None,
                    quota_bytes=None,
                    used_percent=None,
                    reset_unix=None,
                    detail=None,
                    gb_base=entry_gb_base(entry),
                )
            )
    return rows, entry_dirty


def bytes_per_gb(gb_base: int) -> int:
    b = int(gb_base)
    if b not in (GB_BASE_BINARY, GB_BASE_DECIMAL):
        b = GB_BASE_BINARY
    return b**3


def fmt_bytes(n: int | None) -> str:
    """兼容旧逻辑；界面展示请用 fmt_gb。"""
    if n is None:
        return "—"
    for unit, scale in (("TB", 1 << 40), ("GB", 1 << 30), ("MB", 1 << 20)):
        if n >= scale:
            return f"{n / scale:.2f} {unit}"
    return f"{n} B"


def fmt_gb(n: int | None, *, gb_base: int = GB_BASE_BINARY, decimals: int = 2) -> str:
    if n is None:
        return "—"
    denom = bytes_per_gb(gb_base)
    return f"{n / denom:.{decimals}f} GB"


def gb_to_bytes(gb: str, *, gb_base: int = GB_BASE_BINARY) -> int:
    s = gb.strip().replace(",", ".")
    if not s:
        raise ValueError("empty")
    return int(round(float(s) * bytes_per_gb(gb_base)))


def bytes_to_gb_str(n: int, *, gb_base: int = GB_BASE_BINARY) -> str:
    return f"{n / bytes_per_gb(gb_base):.2f}"


def fmt_reset(ts: int | None) -> str:
    if not ts:
        return "—"
    from datetime import datetime

    try:
        return datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M")
    except (OSError, ValueError):
        return "—"
