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
import threading
import tkinter as tk
from contextlib import redirect_stdout
from tkinter import ttk, filedialog, messagebox

# --- make both engines importable without modifying their code ---------------
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "meraki_tools_pkg"))   # for `merakicore`, `commands`
sys.path.insert(0, os.path.join(HERE, "migration_pkg"))      # for `common`, etc.

# meraki-tools engine
from merakicore import client as mt_client
from merakicore import policyobjects as po
from commands import (
    export_orgs, export_networks, export_firewall, export_l7,
    export_content_filter, export_policy_check,
    apply_l7, apply_content_filter, apply_policy_group, apply_policy_bulk,
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
    def __init__(self):
        super().__init__()
        self.title("Meraki Suite")
        self.geometry("900x680")
        self.log_queue = queue.Queue()

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

        # Dark-mode toggle (themes are applied via _apply_theme).
        self.dark_mode = tk.BooleanVar(value=False)
        ttk.Checkbutton(top, text="Dark mode", variable=self.dark_mode,
                        command=self._apply_theme).pack(side="right")

        # Body: left nav + right panel
        body = ttk.Frame(self); body.pack(fill="both", expand=True, padx=8, pady=4)

        nav = ttk.Frame(body); nav.pack(side="left", fill="y")
        self.tree = ttk.Treeview(nav, show="tree", selectmode="browse")
        self.tree.column("#0", width=200, stretch=False)
        self.tree.pack(side="left", fill="y")
        self.tree.bind("<<TreeviewSelect>>", self._on_select)

        self.panel = ttk.Frame(body)
        self.panel.pack(side="left", fill="both", expand=True, padx=(8, 0))

        # Log
        logf = ttk.LabelFrame(self, text="Output")
        logf.pack(fill="both", expand=True, padx=8, pady=6)
        self.log = tk.Text(logf, height=12, wrap="word", state="disabled")
        self.log.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(logf, command=self.log.yview); sb.pack(side="right", fill="y")
        self.log["yscrollcommand"] = sb.set

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
        style.configure("TButton", background=p["field"], foreground=p["fg"])
        style.map("TButton",
                  background=[("active", p["sel"])],
                  foreground=[("active", p["sel_fg"])])
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

        # The log is a plain tk.Text — recolor it directly.
        self.log.configure(bg=p["log_bg"], fg=p["log_fg"], insertbackground=p["fg"])

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

    def _log(self, text):
        self.log["state"] = "normal"; self.log.insert("end", text)
        self.log.see("end"); self.log["state"] = "disabled"

    def _drain(self):
        while not self.log_queue.empty():
            self._log(self.log_queue.get_nowait())
        self.after(80, self._drain)

    def _run(self, fn):
        """Run fn() on a worker thread, streaming its stdout into the log."""
        if not self._sync_key():
            return
        writer = _QueueWriter(self.log_queue)

        def worker():
            try:
                with redirect_stdout(writer):
                    fn()
            except SystemExit as e:
                self.log_queue.put(f"\n[stopped] {e}\n")
            except Exception as e:
                self.log_queue.put(f"\n[error] {e}\n")
        threading.Thread(target=worker, daemon=True).start()

    def _dashboard(self):
        return mt_client.get_dashboard()

    # -- panel rendering -----------------------------------------------------
    def _render_panel(self, iid):
        self._clear_panel()
        builder = getattr(self, "_panel_" + iid.replace(":", "_"), None)
        if builder:
            builder()
        else:
            ttk.Label(self.panel, text="(not yet implemented)").pack(padx=12, pady=12)

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
        ttk.Label(row, text=label, width=22, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=width).pack(side="left", fill="x", expand=True)
        return row

    def _help_line(self, text):
        ttk.Label(self.panel, text=text, wraplength=520,
                  foreground=getattr(self, "_muted_color", "#555")).pack(anchor="w", padx=12, pady=(0, 4))

    def _org_field(self):
        """Org ID field that defaults to MERAKI_ORG_ID; returns the StringVar."""
        var = tk.StringVar(value=os.environ.get("MERAKI_ORG_ID", ""))
        self._field("Organization ID", var)
        return var

    def _split(self, text):
        """Split a space/comma list into a clean list, or None if empty."""
        items = [x.strip() for x in text.replace(",", " ").split() if x.strip()]
        return items or None

    def _go(self, label, fn):
        ttk.Button(self.panel, text=label, command=lambda: self._run(fn)).pack(anchor="w", padx=12, pady=12)

    # ---- read-only Daily panels -------------------------------------------
    def _panel_d_networks(self):
        self._heading("Export networks",
                      "Read-only. Lists the org's networks (all, or a chosen subset) to JSON or CSV.")
        org = self._org_field()
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network IDs (not names), comma/space separated. Leave blank to target all applicable networks.")
        fmt = tk.StringVar(value="json")
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="Format", width=22, anchor="w").pack(side="left")
        ttk.Combobox(row, textvariable=fmt, values=["json", "csv"], width=8, state="readonly").pack(side="left")

        def task():
            export_networks.run(self._dashboard(), org.get().strip(),
                                network_ids=self._split(nets.get()), fmt=fmt.get())
        self._go("Export networks", task)

    def _panel_d_firewall(self):
        self._heading("Export firewall rules",
                      "Read-only. L3 firewall + switch ACL rules, with OBJ/GRP tokens resolved and expanded.")
        org = self._org_field()
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network IDs (not names), comma/space separated. Leave blank to target all applicable networks.")
        fmt = tk.StringVar(value="csv")
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text="Format", width=22, anchor="w").pack(side="left")
        ttk.Combobox(row, textvariable=fmt, values=["csv", "json"], width=8, state="readonly").pack(side="left")

        def task():
            export_firewall.run(self._dashboard(), org.get().strip(),
                                network_ids=self._split(nets.get()), fmt=fmt.get())
        self._go("Export firewall rules", task)

    def _panel_d_export_l7(self):
        self._heading("Export L7 rules from a network",
                      "Read-only. Copies one network's L7 firewall rules to JSON — the 'copy from' "
                      "source for Apply L7.")
        org = self._org_field()
        src = tk.StringVar(); self._field("Source network ID", src)

        def task():
            export_l7.run(self._dashboard(), org.get().strip(), src.get().strip())
        self._go("Export L7 rules", task)

    def _panel_d_export_cf(self):
        self._heading("Export content filtering from a network",
                      "Read-only. Copies one network's content-filtering settings to JSON — the "
                      "'copy from' source for Apply content filter.")
        org = self._org_field()
        src = tk.StringVar(); self._field("Source network ID", src)

        def task():
            export_content_filter.run(self._dashboard(), org.get().strip(), src.get().strip())
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
        ttk.Label(row, text="From file (optional)", width=22, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=ff, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: ff.set(filedialog.askopenfilename() or ff.get())).pack(side="left", padx=4)

        def task():
            entries = po.parse_entries(fqdns=self._split(fqdns.get()),
                                       ips=self._split(ips.get()),
                                       from_file=(ff.get().strip() or None))
            export_policy_check.run(self._dashboard(), entries,
                                    org_id=(org.get().strip() or None))
        self._go("Run policy check", task)

    # ---- write Daily panels ------------------------------------------------
    def _file_field(self, label, var):
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text=label, width=22, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: var.set(filedialog.askopenfilename() or var.get())).pack(side="left", padx=4)

    def _dryrun_and_apply(self, do_apply, what):
        """
        Standard write-panel footer: a dry-run checkbox (default ON) and a Run
        button. do_apply(apply_bool) performs the engine call. On a real apply
        (dry-run unchecked) we show a confirmation dialog first — this replaces
        the CLI's stdin prompt.
        """
        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.panel, text="Dry run (preview only — no changes written)",
                        variable=dry).pack(anchor="w", padx=12, pady=(10, 2))

        def run_clicked():
            if not dry.get():
                if not messagebox.askyesno(
                        "Confirm write",
                        f"This will WRITE changes to live networks:\n\n{what}\n\n"
                        "Have you reviewed a dry run first?\n\nProceed?",
                        icon="warning", default="no"):
                    return
            self._run(lambda: do_apply(not dry.get()))

        ttk.Button(self.panel, text="Run", command=run_clicked).pack(anchor="w", padx=12, pady=10)

    def _panel_d_apply_l7(self):
        self._heading("Apply L7 rules",
                      "Propagate L7 firewall rules from a JSON file (made by Export L7) onto target "
                      "networks. Dry run by default.")
        org = self._org_field()
        src = tk.StringVar(); self._file_field("From file", src)
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network IDs (not names), comma/space separated. Leave blank to target all applicable networks.")
        mode = tk.StringVar(value="replace")
        pos = tk.StringVar(value="bottom")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Mode", width=22, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=mode, values=["replace", "append"], width=10, state="readonly").pack(side="left")
        ttk.Label(r, text="Append position").pack(side="left", padx=(12, 4))
        ttk.Combobox(r, textvariable=pos, values=["bottom", "top"], width=8, state="readonly").pack(side="left")

        def do(apply_bool):
            apply_l7.run(self._dashboard(), org.get().strip(), src.get().strip(),
                         network_ids=self._split(nets.get()), mode=mode.get(),
                         position=pos.get(), apply=apply_bool)
        self._dryrun_and_apply(do, "Apply L7 rules to the targeted networks.")

    def _panel_d_apply_cf(self):
        self._heading("Apply content filter",
                      "Propagate content-filtering settings from a JSON file (made by Export content "
                      "filter) onto target networks. Dry run by default.")
        org = self._org_field()
        src = tk.StringVar(); self._file_field("From file", src)
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network IDs (not names), comma/space separated. Leave blank to target all applicable networks.")
        mode = tk.StringVar(value="replace")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Mode", width=22, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=mode, values=["replace", "append"], width=10, state="readonly").pack(side="left")

        def do(apply_bool):
            apply_content_filter.run(self._dashboard(), org.get().strip(), src.get().strip(),
                                     network_ids=self._split(nets.get()), mode=mode.get(),
                                     apply=apply_bool)
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

        def do(apply_bool):
            entries = po.parse_entries(from_file=(src.get().strip() or None))
            try:
                gsize = int(size.get())
            except ValueError:
                print("  group size must be a number"); return
            apply_policy_bulk.run(self._dashboard(), entries, org_id=org.get().strip(),
                                  group_prefix=prefix.get().strip(), group_size=gsize,
                                  name_base=(name.get().strip() or None),
                                  apply=apply_bool, assume_yes=True)
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

        def task():
            entries = po.parse_entries(fqdns=self._split(fqdns.get()),
                                       ips=self._split(ips.get()),
                                       from_file=(ff.get().strip() or None))
            # dry run only from GUI (engine prompts for org/group on real apply)
            apply_policy_group.run(self._dashboard(), entries, apply=False,
                                   name_base=(name.get().strip() or None), assume_yes=True)
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

        def task():
            import json
            folder = bdir.get().strip()
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
        self._go("List networks", task)

    def _dir_field(self, label, var):
        row = ttk.Frame(self.panel); row.pack(fill="x", padx=12, pady=3)
        ttk.Label(row, text=label, width=22, anchor="w").pack(side="left")
        ttk.Entry(row, textvariable=var, width=34).pack(side="left", fill="x", expand=True)
        ttk.Button(row, text="…", width=3,
                   command=lambda: var.set(filedialog.askdirectory() or var.get())).pack(side="left", padx=4)

    def _panel_m_mig_backup(self):
        self._heading("Backup an org (Migration)",
                      "Read-only. Saves an org's policy objects and/or network settings to a "
                      "timestamped folder. This is the cross-org migration tool — separate from "
                      "Daily Management.")
        org = self._org_field()
        scope = tk.StringVar(value="all")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Scope", width=22, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=scope, values=["objects", "networks", "all"],
                     width=12, state="readonly").pack(side="left")
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network NAMES (not IDs). Leave blank to back up all networks.")
        dest = tk.StringVar(value=os.path.abspath("./backups"))
        self._dir_field("Backup location", dest)

        def task():
            dash = self._mig_dashboard()
            org_id = org.get().strip()
            o = dash.organizations.getOrganization(organizationId=org_id)
            stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
            safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in o["name"])
            backup_dir = os.path.join(dest.get().strip() or "./backups",
                                      f"{org_id}_{safe}_{stamp}")
            print(f"Backup of '{o['name']}' ({org_id}) -> {backup_dir}")
            nl = self._split(nets.get())
            if scope.get() in ("objects", "all"):
                mig_policy_objects.backup(dash, org_id, backup_dir)
            if scope.get() in ("networks", "all"):
                mig_network_settings.backup(dash, org_id, backup_dir, network_filter=nl)
            print(f"\nDone. Backup folder: {backup_dir}")
        self._go("Run backup", task)

    def _panel_m_mig_restore(self):
        self._heading("Restore to an org (Migration)",
                      "Writes config from a backup folder into a target org. Name-matched settings are "
                      "updated and missing ones created. Dry run by default — review it before applying.")
        ttk.Label(self.panel,
                  text="WARNING: a live restore changes configuration across the target org and "
                       "affects networks/rules at scale. Always dry-run first and confirm the org.",
                  wraplength=520, foreground=getattr(self, "_warn_color", "#a00")).pack(anchor="w", padx=12, pady=(0, 8))
        org = self._org_field()
        scope = tk.StringVar(value="all")
        r = ttk.Frame(self.panel); r.pack(fill="x", padx=12, pady=3)
        ttk.Label(r, text="Scope", width=22, anchor="w").pack(side="left")
        ttk.Combobox(r, textvariable=scope, values=["objects", "networks", "all", "vpn"],
                     width=12, state="readonly").pack(side="left")
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network NAMES (not IDs), comma/space separated. Leave blank to restore "
                        "ALL networks in the backup. Use 'List networks in a backup' to see names. "
                        "The dry-run output shows exactly which networks matched.")
        bdir = tk.StringVar(); self._dir_field("Backup folder", bdir)

        def do(apply_bool):
            dash = self._mig_dashboard()
            org_id = org.get().strip()
            backup_dir = bdir.get().strip()
            if not backup_dir or not os.path.isdir(backup_dir):
                print("  pick a valid backup folder first"); return
            o = dash.organizations.getOrganization(organizationId=org_id)
            print(f"Restore from {backup_dir} -> '{o['name']}' ({org_id})  "
                  f"scope={scope.get()}  dry_run={not apply_bool}")
            nl = self._split(nets.get())
            dry = not apply_bool
            if scope.get() == "vpn":
                mig_network_settings.restore_vpn_only(dash, org_id, backup_dir,
                                                      network_filter=nl, dry_run=dry)
                print("\nDone."); return
            id_map = None
            if scope.get() in ("objects", "all"):
                id_map = mig_policy_objects.restore(dash, org_id, backup_dir, dry_run=dry)
            if scope.get() in ("networks", "all"):
                mig_network_settings.restore(dash, org_id, backup_dir, dry_run=dry,
                                             network_filter=nl, id_map=id_map)
            print("\nDone.")

        # restore uses the same dry-run footer; the confirm dialog wording is stronger
        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.panel, text="Dry run (preview only — no changes written)",
                        variable=dry).pack(anchor="w", padx=12, pady=(10, 2))

        def run_clicked():
            if not dry.get():
                if not messagebox.askyesno(
                        "Confirm LIVE restore",
                        f"LIVE RESTORE into org {org.get().strip()} (scope: {scope.get()}).\n\n"
                        "Name-matched settings will be overwritten and missing ones created, "
                        "across the target org.\n\nHave you reviewed a dry run?\n\nProceed?",
                        icon="warning", default="no"):
                    return
            self._run(lambda: do(not dry.get()))
        ttk.Button(self.panel, text="Run restore", command=run_clicked).pack(anchor="w", padx=12, pady=10)

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
        org = self._org_field()
        nets = tk.StringVar(); self._field("Networks (optional)", nets)
        self._help_line("Network NAMES (not IDs). Leave blank for all networks in the backup. "
                        "Dry-run output shows which networks matched.")
        bdir = tk.StringVar(); self._dir_field("Backup folder", bdir)

        def do(apply_bool):
            dash = self._mig_dashboard()
            org_id = org.get().strip()
            backup_dir = bdir.get().strip()
            if not backup_dir or not os.path.isdir(backup_dir):
                print("  pick a valid backup folder first"); return
            o = dash.organizations.getOrganization(organizationId=org_id)
            print(f"Switch port restore from {backup_dir} -> '{o['name']}' ({org_id})  "
                  f"dry_run={not apply_bool}")
            mig_switch_settings.restore_ports(dash, org_id, backup_dir,
                                              network_filter=self._split(nets.get()),
                                              dry_run=not apply_bool)

        dry = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.panel, text="Dry run (preview only — no changes written)",
                        variable=dry).pack(anchor="w", padx=12, pady=(10, 2))

        def run_clicked():
            if not dry.get():
                if not messagebox.askyesno(
                        "Confirm switch port restore",
                        f"This will OVERWRITE port configs on name-matched switches in org "
                        f"{org.get().strip()}.\n\nHave you reviewed a dry run?\n\nProceed?",
                        icon="warning", default="no"):
                    return
            self._run(lambda: do(not dry.get()))
        ttk.Button(self.panel, text="Run port restore", command=run_clicked).pack(anchor="w", padx=12, pady=10)


if __name__ == "__main__":
    App().mainloop()
