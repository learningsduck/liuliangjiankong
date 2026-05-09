"""VPS 流量汇总（Windows 桌面版）"""

from __future__ import annotations

import sys
import threading
from dataclasses import replace
from calendar import monthrange
from datetime import date
from datetime import datetime
from pathlib import Path
import tkinter as tk
from tkinter import messagebox, ttk

from core import (
    GB_BASE_BINARY,
    GB_BASE_DECIMAL,
    ServerRow,
    apply_billing_period_anchor_resets,
    apply_ssh_billing_cycle_to_row,
    apply_used_offset,
    billing_cycle_start_date,
    bytes_to_gb_str,
    collect_rows,
    entry_gb_base,
    fetch_ssh_vnstat,
    fmt_gb,
    fmt_reset,
    gb_to_bytes,
    load_servers,
    save_servers,
)


def app_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


class ServerDialog:
    def __init__(
        self,
        parent: tk.Tk,
        initial: dict | None = None,
        *,
        current_used_bytes: int | None = None,
        current_raw_used_bytes: int | None = None,
    ):
        self.result: dict | None = None
        self._initial = initial or {}
        self._initial_gb = entry_gb_base(self._initial)
        self.current_used_bytes = current_used_bytes
        self.current_raw_used_bytes = current_raw_used_bytes
        self.win = tk.Toplevel(parent)
        self.win.title("服务器配置")
        self.win.transient(parent)
        self.win.grab_set()
        self.win.resizable(False, False)
        frm = ttk.Frame(self.win, padding=10)
        frm.grid(row=0, column=0, sticky="nsew")

        self.var_id = tk.StringVar(value=str(self._initial.get("id", "")))
        self.var_name = tk.StringVar(value=str(self._initial.get("name", "")))
        self.var_type = tk.StringVar(value=str(self._initial.get("type", "ssh_vnstat")))
        self.var_host = tk.StringVar(value=str(self._initial.get("host", "")))
        self.var_port = tk.StringVar(value=str(self._initial.get("port", 22)))
        self.var_user = tk.StringVar(value=str(self._initial.get("username", "root")))
        self.var_key = tk.StringVar(value=str(self._initial.get("private_key_path", "")))
        self.var_iface = tk.StringVar(value=str(self._initial.get("interface", "eth0")))
        qbytes = self._initial.get("monthly_quota_bytes")
        if qbytes is not None and str(qbytes).strip() != "":
            try:
                qtxt = bytes_to_gb_str(int(qbytes), gb_base=self._initial_gb)
            except (TypeError, ValueError):
                qtxt = ""
        else:
            qtxt = ""
        self.var_quota = tk.StringVar(value=qtxt)
        self.var_reset = tk.StringVar(value=str(self._initial.get("billing_reset_day", "")))
        self.var_panel_used = tk.StringVar(value="")
        self.var_veid = tk.StringVar(value=str(self._initial.get("veid", "")))
        self.var_api = tk.StringVar(value=str(self._initial.get("api_key", "")))

        row = 0
        for label, var in (("ID", self.var_id), ("名称", self.var_name)):
            ttk.Label(frm, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(frm, textvariable=var, width=40).grid(row=row, column=1, sticky="we", pady=3)
            row += 1

        ttk.Label(frm, text="类型").grid(row=row, column=0, sticky="w", pady=3)
        type_box = ttk.Combobox(
            frm,
            textvariable=self.var_type,
            values=("ssh_vnstat", "bandwagon"),
            state="readonly",
            width=37,
        )
        type_box.grid(row=row, column=1, sticky="we", pady=3)
        row += 1

        ttk.Label(frm, text="GB进制").grid(row=row, column=0, sticky="w", pady=3)
        self.var_gb_mode = tk.StringVar(
            value="GB: 1000³" if self._initial_gb == GB_BASE_DECIMAL else "GB: 1024³"
        )
        ttk.Combobox(
            frm,
            textvariable=self.var_gb_mode,
            values=("GB: 1024³", "GB: 1000³"),
            state="readonly",
            width=37,
        ).grid(row=row, column=1, sticky="we", pady=3)
        row += 1

        self.ssh_frame = ttk.LabelFrame(frm, text="SSH + vnstat", padding=8)
        self.ssh_frame.grid(row=row, column=0, columnspan=2, sticky="we", pady=(6, 3))
        row += 1

        self._add_field(self.ssh_frame, "主机", self.var_host, 0)
        self._add_field(self.ssh_frame, "端口", self.var_port, 1)
        self._add_field(self.ssh_frame, "用户名", self.var_user, 2)
        self._add_field(self.ssh_frame, "私钥路径", self.var_key, 3)
        self._add_field(self.ssh_frame, "网卡", self.var_iface, 4)
        self._add_field(self.ssh_frame, "月流量上限(GB)", self.var_quota, 5)
        self._add_field(self.ssh_frame, "面板已用(GB)", self.var_panel_used, 6)
        ttk.Label(
            self.ssh_frame,
            text="填商家面板「本月已用」；保存时自动对齐到主页。请先刷新列表再编辑。留空则不对齐。",
            font=("TkDefaultFont", 8),
            foreground="gray",
        ).grid(row=7, column=0, columnspan=2, sticky="w", padx=4)
        self._add_field(self.ssh_frame, "重置日(可选)", self.var_reset, 8)

        self.bwg_frame = ttk.LabelFrame(frm, text="搬瓦工 API", padding=8)
        self.bwg_frame.grid(row=row, column=0, columnspan=2, sticky="we", pady=(6, 3))
        row += 1
        self._add_field(self.bwg_frame, "VEID", self.var_veid, 0)
        self._add_field(self.bwg_frame, "API Key", self.var_api, 1)
        self._add_field(self.bwg_frame, "面板已用(GB)", self.var_panel_used, 2)
        ttk.Label(
            self.bwg_frame,
            text="若 API 已用与面板不一致，填面板「本月已用」后保存即可对齐。请先刷新列表。留空则不对齐。",
            font=("TkDefaultFont", 8),
            foreground="gray",
        ).grid(row=3, column=0, columnspan=2, sticky="w", padx=4)

        btns = ttk.Frame(frm)
        btns.grid(row=row, column=0, columnspan=2, sticky="e", pady=(8, 0))
        ttk.Button(btns, text="取消", command=self.win.destroy).pack(side=tk.RIGHT, padx=4)
        ttk.Button(btns, text="保存", command=self.on_save).pack(side=tk.RIGHT)

        self.var_type.trace_add("write", lambda *_: self.update_visibility())
        self.update_visibility()
        if current_used_bytes is not None:
            self.var_panel_used.set(
                bytes_to_gb_str(current_used_bytes, gb_base=self._initial_gb)
            )
        self.win.wait_window()

    @staticmethod
    def _add_field(parent: ttk.Frame, label: str, var: tk.StringVar, row: int) -> None:
        ttk.Label(parent, text=label).grid(row=row, column=0, sticky="w", pady=3)
        ttk.Entry(parent, textvariable=var, width=34).grid(row=row, column=1, sticky="we", pady=3)

    def dialog_gb_base(self) -> int:
        return (
            GB_BASE_DECIMAL
            if self.var_gb_mode.get().startswith("GB: 1000")
            else GB_BASE_BINARY
        )

    def update_visibility(self) -> None:
        t = self.var_type.get()
        if t == "bandwagon":
            self.ssh_frame.grid_remove()
            self.bwg_frame.grid()
        else:
            self.bwg_frame.grid_remove()
            self.ssh_frame.grid()

    def on_save(self) -> None:
        sid = self.var_id.get().strip()
        name = self.var_name.get().strip() or sid
        stype = self.var_type.get().strip()
        if not sid:
            messagebox.showwarning("提示", "ID 不能为空", parent=self.win)
            return
        if stype not in ("ssh_vnstat", "bandwagon"):
            messagebox.showwarning("提示", "类型无效", parent=self.win)
            return

        entry: dict[str, object] = {"id": sid, "name": name, "type": stype}
        if stype == "bandwagon":
            veid = self.var_veid.get().strip()
            api_key = self.var_api.get().strip()
            if not veid or not api_key:
                messagebox.showwarning("提示", "bandwagon 需要 veid 和 api_key", parent=self.win)
                return
            entry.update({"veid": veid, "api_key": api_key})
        else:
            host = self.var_host.get().strip()
            if not host:
                messagebox.showwarning("提示", "主机不能为空", parent=self.win)
                return
            entry.update(
                {
                    "host": host,
                    "port": int(self.var_port.get().strip() or "22"),
                    "username": self.var_user.get().strip() or "root",
                    "private_key_path": self.var_key.get().strip(),
                    "interface": self.var_iface.get().strip() or "eth0",
                }
            )
            quota = self.var_quota.get().strip()
            if quota:
                try:
                    entry["monthly_quota_bytes"] = gb_to_bytes(quota, gb_base=self.dialog_gb_base())
                except ValueError:
                    messagebox.showwarning(
                        "提示",
                        "月流量上限请输入数字（GB），例如 3000 或 1024.5",
                        parent=self.win,
                    )
                    return
            reset = self.var_reset.get().strip()
            if reset:
                entry["billing_reset_day"] = int(reset)

        gb_b = self.dialog_gb_base()
        entry["gb_base"] = gb_b
        merged: dict[str, object] = {**self._initial, **entry}

        panel_s = self.var_panel_used.get().strip()
        if panel_s:
            if self.current_raw_used_bytes is None:
                messagebox.showwarning(
                    "提示",
                    "填写面板已用前请先在主页点「刷新」，成功拉取流量后再保存。",
                    parent=self.win,
                )
                return
            try:
                panel_bytes = gb_to_bytes(panel_s, gb_base=gb_b)
            except ValueError:
                messagebox.showwarning(
                    "提示",
                    "面板已用请输入数字（GB），可为小数",
                    parent=self.win,
                )
                return
            merged["panel_anchor_used_bytes"] = int(panel_bytes)
            merged["panel_anchor_raw_bytes"] = int(self.current_raw_used_bytes)
            # 迁移到锚点模式后不再使用旧偏移字段
            merged.pop("used_offset_bytes", None)
        else:
            merged.pop("panel_anchor_used_bytes", None)
            merged.pop("panel_anchor_raw_bytes", None)
            merged.pop("used_offset_bytes", None)

        self.result = merged
        self.win.destroy()


class App:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.path = app_dir() / "servers.yaml"
        self.entries: list[dict] = []
        self.refresh_job: str | None = None
        self.refreshing = False
        self.last_rows: list[ServerRow] | None = None

        root.title("VPS 流量汇总")
        root.geometry("1020x500")
        root.minsize(840, 360)

        top = ttk.Frame(root, padding=8)
        top.pack(fill=tk.X)

        self.btn_refresh = ttk.Button(top, text="刷新", command=lambda: self.refresh(False))
        self.btn_refresh.pack(side=tk.LEFT, padx=(0, 5))
        ttk.Button(top, text="新增", command=self.add_server).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="编辑", command=self.edit_server).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="删除", command=self.delete_server).pack(side=tk.LEFT, padx=5)
        ttk.Button(top, text="测试连接", command=self.test_selected).pack(side=tk.LEFT, padx=5)

        ttk.Label(top, text="自动刷新").pack(side=tk.LEFT, padx=(18, 4))
        self.var_interval = tk.StringVar(value="关闭")
        self.cmb_interval = ttk.Combobox(
            top,
            textvariable=self.var_interval,
            values=("关闭", "1 分钟", "5 分钟", "10 分钟"),
            state="readonly",
            width=9,
        )
        self.cmb_interval.pack(side=tk.LEFT)
        self.cmb_interval.bind("<<ComboboxSelected>>", lambda _: self.schedule_refresh())

        self.status = ttk.Label(top, text="加载中…")
        self.status.pack(side=tk.LEFT, padx=12)

        cols = ("id", "name", "used", "pct", "quota", "avgday", "forecast", "daysleft", "state", "vnraw", "note")
        self.tree = ttk.Treeview(root, columns=cols, show="headings", height=16)
        headings = (
            "ID",
            "名称",
            "已用(GB)",
            "占比",
            "套餐(GB)",
            "当月平均每天用量",
            "预计当月用量",
            "距离重置日",
            "状态",
            "vnstat当月总量(GB)",
            "备注/错误",
        )
        widths = (120, 140, 100, 80, 100, 130, 150, 90, 60, 150, 160)
        for c, h, w in zip(cols, headings, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=w, stretch=(c == "note"), anchor=tk.CENTER)

        scroll = ttk.Scrollbar(root, orient=tk.VERTICAL, command=self.tree.yview)
        self.tree.configure(yscrollcommand=scroll.set)

        mid = ttk.Frame(root)
        mid.pack(fill=tk.BOTH, expand=True, padx=8, pady=(0, 8))
        self.tree.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scroll.pack(side=tk.RIGHT, fill=tk.Y)

        self.load_entries()
        self.refresh(False)

    def load_entries(self) -> None:
        if self.path.is_file():
            self.entries = load_servers(self.path)
        else:
            self.entries = []
            self.status.configure(text="未找到 servers.yaml，先点新增创建")

    def save_entries(self) -> None:
        save_servers(self.path, self.entries)

    def selected_id(self) -> str | None:
        item = self.tree.focus()
        if not item:
            return None
        vals = self.tree.item(item, "values")
        return str(vals[0]) if vals else None

    def find_entry(self, sid: str) -> tuple[int, dict] | None:
        for i, e in enumerate(self.entries):
            if str(e.get("id")) == sid:
                return i, e
        return None

    def find_last_row(self, sid: str) -> ServerRow | None:
        if not self.last_rows:
            return None
        for r in self.last_rows:
            if r.id == sid:
                return r
        return None

    def add_server(self) -> None:
        dlg = ServerDialog(self.root)
        if not dlg.result:
            return
        sid = str(dlg.result.get("id"))
        if self.find_entry(sid):
            messagebox.showwarning("提示", f"ID 已存在: {sid}")
            return
        self.entries.append(dlg.result)
        self.save_entries()
        self.refresh(False)

    def edit_server(self) -> None:
        sid = self.selected_id()
        if not sid:
            messagebox.showinfo("提示", "请先选中一行")
            return
        found = self.find_entry(sid)
        if not found:
            messagebox.showerror("错误", "未找到选中服务器")
            return
        idx, old = found
        current = self.find_last_row(sid)
        dlg = ServerDialog(
            self.root,
            old,
            current_used_bytes=(current.used_bytes if current else None),
            current_raw_used_bytes=(current.raw_used_bytes if current else None),
        )
        if not dlg.result:
            return
        new_sid = str(dlg.result.get("id"))
        if new_sid != sid and self.find_entry(new_sid):
            messagebox.showwarning("提示", f"ID 已存在: {new_sid}")
            return
        self.entries[idx] = dlg.result
        self.save_entries()
        self.refresh(False)

    def delete_server(self) -> None:
        sid = self.selected_id()
        if not sid:
            messagebox.showinfo("提示", "请先选中一行")
            return
        if not messagebox.askyesno("确认", f"删除 {sid} ?"):
            return
        self.entries = [e for e in self.entries if str(e.get("id")) != sid]
        self.save_entries()
        self.refresh(False)

    def interval_ms(self) -> int | None:
        label = self.var_interval.get()
        return {
            "关闭": None,
            "1 分钟": 60_000,
            "5 分钟": 300_000,
            "10 分钟": 600_000,
        }.get(label)

    def schedule_refresh(self) -> None:
        if self.refresh_job:
            self.root.after_cancel(self.refresh_job)
            self.refresh_job = None
        ms = self.interval_ms()
        if ms:
            self.refresh_job = self.root.after(ms, lambda: self.refresh(True))

    def find_entry_by_id(self, sid: str) -> dict | None:
        found = self.find_entry(sid)
        if not found:
            return None
        _, entry = found
        return entry

    @staticmethod
    def _calc_used_days(entry: dict | None) -> int:
        """计算当前计费月已用天数（含今天）。"""
        today = date.today()
        if not entry:
            return max(today.day, 1)
        try:
            reset_day = int(entry.get("billing_reset_day"))
        except (TypeError, ValueError):
            return max(today.day, 1)
        if reset_day < 1:
            return max(today.day, 1)

        start = billing_cycle_start_date(today, reset_day)
        if start is None:
            return max(today.day, 1)
        used_days = (today - start).days + 1
        return max(used_days, 1)

    @staticmethod
    def _calc_cycle_total_days(entry: dict | None) -> int:
        """计算当前计费月总天数。"""
        today = date.today()
        if not entry:
            return monthrange(today.year, today.month)[1]
        try:
            reset_day = int(entry.get("billing_reset_day"))
        except (TypeError, ValueError):
            return monthrange(today.year, today.month)[1]
        if reset_day < 1:
            return monthrange(today.year, today.month)[1]

        if today.day >= reset_day:
            start_month = today.month
            start_year = today.year
            if today.month == 12:
                next_month = 1
                next_year = today.year + 1
            else:
                next_month = today.month + 1
                next_year = today.year
        else:
            if today.month == 1:
                start_month = 12
                start_year = today.year - 1
            else:
                start_month = today.month - 1
                start_year = today.year
            next_month = today.month
            next_year = today.year

        start_day = min(reset_day, monthrange(start_year, start_month)[1])
        end_day = min(reset_day, monthrange(next_year, next_month)[1])
        start = date(start_year, start_month, start_day)
        end = date(next_year, next_month, end_day)
        total_days = (end - start).days
        return max(total_days, 1)

    @staticmethod
    def _calc_days_until_reset(entry: dict | None) -> int | None:
        today = date.today()
        if not entry:
            return None
        try:
            reset_day = int(entry.get("billing_reset_day"))
        except (TypeError, ValueError):
            return None
        if reset_day < 1:
            return None

        if today.day < reset_day:
            target_year = today.year
            target_month = today.month
        else:
            if today.month == 12:
                target_year = today.year + 1
                target_month = 1
            else:
                target_year = today.year
                target_month = today.month + 1
        target_day = min(reset_day, monthrange(target_year, target_month)[1])
        target = date(target_year, target_month, target_day)
        return max((target - today).days, 0)

    def render_rows(self, rows: list[ServerRow]) -> None:
        for i in self.tree.get_children():
            self.tree.delete(i)
        for r in rows:
            used = fmt_gb(r.used_bytes, gb_base=r.gb_base, decimals=1)
            quota = fmt_gb(r.quota_bytes, gb_base=r.gb_base, decimals=1)
            vnraw = fmt_gb(r.raw_used_bytes, gb_base=r.gb_base, decimals=1)
            pct = f"{r.used_percent:.1f}%" if r.used_percent is not None else "—"
            entry = self.find_entry_by_id(r.id)
            used_days = self._calc_used_days(entry)
            days_left = self._calc_days_until_reset(entry)
            days_left_text = f"{days_left}天" if days_left is not None else "—"
            if r.used_bytes is not None:
                avg_per_day = (r.used_bytes / (r.gb_base**3)) / used_days
                avgday = f"{avg_per_day:.1f} GB/天"
                cycle_total_days = self._calc_cycle_total_days(entry)
                forecast_gb = avg_per_day * cycle_total_days
                if r.quota_bytes and r.quota_bytes > 0:
                    quota_gb = r.quota_bytes / (r.gb_base**3)
                    forecast_pct = forecast_gb / quota_gb * 100.0
                    forecast = f"{forecast_gb:.1f} GB ({forecast_pct:.1f}%)"
                else:
                    forecast = f"{forecast_gb:.1f} GB"
            else:
                avgday = "—"
                forecast = "—"
            if r.ok:
                state = "正常"
                reset = fmt_reset(r.reset_unix) if r.type == "bandwagon" else "—"
                note = (r.detail or "") + (f" | 重置 {reset}" if reset != "—" else "")
                if r.raw_used_bytes is not None:
                    note += f" | raw={fmt_gb(r.raw_used_bytes, gb_base=r.gb_base, decimals=1)}"
            else:
                state = "失败"
                note = r.error or ""
            self.tree.insert(
                "",
                tk.END,
                values=(r.id, r.name, used, pct, quota, avgday, forecast, days_left_text, state, vnraw, note[:220]),
            )
        self.last_rows = rows
        self.status.configure(text=f"上次刷新：{datetime.now().strftime('%H:%M:%S')}")

    def refresh(self, from_timer: bool) -> None:
        if self.refreshing:
            return
        self.load_entries()
        self.refreshing = True
        self.btn_refresh.configure(state="disabled")
        self.status.configure(text="正在拉取…")

        def work():
            try:
                dirty_apply = apply_billing_period_anchor_resets(self.entries)
                rows, dirty_collect = collect_rows(self.entries)
                if dirty_apply or dirty_collect:
                    save_servers(self.path, self.entries)
            except Exception as ex:
                self.root.after(0, lambda err=ex: done(None, err))
                return
            self.root.after(0, lambda r=rows: done(r, None))

        def done(rows: list[ServerRow] | None, e: Exception | None):
            self.refreshing = False
            self.btn_refresh.configure(state="normal")
            if e:
                self.status.configure(text="失败")
                if not from_timer:
                    messagebox.showerror("错误", str(e))
            else:
                assert rows is not None
                self.render_rows(rows)
            self.schedule_refresh()

        threading.Thread(target=work, daemon=True).start()

    def test_selected(self) -> None:
        sid = self.selected_id()
        if not sid:
            messagebox.showinfo("提示", "请先选中一行")
            return
        found = self.find_entry(sid)
        if not found:
            messagebox.showerror("错误", "未找到选中服务器")
            return
        _, entry = found
        if str(entry.get("type", "")).lower() != "ssh_vnstat":
            messagebox.showinfo("提示", "当前只支持测试 SSH + vnstat 类型")
            return

        self.status.configure(text=f"测试连接中：{sid}")

        def work():
            try:
                dirty_apply = apply_billing_period_anchor_resets(self.entries)
                row0 = fetch_ssh_vnstat(entry)
                row1 = apply_used_offset(
                    replace(row0, gb_base=entry_gb_base(entry)),
                    entry,
                )
                row, dirty_cap = apply_ssh_billing_cycle_to_row(
                    entry,
                    row1,
                    row0.used_bytes if row0.ok else None,
                )
                if dirty_apply or dirty_cap:
                    save_servers(self.path, self.entries)
            except Exception as ex:
                self.root.after(0, lambda err=ex: done_err(err))
                return
            self.root.after(0, lambda r=row: done(r))

        def done(row: ServerRow):
            if row.ok:
                gb = entry_gb_base(entry)
                messagebox.showinfo(
                    "连接测试",
                    f"{sid} 连接成功\n已用: {fmt_gb(row.used_bytes, gb_base=gb)}",
                )
                self.status.configure(text=f"测试成功：{sid}")
            else:
                messagebox.showerror("连接测试失败", row.error or "未知错误")
                self.status.configure(text=f"测试失败：{sid}")

        def done_err(err: Exception) -> None:
            messagebox.showerror("错误", str(err))
            self.status.configure(text=f"测试失败：{sid}")

        threading.Thread(target=work, daemon=True).start()


def main() -> None:
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
