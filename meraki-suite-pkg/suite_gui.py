#!/usr/bin/env python3
"""
Meraki Suite — unified GUI front door.

This is ONE window with TWO independent sections:
  - Daily Management : the meraki-tools commands (export + propagate + policy)
  - Migration        : the cross-org backup / restore tool

The two engines are NOT merged. This GUI simply imports each package and routes
button clicks to the right one. Each keeps its own code, its own client/auth.
Both read the API key from MERAKI_DASHBOARD_API_KEY.

Build/run note: we add both package directories to sys.path so each package's
internal imports resolve unchanged — the engine code is untouched.
"""

import os
import sys
import queue
import subprocess
import threading
import tkinter as tk
from contextlib import redirect_stdout
from tkinter import ttk, filedialog, messagebox

from gui_widgets import OrgPicker, NetworkPicker, LogToolbar, ProgressBar

# --- make both engines importable without modifying their code ---------------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "meraki_tools_pkg"))   # for `merakicore`, `commands`
sys.path.insert(0, os.path.join(HERE, "migration_pkg"))      # for `common`, etc.

# meraki-tools engine
from merakicore import client as mt_client
from merakicore import networks as net_mod
from merakicore import policyobjects as po
from commands import (
    export_orgs, export_networks, export_firewall, export_l7,
    export_content_filter, export_policy_check, export_groups,
    apply_l7, apply_content_filter, apply_policy_group, apply_policy_bulk,
    export_l3_migration, apply_l3_insert, apply_l3_insert_batch,
    apply_l3_reinflate, apply_l3_flattened, apply_l3_restore_batch,
)

# migration engine (independent package; its modules use top-level imports,
# resolved by the migration_pkg path added above)
import common as mig_common
import policy_objects as mig_policy_objects
import network_settings as mig_network_settings
import switch_settings as mig_switch_settings
from datetime import datetime


# ----------------------------------------------------------------------------
# Plumbing: run an engine call on a thread, stream its print() into a log box.
# ----------------------------------------------------------------------------
class _QueueWriter:
    def __init__(self, q): self.q = q
    def write(self, s):
        if s: self.q.put(s)
        return len(s)
    def flush(self): pass


class App(tk.Tk):
    # Fixed brand colors — plain tk widgets, so these stay constant across the
    # light/dark ttk theme switch rather than living in PALETTES.
    ACCENT_GREEN = "#4CAF50"
    ACCENT_GREEN_ACTIVE = "#3D8B40"
    ACCENT_GREEN_DARK = "#2E7D32"

    def __init__(self):
        super().__init__()
        self.title("Meraki Suite")
        self.geometry("980x760")
        self.log_queue = queue.Queue()
        self._busy = False   # guards against starting a second op while one is running

        # In-window brand header — plain tk widgets (not ttk) so its green
        # stays fixed regardless of light/dark mode.
        header = tk.Frame(self, bg=self.ACCENT_GREEN_DARK)
        header.pack(fill="x")
        tk.Label(header, text="Meraki Suite", bg=self.ACCENT_GREEN_DARK, fg="white",
                 font=("", 14, "bold"), padx=12, pady=8).pack(side="left")

        # API key bar (shared by both engines via the env var)
        top = ttk.Frame(self); top.pack(fill="x", padx=8, pady=6)
        ttk.Label(top, text="API key:").pack(side="left")
        self.api_key = tk.StringVar(value=os.environ.get(mt_client.ENV_VAR, ""))
        e = ttk.Entry(top, textvariable=self.api_key, show="*", width=48)
        e.pack(side="left", padx=6)
        self.show_key = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Show", variable=self.show_key,
                        command=lambda: e.config(show="" if self.show_key.get() else "*")).pack(side="left")
        ttk.Label(top, text="(MERAKI_DASHBOARD_API_KEY)").pack(side="left", padx=8)
        test_btn = ttk.Button(top, text="Test connection")
        test_btn.configure(command=lambda: self._test_connection(test_btn))
        test_btn.pack(side="left", padx=(4, 0))

        # Dark-mode toggle (themes are applied via _apply_theme).
        self.dark_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Dark mode", variable=self.dark_mode,
                        command=self._apply_theme).pack(side="right")

        # Log — packed (claimed) BEFORE body and pinned to side="bottom", so it
        # always keeps at least its natural height even when a tall panel (lots
        # of fields, a NetworkPicker, etc.) would otherwise want more vertical
        # space than the window has. Without this, pack() gives body first dibs
        # (packed first = claims space first) and can squeeze Output to nothing.
        logf = ttk.LabelFrame(self, text="Output")
        logf.pack(side="bottom", fill="both", expand=True, padx=8, pady=6)
        logrow = ttk.Frame(logf); logrow.pack(side="bottom", fill="both", expand=True)
        self.log = tk.Text(logrow, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logrow, command=self.log.yview); sb.pack(side="right", fill="y")
        self.log["yscrollcommand"] = sb.set
        toolbar_row = ttk.Frame(logf); toolbar_row.pack(anchor="w", fill="x", padx=4, pady=(4, 2))
        LogToolbar(toolbar_row, self.log).pack(side="left")
        ttk.Button(toolbar_row, text="Open output folder", command=self._open_output_folder
                  ).pack(side="left", padx=(8, 0))
        self._progress = ProgressBar(toolbar_row)

        # Body: left nav + right panel. If a panel's content is taller than the
        # space left above the Output box, the panel scrolls internally (see
        # the Canvas+Scrollbar wrapper below) instead of pushing Output off-screen.
        body = ttk.Frame(self); body.pack(side="top", fill="both", expand=True, padx=8, pady=4)

        nav = ttk.Frame(body); nav.pack(side="left", fill="y")
        self.tree = ttk.Treeview(nav, show="tree", selectmode="browse")
        self.tree.column("#0", width=320, stretch=False)
        self.tree.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        panel_holder = ttk.Frame(body)
        panel_holder.pack(side="left", fill="both", expand=True, padx=(8, 0))
        panel_canvas = tk.Canvas(panel_holder, highlightthickness=0)
        panel_scroll = ttk.Scrollbar(panel_holder, orient="vertical", command=panel_canvas.yview)
        panel_canvas.configure(yscrollcommand=panel_scroll.set)
        panel_canvas.pack(side="left", fill="both", expand=True)
        panel_scroll.pack(side="right", fill="y")
        self.panel = ttk.Frame(panel_canvas)
        panel_window = panel_canvas.create_window((0, 0), window=self.panel, anchor="nw")
        self.panel.bind("<Configure>",
                        lambda e: panel_canvas.configure(scrollregion=panel_canvas.bbox("all")))
        panel_canvas.bind("<Configure>",
                          lambda e: panel_canvas.itemconfigure(panel_window, width=e.width))
        self._panel_canvas = panel_canvas

        # Mouse-wheel scrolling, only while the pointer is over the panel area
        # (so it doesn't hijack scrolling elsewhere, e.g. the nav tree or Output).
        def _wheel(event):
            delta = -1 if event.num == 4 else (1 if event.num == 5 else -event.delta // 120)
            panel_canvas.yview_scroll(delta, "units")

        def _bind_wheel(_e):
            panel_canvas.bind_all("<MouseWheel>", _wheel)
            panel_canvas.bind_all("<Button-4>", _wheel)
            panel_canvas.bind_all("<Button-5>", _wheel)

        def _unbind_wheel(_e):
            panel_canvas.unbind_all("<MouseWheel>")
            panel_canvas.unbind_all("<Button-4>")
            panel_canvas.unbind_all("<Button-5>")

        panel_canvas.bind("<Enter>", _bind_wheel)
        panel_canvas.bind("<Leave>", _unbind_wheel)

        self._build_nav()
        self._apply_theme()          # start in light mode (matches dark_mode=False)
        self.after(80, self._drain)

    # -- theming -------------------------------------------------------------
    PALETTES = {
        "light": {
            "bg": "#f0f0f0", "fg": "#000000", "field": "#ffffff", "field_fg": "#000000",
            "sel": "#0078d7", "sel_fg": "#ffffff", "log_bg": "#ffffff", "log_fg": "#111111",
            "warn": "#a00000", "muted": "#555555",
        },
        "dark": {
            "bg": "#1e1e1e", "fg": "#e6e6e6", "field": "#2b2b2b", "field_fg": "#e6e6e6",
            "sel": "#0a84ff", "sel_fg": "#ffffff", "log_bg": "#141414", "log_fg": "#e0e0e0",
            "warn": "#ff6b6b", "muted": "#aaaaaa",
        },
    }

    def _apply_theme(self):
        p = self.PALETTES["dark" if self.dark_mode.get() else "light"]
        style = ttk.Style(self)
        # 'clam' honors custom colors far more reliably than the native themes.
        try:
            style.theme_use("clam")
        except Exception:
            pass

        self.configure(bg=p["bg"])
        style.configure(".", background=p["bg"], foreground=p["fg"])
        style.configure("TFrame", background=p["bg"])
        style.configure("TLabel", background=p["bg"], foreground=p["fg"])
        style.configure("TLabelframe", background=p["bg"], foreground=p["fg"])
        style.configure("TLabelframe.Label", background=p["bg"], foreground=p["fg"])
        style.configure("TCheckbutton", background=p["bg"], foreground=p["fg"])
        style.map("TCheckbutton", background=[("active", p["bg"])])
        style.configure("TButton", background=self.ACCENT_GREEN, foreground="white")
        style.map("TButton",
                  background=[("active", self.ACCENT_GREEN_ACTIVE), ("disabled", p["field"])],
                  foreground=[("disabled", p["muted"])])
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["field_fg"],
                        insertcolor=p["fg"])
        style.configure("TCombobox", fieldbackground=p["field"], background=p["field"],
                        foreground=p["field_fg"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field"])],
                  foreground=[("readonly", p["field_fg"])])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"],
                        foreground=p["fg"])
        style.map("Treeview", background=[("selected", p["sel"])],
                  foreground=[("selected", p["sel_fg"])])

        # The log and the panel-scroll canvas are plain tk widgets — recolor directly.
        self.log.configure(bg=p["log_bg"], fg=p["log_fg"], insertbackground=p["fg"])
        self._panel_canvas.configure(bg=p["bg"])

        # Re-render the current panel so any color-bearing labels pick up the theme.
        self._warn_color = p["warn"]
        self._muted_color = p["muted"]
        sel = self.tree.selection()
        if sel and ":" in sel[0]:
            self._render_panel(sel[0])

    # -- navigation ----------------------------------------------------------
    def _build_nav(self):
        daily = self.tree.insert("", "end", text="Daily Management", open=True)
        for key, label in [
            ("orgs", "List organizations"),
            ("networks", "Export networks"),
            ("export_groups", "Export groups"),
            ("firewall", "Export firewall rules"),
            ("export_l7", "Export L7 (from a network)"),
            ("export_cf", "Export content filter"),
            ("policy_check", "Policy check (audit)"),
            ("apply_l7", "Apply L7 rules"),
            ("apply_cf", "Apply content filter"),
            ("policy_group", "Policy → group"),
            ("policy_bulk", "Policy → bulk groups"),
        ]:
            self.tree.insert(daily, "end", iid=f"d:{key}", text="  " + label)

        mig = self.tree.insert("", "end", text="Migration", open=True)
        for key, label in [
            ("mig_list", "List networks in a backup"),
            ("mig_backup", "Backup an org"),
            ("mig_restore", "Restore to an org"),
            ("mig_ports", "Restore switch ports"),
        ]:
            self.tree.insert(mig, "end", iid=f"m:{key}", text="  " + label)

        l3 = self.tree.insert("", "end", text="L3 Rule Tools (advanced)", open=True)
        for key, label in [
            ("migration_export", "Export L3 migration (name + flattened)"),
            ("insert", "Apply L3 insert"),
            ("insert_batch", "Apply L3 insert (batch)"),
            ("reinflate", "Apply L3 reinflate"),
            ("flattened", "Apply L3 flattened"),
            ("restore_batch", "Apply L3 restore (batch rollback)"),
        ]:
            self.tree.insert(l3, "end", iid=f"l3:{key}", text="  " + label)

    def _on_select(self, _evt):
        sel = self.tree.selection()
        if not sel or ":" not in sel[0]:
            return
        self._render_panel(sel[0])

    # -- shared helpers ------------------------------------------------------
    def _clear_panel(self):
        for w in self.panel.winfo_children():
            w.destroy()

    def _sync_key(self):
        key = self.api_key.get().strip()
        if not key:
            messagebox.showwarning("API key", "Enter your Meraki API key first.")
            return False
        os.environ[mt_client.ENV_VAR] = key   # both engines read this
        return True

    def _open_output_folder(self):
        """Open the default output/ folder (exports, backups, L3 migration/batch
        files) in the OS file manager, creating it first if nothing has been
        written yet."""
        out_dir = os.path.join(HERE, "output")
        os.makedirs(out_dir, exist_ok=True)
        try:
            if sys.platform.startswith("win"):
                os.startfile(out_dir)
            elif sys.platform == "darwin":
                subprocess.run(["open", out_dir], check=False)
            else:
                subprocess.run(["xdg-open", out_dir], check=False)
        except Exception as e:
            messagebox.showerror("Could not open folder", f"{out_dir}\n\n{e}")

    def _test_connection(self, button):
        """Lightweight API-key/connectivity check: list orgs and report the
        count. Doesn't touch any org's configuration."""
        def call():
            orgs = self._dashboard().organizations.getOrganizations()
            print(f"  Connection OK — this API key can see {len(orgs)} organization(s).")
        self._run(call, button=button)

    def _log(self, text):
        self.log["state"] = "normal"; self.log.insert("end", text)
        self.log.see("end"); self.log["state"] = "disabled"

    def _drain(self):
        while not self.log_queue.empty():
            self._log(self.log_queue.get_nowait())
        self.after(80, self._drain)

    def _run(self, fn, button=None, cancel_event=None):
        """
        Run fn() on a worker thread, streaming its stdout into the log. Drives
        the shared progress bar and, if `button` is given, disables it for the
        duration (and guards against starting a second operation while one is
        already running).
        """
        if not self._sync_key():
            return
        if self._busy:
            messagebox.showinfo(
                "Busy", "An operation is already running. Wait for it to finish "
                "(or click Cancel) before starting another.")
            return
        self._busy = True
        if button is not None:
            button.configure(state="disabled")
        self._progress.on_cancel(cancel_event.set if cancel_event is not None else (lambda: None))

        def _on_finished():
            self._busy = False
            if button is not None and button.winfo_exists():
                button.configure(state="normal")
        self._progress.on_finish(_on_finished)
        self._progress.start()

        writer = _QueueWriter(self.log_queue)

        def worker():
            try:
                with redirect_stdout(writer):
                    fn()
            except SystemExit as e:
                self.log_queue.put(f"\n[stopped] {e}\n")
            except Exception as e:
                self.log_queue.put(f"\n[error] {e}\n")
            finally:
                self._progress.finish()
        threading.Thread(target=worker, daemon=True).start()

    def _engine_call(self, button, fn, *args, **kwargs):
        """
        Call an engine fn(*args, progress_cb=..., cancel_event=..., **kwargs) on
        a worker thread, wired to the shared progress bar + Cancel button, with
        `button` disabled for the duration. Use for any WRITE engine call --
        read-only exports don't take progress_cb/cancel_event, so they should
        keep using self._run(...) directly.
        """
        cancel_event = threading.Event()
        kwargs["progress_cb"] = lambda d, t: self._progress.report(d, t)
        kwargs["cancel_event"] = cancel_event
        self._run(lambda: fn(*args, **kwargs), button=button, cancel_event=cancel_event)

    def _dashboard(self):
        return mt_client.get_dashboard()

    # -- org/network picker data sources -------------------------------------
    # Both engines create equivalent Meraki clients from the same API key, so
    # one shared fetch works for both; kept as separate methods (rather than
    # one shared dashboard) to match the "each engine builds its own client"
    # design principle documented in meraki_tools_pkg/README.md.
    def _fetch_orgs_daily(self):
        if not self._sync_key():
            return []
        return self._dashboard().organizations.getOrganizations()

    def _fetch_networks_daily(self, org_id):
        if not self._sync_key():
            return []
        return net_mod.fetch_all_networks(self._dashboard(), org_id)

    def _fetch_orgs_mig(self):
        if not self._sync_key():
            return []
        return self._mig_dashboard().organizations.getOrganizations()

    def _fetch_networks_mig(self, org_id):
        if not self._sync_key():
            return []
        return net_mod.fetch_all_networks(self._mig_dashboard(), org_id)

    # -- panel rendering -----------------------------------------------------
    def _render_panel(self, iid):
        self._clear_panel()
        builder = getattr(self, "_panel_" + iid.replace(":", "_"), None)
        if builder:
            builder()
        else:
            ttk.Label(self.panel, text="(not yet implemented)").pack(padx=12, pady=12)
        self._panel_canvas.yview_moveto(0)

    # Each panel is a small form. Only a couple are shown here fully; the rest
    # follow the same shape and are filled in as the section is built out.

    def _panel_d_orgs(self):
        ttk.Label(self.panel, text="List organizations", font=("", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 4))
        ttk.Label(self.panel, text="Read-only. Lists every org your API key can see, with IDs.",
                  wraplength=520).pack(anchor="w", padx=12)
        ttk.Button(self.panel, text="List organizations",
                   command=lambda: self._run(lambda: export_orgs.run(self._dashboard()))
                   ).pack(anchor="w", padx=12, pady=12)

    # ---- small form helpers ------------------------------------------------
    def _heading(self, title, blurb):
        ttk.Label(self.panel, text=title, font=("", 12, "bold")).pack(anchor="w", padx=12, pady=(12, 2))
        ttk.Label(self.panel, text=blurb, wraplength=520, foreground=getattr(self, "_muted_color", "#444")).pack(anchor="w", padx=12, pady=(0, 8))

    def _field(self, label, var, width=40):
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text=label, width=26, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=width).pack(side="left", fill="x", expand=True)
        return row

    def _help_line(self, text):
        ttk.Label(self.panel, text=text, wraplength=520,
                  foreground=getattr(self, "_muted_color", "#555")).pack(anchor="w", padx=12, pady=(0, 4))

    def _org_field(self, fetch_orgs=None):
        """
        Org picker that defaults to MERAKI_ORG_ID. Returns an OrgPicker, which
        duck-types a StringVar's .get() (returns the org ID) so existing
        `org.get().strip()` call sites work unchanged.
        """
        fetch = fetch_orgs or self._fetch_orgs_daily
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="Organization", width=26, anchor="w").pack(side="left")
        picker = OrgPicker(row, fetch, initial=os.environ.get("MERAKI_ORG_ID", ""))
        picker.pack(side="left", fill="x", expand=True)
        return picker

    def _network_picker(self, org_picker, fetch_networks=None, key="id", label="Networks"):
        """
        Checklist of the selected org's networks; auto-loads when org_picker's
        org changes. Returns a NetworkPicker (.get_selected() -> None for "all
        applicable networks", matching the previous blank-field convention, or
        a list of ids/names per `key`).
        """
        fetch = fetch_networks or self._fetch_networks_daily
        ttk.Label(self.panel, text=f"{label} (optional — leave 'All networks' checked for every applicable one)",
                  wraplength=520, foreground=getattr(self, "_muted_color", "#555")
                  ).pack(anchor="w", padx=12, pady=(8, 0))
        picker = NetworkPicker(self.panel, fetch, key=key)
        picker.pack(fill="x", padx=12, pady=(2, 6))
        org_picker.on_change(picker.load)
        if org_picker.get():
            picker.load(org_picker.get())
        return picker

    def _split(self, text):
        """Split a space/comma list into a clean list, or None if empty."""
        items = [x.strip() for x in text.replace(",", " ").split() if x.strip()]
        return items or None

    def _go(self, label, fn):
        """
        fn(button) is called directly (on the main thread) when the button is
        clicked -- `button` is this button, so fn can pass it to self._run(...)
        to be disabled for the duration. fn must snapshot any widget/variable
        values it needs into plain Python values FIRST, then hand the actual
        engine call to self._run(...) itself — Tk variables can only be read
        from the thread running mainloop, so none of that reading may happen
        inside the self._run() worker thread.
        """
        btn = ttk.Button(self.panel, text=label)
        btn.configure(command=lambda: fn(btn))
        btn.pack(anchor="w", padx=12, pady=12)

    # ---- read-only Daily panels -------------------------------------------
    def _panel_d_networks(self):
        self._heading("Export networks",
                      "Read-only. Lists the org's networks (all, or a chosen subset) to JSON or CSV.")
        org = self._org_field()
        nets = self._network_picker(org)
        fmt = tk.StringVar(value="json")
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="Format", width=26, anchor="w").pack(side="left")
        ttk.Combobox(row, textvariable=fmt, values=["json", "csv"], width=8, state="readonly").pack(side="left")

        def task(button):
            org_id = org.get()
            network_ids = nets.get_selected()
            fmt_val = fmt.get()
            self._run(lambda: export_networks.run(self._dashboard(), org_id,
                                                  network_ids=network_ids, fmt=fmt_val),
                     button=button)
        self._go("Export networks", task)

    def _panel_d_export_groups(self):
        self._heading("Export groups",
                      "Read-only. Lists an org's policy object groups (name, ID, member count) — "
                      "a discovery aid to confirm exact group names before referencing them "
                      "elsewhere (e.g. an L3-insert rule file, which references groups by name).")
        org = self._org_field()
        fmt = tk.StringVar(value="json")
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="Format", width=26, anchor="w").pack(side="left")
        ttk.Combobox(row, textvariable=fmt, values=["json", "csv"], width=8, state="readonly").pack(side="left")
        show_members = tk.BooleanVar(value=False)
        ttk.Checkbutton(self.panel, text="Include member object IDs (JSON only)",
                        variable=show_members).pack(anchor="w", padx=12, pady=(6, 0))

        def task(button):
            org_id = org.get()
            fmt_val = fmt.get()
            show_members_val = show_members.get()
            self._run(lambda: export_groups.run(self._dashboard(), org_id, fmt=fmt_val,
                                                show_members=show_members_val),
                     button=button)
        self._go("Export groups", task)

    def _panel_d_firewall(self):
        self._heading("Export firewall rules",
                      "Read-only. L3 firewall + switch ACL rules, with OBJ/GRP tokens resolved and expanded.")
        org = self._org_field()
        nets = self._network_picker(org)
        fmt = tk.StringVar(value="csv")
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="Format", width=26, anchor="w").pack(side="left")
        ttk.Combobox(row, textvariable=fmt, values=["csv", "json"], width=8, state="readonly").pack(side="left")

        def task(button):
            org_id = org.get()
            network_ids = nets.get_selected()
            fmt_val = fmt.get()
            self._run(lambda: export_firewall.run(self._dashboard(), org_id,
                                                  network_ids=network_ids, fmt=fmt_val),
                     button=button)
        self._go("Export firewall rules", task)

    def _panel_d_export_l7(self):
        self._heading("Export L7 rules from a network",
                      "Read-only. Copies one network's L7 firewall rules to JSON — the 'copy from' "
                      "source for Apply L7.")
        org = self._org_field()
        src = tk.StringVar(); self._field("Source network ID", src)

        def task(button):
            org_id = org.get()
            src_id = src.get().strip()
            self._run(lambda: export_l7.run(self._dashboard(), org_id, src_id), button=button)
        self._go("Export L7 rules", task)

    def _panel_d_export_cf(self):
        self._heading("Export content filtering from a network",
                      "Read-only. Copies one network's content-filtering settings to JSON — the "
                      "'copy from' source for Apply content filter.")
        org = self._org_field()
        src = tk.StringVar(); self._field("Source network ID", src)

        def task(button):
            org_id = org.get()
            src_id = src.get().strip()
            self._run(lambda: export_content_filter.run(self._dashboard(), org_id, src_id),
                     button=button)
        self._go("Export content filter", task)

    def _panel_d_policy_check(self):
        self._heading("Policy check (audit)",
                      "Read-only. Reports whether the given FQDNs/IPs exist as policy objects — in one "
                      "org (set Organization ID) or across all orgs (leave it blank).")
        org = self._org_field()
        fqdns = tk.StringVar(); self._field("FQDNs (optional)", fqdns)
        ips = tk.StringVar(); self._field("IPs/CIDRs (optional)", ips)
        ff = tk.StringVar()
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="From file (optional)", width=26, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=ff, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: ff.set(filedialog.askopenfilename() or ff.get())).pack(side="left", padx=4)

        def task(button):
            fqdns_val = self._split(fqdns.get())
            ips_val = self._split(ips.get())
            ff_val = ff.get().strip() or None
            org_id = org.get().strip() or None

            def call():
                entries = po.parse_entries(fqdns=fqdns_val, ips=ips_val, from_file=ff_val)
                export_policy_check.run(self._dashboard(), entries, org_id=org_id)
            self._run(call, button=button)
        self._go("Run policy check", task)

    # ---- write Daily panels ------------------------------------------------
    def _file_field(self, label, var):
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text=label, width=26, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: var.set(filedialog.askopenfilename() or var.get())).pack(side="left", padx=4)

    def _dryrun_and_apply(self, do_apply, what):
        """
        Standard write-panel footer: a dry-run checkbox (default ON) and a Run
        button. do_apply(apply_bool, button) is called directly on the main
        thread (the button click handler) — it must snapshot any widget values
        it needs into plain Python values, then hand the actual engine call to
        self._engine_call(button, ...)/self._run(...) itself (Tk variables
        can't be read from the worker thread). On a real apply (dry-run
        unchecked) we show a confirmation dialog first — this replaces the
        CLI's stdin prompt.
        """
        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.panel, text="Dry run (preview only — no changes written)",
                        variable=dry).pack(anchor="w", padx=12, pady=(10, 2))

        btn = ttk.Button(self.panel, text="Run")

        def run_clicked():
            apply_bool = not dry.get()
            if apply_bool:
                if not messagebox.askyesno(
                        "Confirm write",
                        f"This will WRITE changes to live networks:\n\n{what}\n\n"
                        "Have you reviewed a dry run first?\n\nProceed?",
                        icon="warning", default="no"):
                    return
            do_apply(apply_bool, btn)

        btn.configure(command=run_clicked)
        btn.pack(anchor="w", padx=12, pady=10)

    def _panel_d_apply_l7(self):
        self._heading("Apply L7 rules",
                      "Propagate L7 firewall rules from a JSON file (made by Export L7) onto target "
                      "networks. Dry run by default.")
        org = self._org_field()
        src = tk.StringVar(); self._file_field("From file", src)
        nets = self._network_picker(org)
        mode = tk.StringVar(value="replace")
        pos = tk.StringVar(value="bottom")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Mode", width=26, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=mode, values=["replace", "append"], width=10, state="readonly").pack(side="left")
        ttk.Label(r, text="Append position").pack(side="left", padx=(12, 4))
        ttk.Combobox(r, textvariable=pos, values=["bottom", "top"], width=8, state="readonly").pack(side="left")

        def do(apply_bool, button):
            org_id = org.get()
            src_file = src.get().strip()
            network_ids = nets.get_selected()
            mode_val = mode.get()
            pos_val = pos.get()
            self._engine_call(button, apply_l7.run, self._dashboard(), org_id, src_file,
                              network_ids=network_ids, mode=mode_val,
                              position=pos_val, apply=apply_bool)
        self._dryrun_and_apply(do, "Apply L7 rules to the targeted networks.")

    def _panel_d_apply_cf(self):
        self._heading("Apply content filter",
                      "Propagate content-filtering settings from a JSON file (made by Export content "
                      "filter) onto target networks. Dry run by default.")
        org = self._org_field()
        src = tk.StringVar(); self._file_field("From file", src)
        nets = self._network_picker(org)
        mode = tk.StringVar(value="replace")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Mode", width=26, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=mode, values=["replace", "append"], width=10, state="readonly").pack(side="left")

        def do(apply_bool, button):
            org_id = org.get()
            src_file = src.get().strip()
            network_ids = nets.get_selected()
            mode_val = mode.get()
            self._engine_call(button, apply_content_filter.run, self._dashboard(), org_id, src_file,
                              network_ids=network_ids, mode=mode_val, apply=apply_bool)
        self._dryrun_and_apply(do, "Apply content-filtering settings to the targeted networks.")

    def _panel_d_policy_bulk(self):
        self._heading("Policy → bulk groups",
                      "Create many policy objects from a file and spread them across numbered groups "
                      "(PREFIX 1, PREFIX 2, ...). Idempotent: re-running only adds new entries. "
                      "Dry run shows the full plan.")
        ttk.Label(self.panel,
                  text="Note: a policy object group holds a maximum of 150 objects. Keep Group size "
                       "at or below 150 (140 is a safe default for headroom); lower it for lower-end "
                       "MX models.",
                  wraplength=520, foreground=getattr(self, "_warn_color", "#a00")).pack(anchor="w", padx=12, pady=(0, 8))
        org = self._org_field()
        src = tk.StringVar(); self._file_field("From file", src)
        prefix = tk.StringVar(); self._field("Group prefix", prefix)
        name = tk.StringVar(); self._field("Object name base", name)
        size = tk.StringVar(value="140"); self._field("Group size", size)

        def do(apply_bool, button):
            src_file = src.get().strip() or None
            org_id = org.get().strip()
            prefix_val = prefix.get().strip()
            name_val = name.get().strip() or None
            try:
                gsize = int(size.get())
            except ValueError:
                messagebox.showerror("Invalid group size", "Group size must be a whole number.")
                return
            # entries parsing (file I/O -- can raise) stays inside the worker,
            # not here, so a bad file path is reported in the Output box
            # instead of crashing the click handler on the main thread.
            cancel_event = threading.Event()

            def call():
                entries = po.parse_entries(from_file=src_file)
                apply_policy_bulk.run(self._dashboard(), entries, org_id=org_id,
                                      group_prefix=prefix_val, group_size=gsize,
                                      name_base=name_val, apply=apply_bool, assume_yes=True,
                                      progress_cb=lambda d, t: self._progress.report(d, t),
                                      cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._dryrun_and_apply(
            do, "Create policy objects and create/update the numbered groups in this org.")

    def _panel_d_policy_group(self):
        self._heading("Policy → single group",
                      "Create policy objects and add them to ONE group. NOTE: choosing the org and "
                      "group is interactive in the engine, so from the GUI this panel supports DRY "
                      "RUN preview; use the command line for the live apply, or use Bulk groups "
                      "(which is fully GUI-driven).")
        ttk.Label(self.panel,
                  text="(For live single-group applies, the CLI's interactive picker is required. "
                       "Bulk groups covers the non-interactive case.)",
                  wraplength=520, foreground=getattr(self, "_warn_color", "#a00")).pack(anchor="w", padx=12, pady=(0, 8))
        fqdns = tk.StringVar(); self._field("FQDNs (optional)", fqdns)
        ips = tk.StringVar(); self._field("IPs/CIDRs (optional)", ips)
        ff = tk.StringVar(); self._file_field("From file (optional)", ff)
        name = tk.StringVar(); self._field("Object name base", name)

        def task(button):
            fqdns_val = self._split(fqdns.get())
            ips_val = self._split(ips.get())
            ff_val = ff.get().strip() or None
            name_val = name.get().strip() or None
            cancel_event = threading.Event()

            def call():
                entries = po.parse_entries(fqdns=fqdns_val, ips=ips_val, from_file=ff_val)
                # dry run only from GUI (engine prompts for org/group on real apply)
                apply_policy_group.run(self._dashboard(), entries, apply=False,
                                       name_base=name_val, assume_yes=True,
                                       progress_cb=lambda d, t: self._progress.report(d, t),
                                       cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._go("Dry-run preview", task)

    # ---- Migration panels (independent engine) -----------------------------
    def _mig_dashboard(self):
        # The migration engine builds its own client; it reads the same env var.
        return mig_common.get_dashboard()

    def _panel_m_mig_list(self):
        self._heading("List networks in a backup",
                      "Read-only. Shows the network NAMES and IDs stored in a backup folder, so you "
                      "can see exactly what a restore would target. Migration restore matches by "
                      "network NAME — use the Name column when filling the Networks field on the "
                      "restore panels.")
        bdir = tk.StringVar(); self._dir_field("Backup folder", bdir)

        def task(button):
            folder = bdir.get().strip()

            def call():
                import json
                path = os.path.join(folder, "networks.json")
                if not os.path.isfile(path):
                    print(f"  no networks.json found in {folder}")
                    print("  (pick the backup folder that directly contains networks.json)")
                    return
                with open(path) as fh:
                    nets = json.load(fh)
                print(f"  {len(nets)} network(s) in this backup:\n")
                print(f"  {'NAME':<40} {'ID'}")
                print(f"  {'-'*40} {'-'*20}")
                for n in sorted(nets, key=lambda x: x.get("name", "").lower()):
                    print(f"  {n.get('name',''):<40} {n.get('id','')}")
            self._run(call, button=button)
        self._go("List networks", task)

    def _dir_field(self, label, var):
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text=label, width=26, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: var.set(filedialog.askdirectory() or var.get())).pack(side="left", padx=4)

    def _panel_m_mig_backup(self):
        self._heading("Backup an org (Migration)",
                      "Read-only. Saves an org's policy objects and/or network settings to a "
                      "timestamped folder. This is the cross-org migration tool — separate from "
                      "Daily Management.")
        org = self._org_field(fetch_orgs=self._fetch_orgs_mig)
        scope = tk.StringVar(value="all")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Scope", width=26, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=scope, values=["objects", "networks", "all"],
                     width=12, state="readonly").pack(side="left")
        nets = self._network_picker(org, fetch_networks=self._fetch_networks_mig, key="name")
        dest = tk.StringVar(value=mig_common.BACKUP_ROOT)
        self._dir_field("Backup location", dest)

        def task(button):
            org_id = org.get()
            dest_val = dest.get().strip() or mig_common.BACKUP_ROOT
            nl = nets.get_selected()
            scope_val = scope.get()

            def call():
                dash = self._mig_dashboard()
                o = dash.organizations.getOrganization(organizationId=org_id)
                stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in o["name"])
                backup_dir = os.path.join(dest_val, f"{org_id}_{safe}_{stamp}")
                print(f"Backup of '{o['name']}' ({org_id}) -> {backup_dir}")
                if scope_val in ("objects", "all"):
                    mig_policy_objects.backup(dash, org_id, backup_dir)
                if scope_val in ("networks", "all"):
                    mig_network_settings.backup(dash, org_id, backup_dir, network_filter=nl)
                print(f"\nDone. Backup folder: {backup_dir}")
            self._run(call, button=button)
        self._go("Run backup", task)

    def _panel_m_mig_restore(self):
        self._heading("Restore to an org (Migration)",
                      "Writes config from a backup folder into a target org. Name-matched settings are "
                      "updated and missing ones created. Dry run by default — review it before applying.")
        ttk.Label(self.panel,
                  text="WARNING: a live restore changes configuration across the target org and "
                       "affects networks/rules at scale. Always dry-run first and confirm the org.",
                  wraplength=520, foreground=getattr(self, "_warn_color", "#a00")).pack(anchor="w", padx=12, pady=(0, 8))
        org = self._org_field(fetch_orgs=self._fetch_orgs_mig)
        scope = tk.StringVar(value="all")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Scope", width=26, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=scope, values=["objects", "networks", "all", "vpn"],
                     width=12, state="readonly").pack(side="left")
        nets = self._network_picker(org, fetch_networks=self._fetch_networks_mig, key="name")
        self._help_line("Use 'List networks in a backup' to confirm names. The dry-run output shows "
                        "exactly which networks matched.")
        bdir = tk.StringVar(); self._dir_field("Backup folder", bdir)

        def do(apply_bool, button):
            org_id = org.get()
            backup_dir = bdir.get().strip()
            nl = nets.get_selected()
            scope_val = scope.get()
            dry_run = not apply_bool
            cancel_event = threading.Event()
            progress_cb = lambda d, t: self._progress.report(d, t)

            def call():
                dash = self._mig_dashboard()
                if not backup_dir or not os.path.isdir(backup_dir):
                    print("  pick a valid backup folder first"); return
                o = dash.organizations.getOrganization(organizationId=org_id)
                print(f"Restore from {backup_dir} -> '{o['name']}' ({org_id})  "
                      f"scope={scope_val}  dry_run={dry_run}")
                if scope_val == "vpn":
                    mig_network_settings.restore_vpn_only(
                        dash, org_id, backup_dir, network_filter=nl, dry_run=dry_run,
                        progress_cb=progress_cb, cancel_event=cancel_event)
                    print("\nDone."); return
                id_map = None
                if scope_val in ("objects", "all"):
                    id_map = mig_policy_objects.restore(
                        dash, org_id, backup_dir, dry_run=dry_run,
                        progress_cb=progress_cb, cancel_event=cancel_event)
                if scope_val in ("networks", "all"):
                    mig_network_settings.restore(
                        dash, org_id, backup_dir, dry_run=dry_run, network_filter=nl,
                        id_map=id_map, progress_cb=progress_cb, cancel_event=cancel_event)
                print("\nDone.")
            self._run(call, button=button, cancel_event=cancel_event)

        # restore uses the same dry-run footer; the confirm dialog wording is stronger
        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.panel, text="Dry run (preview only — no changes written)",
                        variable=dry).pack(anchor="w", padx=12, pady=(10, 2))

        btn = ttk.Button(self.panel, text="Run restore")

        def run_clicked():
            apply_bool = not dry.get()
            org_id_confirm = org.get()
            scope_confirm = scope.get()
            if apply_bool:
                if not messagebox.askyesno(
                        "Confirm LIVE restore",
                        f"LIVE RESTORE into org {org_id_confirm} (scope: {scope_confirm}).\n\n"
                        "Name-matched settings will be overwritten and missing ones created, "
                        "across the target org.\n\nHave you reviewed a dry run?\n\nProceed?",
                        icon="warning", default="no"):
                    return
            do(apply_bool, btn)
        btn.configure(command=run_clicked)
        btn.pack(anchor="w", padx=12, pady=10)

    def _panel_m_mig_ports(self):
        self._heading("Restore switch ports (Migration)",
                      "Applies per-port switch configs from a backup onto the target org's switches. "
                      "Matching is by DEVICE NAME — a new switch named exactly like the backed-up one "
                      "inherits its port config. Dry run by default.")
        ttk.Label(self.panel,
                  text="Run this AFTER the new switches are claimed into the target networks and "
                       "named to match the originals. A live run OVERWRITES port configs on "
                       "name-matched switches.",
                  wraplength=520, foreground=getattr(self, "_warn_color", "#a00")).pack(anchor="w", padx=12, pady=(0, 8))
        org = self._org_field(fetch_orgs=self._fetch_orgs_mig)
        nets = self._network_picker(org, fetch_networks=self._fetch_networks_mig, key="name")
        self._help_line("Dry-run output shows which networks matched.")
        bdir = tk.StringVar(); self._dir_field("Backup folder", bdir)

        def do(apply_bool, button):
            org_id = org.get()
            backup_dir = bdir.get().strip()
            network_filter = nets.get_selected()
            dry_run = not apply_bool
            cancel_event = threading.Event()

            def call():
                dash = self._mig_dashboard()
                if not backup_dir or not os.path.isdir(backup_dir):
                    print("  pick a valid backup folder first"); return
                o = dash.organizations.getOrganization(organizationId=org_id)
                print(f"Switch port restore from {backup_dir} -> '{o['name']}' ({org_id})  "
                      f"dry_run={dry_run}")
                mig_switch_settings.restore_ports(
                    dash, org_id, backup_dir, network_filter=network_filter, dry_run=dry_run,
                    progress_cb=lambda d, t: self._progress.report(d, t), cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)

        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.panel, text="Dry run (preview only — no changes written)",
                        variable=dry).pack(anchor="w", padx=12, pady=(10, 2))

        btn = ttk.Button(self.panel, text="Run port restore")

        def run_clicked():
            apply_bool = not dry.get()
            org_id_confirm = org.get()
            if apply_bool:
                if not messagebox.askyesno(
                        "Confirm switch port restore",
                        f"This will OVERWRITE port configs on name-matched switches in org "
                        f"{org_id_confirm}.\n\nHave you reviewed a dry run?\n\nProceed?",
                        icon="warning", default="no"):
                    return
            do(apply_bool, btn)
        btn.configure(command=run_clicked)
        btn.pack(anchor="w", padx=12, pady=10)

    # ---- L3 Rule Tools (advanced): CLI-parity panels for the flatten/insert/ ---
    # ---- reinflate/batch migration workflow ------------------------------------
    def _panel_l3_migration_export(self):
        self._heading("Export L3 migration (name + flattened)",
                      "Read-only. Exports one network's L3 ruleset in two forms: name-referenced "
                      "(for Apply L3 reinflate) and fully flattened literals (for Apply L3 "
                      "flattened). This is the source-side step of an org-to-org L3 migration.")
        org = self._org_field()
        src = tk.StringVar(); self._field("Source network ID", src)
        prefix = tk.StringVar(); self._field("Output prefix", prefix)
        self._help_line("Default: l3_migration_<network id>_named.json / _flattened.json")

        def task(button):
            org_id = org.get()
            network_id = src.get().strip()
            prefix_val = prefix.get().strip() or None
            self._run(lambda: export_l3_migration.run(self._dashboard(), org_id, network_id,
                                                       output_prefix=prefix_val),
                     button=button)
        self._go("Export L3 migration", task)

    def _panel_l3_insert(self):
        self._heading("Apply L3 insert",
                      "Insert ONE L3 firewall rule at position 1 across target networks, leaving "
                      "every other rule untouched. Dest group/object references in the rule file "
                      "are BY NAME and resolved against each target org's live IDs — a missing name "
                      "causes a refusal, never a broken rule. Re-running never stacks duplicates. "
                      "Dry run by default.")
        org = self._org_field()
        rule_file = tk.StringVar(); self._file_field("Rule file (JSON)", rule_file)
        self._help_line("A rule spec JSON with comment/policy/protocol/dest_groups/dest_objects/"
                        "dest_literals — see examples/README.md. Author this by hand or script; "
                        "the GUI does not build rule files.")
        comment = tk.StringVar(); self._field("Comment override", comment)
        nets = self._network_picker(org)

        def do(apply_bool, button):
            org_id = org.get()
            rule_file_val = rule_file.get().strip()
            comment_val = comment.get().strip() or None
            network_ids = nets.get_selected()
            cancel_event = threading.Event()

            def call():
                spec = apply_l3_insert.load_rule_file(rule_file_val)
                if comment_val:
                    spec["comment"] = comment_val
                apply_l3_insert.run(self._dashboard(), org_id, spec,
                                    network_ids=network_ids, apply=apply_bool,
                                    progress_cb=lambda d, t: self._progress.report(d, t),
                                    cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._dryrun_and_apply(do, "Insert this rule at position 1 on the targeted networks.")

    def _panel_l3_insert_batch(self):
        self._heading("Apply L3 insert (batch)",
                      "Insert one L3 rule at position 1 across a BATCH of networks, backing up each "
                      "network's original ruleset to one timestamped file first. Built for "
                      "high-blast-radius rollouts: slice a large org into batches with Skip/Limit "
                      "over a deterministic (network-id-sorted) order, verify each slice, then "
                      "proceed. Roll a batch back with 'Apply L3 restore (batch rollback)'.")
        ttk.Label(self.panel,
                  text="A live run backs up the WHOLE batch before writing anything, then inserts. "
                       "Keep batches small enough to verify between slices.",
                  wraplength=520, foreground=getattr(self, "_warn_color", "#a00")).pack(anchor="w", padx=12, pady=(0, 8))
        org = self._org_field()
        rule_file = tk.StringVar(); self._file_field("Rule file (JSON)", rule_file)
        comment = tk.StringVar(); self._field("Comment override", comment)
        nets = self._network_picker(org)
        skip = tk.StringVar(value="0"); self._field("Skip (networks)", skip)
        limit = tk.StringVar(); self._field("Limit (optional)", limit)
        self._help_line("Skip/Limit slice the network-id-sorted target list, e.g. batch 1: "
                        "Limit=250; batch 2: Skip=250, Limit=250.")
        backup_prefix = tk.StringVar(); self._field("Backup prefix", backup_prefix)

        def do(apply_bool, button):
            org_id = org.get()
            rule_file_val = rule_file.get().strip()
            comment_val = comment.get().strip() or None
            network_ids = nets.get_selected()
            backup_prefix_val = backup_prefix.get().strip() or None
            try:
                skip_val = int(skip.get() or 0)
                limit_val = int(limit.get()) if limit.get().strip() else None
            except ValueError:
                messagebox.showerror("Invalid Skip/Limit", "Skip and Limit must be whole numbers.")
                return
            cancel_event = threading.Event()

            def call():
                spec = apply_l3_insert.load_rule_file(rule_file_val)
                if comment_val:
                    spec["comment"] = comment_val
                apply_l3_insert_batch.run(self._dashboard(), org_id, spec, skip=skip_val,
                                          limit=limit_val, network_ids=network_ids,
                                          apply=apply_bool, backup_prefix=backup_prefix_val,
                                          progress_cb=lambda d, t: self._progress.report(d, t),
                                          cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._dryrun_and_apply(
            do, "Insert this rule at position 1 across the selected batch of networks "
                "(a live run backs up the batch first).")

    def _panel_l3_reinflate(self):
        self._heading("Apply L3 reinflate",
                      "Rebuild an L3 ruleset's object/group references against a TARGET org's live "
                      "IDs. Use after recreating the same-named objects/groups in the target org "
                      "(e.g. via Policy → bulk groups). A referenced name missing in the target org "
                      "causes a refusal, never a broken rule. Dry run by default.")
        org = self._org_field()
        rule_file = tk.StringVar(); self._file_field("Ruleset file", rule_file)
        self._help_line("The *_named.json produced by Export L3 migration.")
        nets = self._network_picker(org)

        def do(apply_bool, button):
            org_id = org.get()
            rule_file_val = rule_file.get().strip()
            network_ids = nets.get_selected()
            cancel_event = threading.Event()

            def call():
                ruleset = apply_l3_reinflate.load_ruleset(rule_file_val)
                apply_l3_reinflate.run(self._dashboard(), org_id, ruleset,
                                       network_ids=network_ids, apply=apply_bool,
                                       progress_cb=lambda d, t: self._progress.report(d, t),
                                       cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._dryrun_and_apply(
            do, "Replace the L3 ruleset on the targeted networks with the reinflated rules.")

    def _panel_l3_flattened(self):
        self._heading("Apply L3 flattened",
                      "Push a FLATTENED L3 ruleset (every object/group reference expanded to "
                      "literal IPs/CIDRs/FQDNs) onto target networks — the online-migration step, "
                      "so the appliance stays functional before objects/groups exist in the target "
                      "org. Refuses a ruleset that still has OBJ()/GRP() references. Dry run by "
                      "default.")
        org = self._org_field()
        rule_file = tk.StringVar(); self._file_field("Ruleset file", rule_file)
        self._help_line("The *_flattened.json produced by Export L3 migration.")
        nets = self._network_picker(org)

        def do(apply_bool, button):
            org_id = org.get()
            rule_file_val = rule_file.get().strip()
            network_ids = nets.get_selected()
            cancel_event = threading.Event()

            def call():
                ruleset = apply_l3_flattened.load_ruleset(rule_file_val)
                apply_l3_flattened.run(self._dashboard(), org_id, ruleset,
                                       network_ids=network_ids, apply=apply_bool,
                                       progress_cb=lambda d, t: self._progress.report(d, t),
                                       cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._dryrun_and_apply(
            do, "Replace the L3 ruleset on the targeted networks with the flattened ruleset.")

    def _panel_l3_restore_batch(self):
        self._heading("Apply L3 restore (batch rollback)",
                      "Roll back a batch by restoring each network's original L3 ruleset VERBATIM "
                      "from a batch backup file (made by Apply L3 insert (batch)). No org/network "
                      "selection needed — the backup file already lists exactly which networks to "
                      "restore. Dry run by default.")
        backup_file = tk.StringVar(); self._file_field("Backup file", backup_file)

        def do(apply_bool, button):
            backup_file_val = backup_file.get().strip()
            cancel_event = threading.Event()

            def call():
                backup = apply_l3_restore_batch.load_backup(backup_file_val)
                apply_l3_restore_batch.run(self._dashboard(), backup, apply=apply_bool,
                                           progress_cb=lambda d, t: self._progress.report(d, t),
                                           cancel_event=cancel_event)
            self._run(call, button=button, cancel_event=cancel_event)
        self._dryrun_and_apply(do, "Restore each backed-up network's original L3 ruleset verbatim.")


if __name__ == "__main__":
    App().mainloop()
