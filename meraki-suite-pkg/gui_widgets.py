#!/usr/bin/env python3
"""
gui_widgets.py — reusable Tkinter widgets for suite_gui.py.

Generic UI components only: nothing here imports merakicore/commands or knows
about Meraki. suite_gui.py wires these to the engines by passing plain
callables — fetch_orgs() -> list[{"id","name"}], fetch_networks(org_id) ->
list[dict] (full network dicts, at least "id"/"name") — so the same widgets
serve both the Daily Management (ID-targeted) and Migration (name-targeted)
engines.

Network calls run on a background thread; results are handed back through a
queue.Queue and applied to widgets only from a self.after() poll loop on the
main thread — the same pattern suite_gui.py's own log queue already uses, so
this stays safe under Tkinter's single-thread-touches-widgets rule.
"""

import queue
import threading
import tkinter as tk
from tkinter import ttk, filedialog


class OrgPicker(ttk.Frame):
    """
    Organization selector: an editable Combobox showing "Name — ID", backed by
    a fetch_orgs() callback, plus a refresh button.

    Duck-types a StringVar's .get()/.set() so existing call sites written as
    `org.get().strip()` keep working unchanged. Typing/pasting a raw ID (not
    picked from the list) is also supported — .get() falls back to the raw text.
    """

    def __init__(self, parent, fetch_orgs, initial=""):
        super().__init__(parent)
        self._fetch_orgs = fetch_orgs
        self._by_label = {}
        self._result_q = queue.Queue()
        self._change_cbs = []
        self._last_fired = None

        self.var = tk.StringVar(value=initial)
        self.combo = ttk.Combobox(self, textvariable=self.var, width=44)
        self.combo.pack(side="left", fill="x", expand=True)
        self.combo.bind("<<ComboboxSelected>>", lambda e: self._fire_change())
        self.combo.bind("<Return>", lambda e: self._fire_change())
        self.combo.bind("<FocusOut>", lambda e: self._fire_change())

        ttk.Button(self, text="↻ Orgs", width=8, command=self.refresh).pack(side="left", padx=(4, 0))

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var, foreground="#888").pack(side="left", padx=(6, 0))

        self._poll()

    # -- duck-typed StringVar-ish interface -----------------------------------
    def get(self):
        """Return the org ID: resolves a picked 'Name — ID' label back to its
        ID, or returns the raw text as-is (so a hand-typed/pasted ID works)."""
        text = self.var.get().strip()
        return self._by_label.get(text, text)

    def set(self, value):
        self.var.set(value)

    # -- behavior --------------------------------------------------------------
    def refresh(self):
        self.status_var.set("loading…")

        def work():
            try:
                orgs = self._fetch_orgs()
            except Exception as e:
                self._result_q.put(("error", str(e)))
                return
            self._result_q.put(("ok", orgs))

        threading.Thread(target=work, daemon=True).start()

    def on_change(self, cb):
        """Register callback(org_id) fired when the org changes (picked from
        the list, or typed/pasted and confirmed with Enter/Tab)."""
        self._change_cbs.append(cb)

    def _fire_change(self):
        val = self.get()
        if val == self._last_fired:
            return
        self._last_fired = val
        for cb in self._change_cbs:
            cb(val)

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self._result_q.get_nowait()
                if kind == "error":
                    self.status_var.set(f"error: {payload}")
                else:
                    self._by_label = {}
                    labels = []
                    for o in sorted(payload, key=lambda o: (o.get("name") or "").lower()):
                        label = f"{o.get('name', '')} — {o.get('id', '')}"
                        self._by_label[label] = o.get("id", "")
                        labels.append(label)
                    self.combo["values"] = labels
                    self.status_var.set(f"{len(labels)} org(s)")
        except queue.Empty:
            pass
        self.after(150, self._poll)


class NetworkPicker(ttk.Frame):
    """
    Checklist of an org's networks: a Treeview with a checkbox-style column,
    a live text filter, Select all / Select none, and an "All networks" toggle
    that makes get_selected() return None -- matching the existing convention
    that a blank Networks field targets every applicable network in the org.

    fetch_networks(org_id) -> list of network dicts (must include "id"/"name";
    "productTypes" or "tags" are shown if present).
    key: "id" (default, for Daily Management) or "name" (for Migration, which
    matches networks by name) -- selects what get_selected() returns.
    """

    def __init__(self, parent, fetch_networks, key="id", height=8):
        super().__init__(parent)
        self._fetch_networks = fetch_networks
        self._key = key
        self._networks = []
        self._by_id = {}
        self._order_index = {}
        self._checked = set()
        self._result_q = queue.Queue()
        self._last_org_id = None
        self._sort_col = "name"
        self._sort_reverse = False

        top = ttk.Frame(self)
        top.pack(fill="x")
        self.all_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(top, text="All networks (uncheck to pick specific ones)",
                        variable=self.all_var, command=self._on_all_toggle).pack(side="left")

        bar = ttk.Frame(self)
        bar.pack(fill="x", pady=(4, 2))
        ttk.Label(bar, text="Filter:").pack(side="left")
        self.filter_var = tk.StringVar()
        self.filter_var.trace_add("write", lambda *_a: self._apply_filter())
        ttk.Entry(bar, textvariable=self.filter_var, width=20).pack(side="left", padx=(4, 8))
        ttk.Button(bar, text="Select all", command=self._select_all).pack(side="left")
        ttk.Button(bar, text="Select none", command=self._select_none).pack(side="left", padx=(4, 0))
        ttk.Button(bar, text="↻ Load", command=self.refresh).pack(side="left", padx=(4, 0))
        self.status_var = tk.StringVar(value="(no org loaded yet)")
        ttk.Label(bar, textvariable=self.status_var, foreground="#888").pack(side="left", padx=(8, 0))

        tree_frame = ttk.Frame(self)
        tree_frame.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(tree_frame, columns=("sel", "name", "id", "tags"),
                                 show="headings", height=height, selectmode="none")
        self._col_labels = {"sel": "", "name": "Name", "id": "ID", "tags": "Type / tags"}
        for col, text, width, stretch in [
            ("sel", "", 28, False), ("name", "Name", 220, True),
            ("id", "ID", 150, False), ("tags", "Type / tags", 160, False),
        ]:
            if col == "sel":
                self.tree.heading(col, text=text)
            else:
                self.tree.heading(col, text=text, command=lambda c=col: self._sort_by(c))
            self.tree.column(col, width=width, stretch=stretch, anchor="w")
        self.tree.pack(side="left", fill="both", expand=True)
        sb = ttk.Scrollbar(tree_frame, command=self.tree.yview)
        sb.pack(side="left", fill="y")
        self.tree["yscrollcommand"] = sb.set
        self.tree.bind("<Button-1>", self._on_click)

        self._poll()

    # -- public API --------------------------------------------------------
    def load(self, org_id):
        """Fetch this org's networks in the background and populate the list."""
        if not org_id:
            return
        self._last_org_id = org_id
        self.status_var.set("loading…")

        def work():
            try:
                nets = self._fetch_networks(org_id)
            except Exception as e:
                self._result_q.put(("error", str(e)))
                return
            self._result_q.put(("ok", nets))

        threading.Thread(target=work, daemon=True).start()

    def refresh(self):
        if self._last_org_id:
            self.load(self._last_org_id)
        else:
            self.status_var.set("pick/enter an org first")

    def get_selected(self):
        """None = all applicable networks; otherwise a list of ids or names
        (per this picker's `key`) for the checked rows."""
        if self.all_var.get():
            return None
        return [n.get(self._key) or n["id"] for n in self._networks if n["id"] in self._checked]

    # -- internal ------------------------------------------------------------
    def _on_all_toggle(self):
        if self.all_var.get():
            self._checked.clear()
            for n in self._networks:
                self._refresh_row(n["id"])

    def _on_click(self, event):
        row = self.tree.identify_row(event.y)
        if not row:
            return
        if self.all_var.get():
            self.all_var.set(False)
        if row in self._checked:
            self._checked.discard(row)
        else:
            self._checked.add(row)
        self._refresh_row(row)

    def _select_all(self):
        if self.all_var.get():
            self.all_var.set(False)
        self._checked = {n["id"] for n in self._networks}
        for n in self._networks:
            self._refresh_row(n["id"])

    def _select_none(self):
        self._checked.clear()
        for n in self._networks:
            self._refresh_row(n["id"])

    def _sort_key_fn(self, col):
        if col == "id":
            return lambda n: n["id"]
        if col == "tags":
            return lambda n: ",".join(n.get("productTypes") or n.get("tags") or [])
        return lambda n: (n.get("name") or "").lower()

    def _sort_by(self, col):
        if self._sort_col == col:
            self._sort_reverse = not self._sort_reverse
        else:
            self._sort_col = col
            self._sort_reverse = False
        self._networks.sort(key=self._sort_key_fn(col), reverse=self._sort_reverse)
        self._order_index = {n["id"]: i for i, n in enumerate(self._networks)}
        for i, n in enumerate(self._networks):
            self.tree.move(n["id"], "", i)
        self._update_headings()

    def _update_headings(self):
        for col, label in self._col_labels.items():
            if col == "sel":
                continue
            suffix = ""
            if col == self._sort_col:
                suffix = " ▼" if self._sort_reverse else " ▲"
            self.tree.heading(col, text=label + suffix)

    def _refresh_row(self, nid):
        n = self._by_id.get(nid)
        if not n:
            return
        mark = "☑" if nid in self._checked else "☐"
        tags = ",".join(n.get("productTypes") or n.get("tags") or [])
        self.tree.item(nid, values=(mark, n.get("name", ""), nid, tags))

    def _apply_filter(self):
        if not self._networks:
            return
        needle = self.filter_var.get().strip().lower()
        attached = set(self.tree.get_children(""))
        for n in self._networks:
            nid = n["id"]
            visible = (not needle) or (needle in (n.get("name") or "").lower())
            is_attached = nid in attached
            if visible and not is_attached:
                self.tree.reattach(nid, "", self._order_index.get(nid, "end"))
            elif not visible and is_attached:
                self.tree.detach(nid)

    def _populate(self, nets):
        for old_id in list(self._by_id.keys()):
            if self.tree.exists(old_id):
                self.tree.delete(old_id)

        self._networks = sorted(nets, key=self._sort_key_fn(self._sort_col),
                                reverse=self._sort_reverse)
        self._by_id = {n["id"]: n for n in nets}
        self._order_index = {}
        self._checked = set()
        for i, n in enumerate(self._networks):
            nid = n["id"]
            self._order_index[nid] = i
            tags = ",".join(n.get("productTypes") or n.get("tags") or [])
            self.tree.insert("", "end", iid=nid, values=("☐", n.get("name", ""), nid, tags))
        self.status_var.set(f"{len(nets)} network(s)")
        self._update_headings()
        self._apply_filter()

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, payload = self._result_q.get_nowait()
                if kind == "error":
                    self.status_var.set(f"error: {payload}")
                else:
                    self._populate(payload)
        except queue.Empty:
            pass
        self.after(150, self._poll)


class LogToolbar(ttk.Frame):
    """Clear / Save-to-file controls for a tk.Text log widget."""

    def __init__(self, parent, text_widget):
        super().__init__(parent)
        self._text = text_widget
        ttk.Button(self, text="Clear", command=self.clear).pack(side="left")
        ttk.Button(self, text="Save to file…", command=self.save).pack(side="left", padx=(6, 0))

    def clear(self):
        prev_state = self._text["state"]
        self._text["state"] = "normal"
        self._text.delete("1.0", "end")
        self._text["state"] = prev_state

    def save(self):
        path = filedialog.asksaveasfilename(
            defaultextension=".txt",
            filetypes=[("Text files", "*.txt"), ("All files", "*.*")],
            initialfile="meraki_suite_output.txt",
        )
        if not path:
            return
        content = self._text.get("1.0", "end-1c")
        with open(path, "w") as fh:
            fh.write(content)


class ProgressBar(ttk.Frame):
    """
    Status label + progress bar + Cancel button for a running operation.
    Hidden until start() is called; hides itself again once finish() is
    drained. report()/finish() are safe to call from a worker thread -- they
    only push onto a queue.Queue, applied to widgets by the same
    self.after()-poll pattern OrgPicker/NetworkPicker use.

    Engine functions that don't accept progress_cb/cancel_event (plain reads)
    simply never call report(), so the bar just shows an indeterminate
    "Running..." state between start() and finish() for those.
    """

    def __init__(self, parent):
        super().__init__(parent)
        self._result_q = queue.Queue()
        self._cancel_cb = None
        self._finish_cb = None
        self._visible = False

        self.status_var = tk.StringVar(value="")
        ttk.Label(self, textvariable=self.status_var).pack(side="left", padx=(0, 6))
        self.bar = ttk.Progressbar(self, length=180)
        self.bar.pack(side="left", padx=(0, 6))
        self.cancel_btn = ttk.Button(self, text="Cancel", command=self._on_cancel_click,
                                     state="disabled")
        self.cancel_btn.pack(side="left")

        self._poll()

    def on_cancel(self, cb):
        """Register callback() fired when Cancel is clicked."""
        self._cancel_cb = cb

    def on_finish(self, cb):
        """Register callback() fired (on the main thread) once finish() is
        drained -- e.g. to re-enable a Run button. Overwriting a previous
        registration is fine: only one operation runs at a time."""
        self._finish_cb = cb

    def _on_cancel_click(self):
        if self._cancel_cb:
            self._cancel_cb()
        self.status_var.set("Cancelling…")
        self.cancel_btn.configure(state="disabled")

    def start(self):
        """Show the bar (indeterminate until the first report()) and enable
        Cancel. Call on the main thread when a run begins."""
        if not self._visible:
            self.pack(side="left", padx=(8, 0))
            self._visible = True
        self.bar.configure(mode="indeterminate", maximum=100)
        self.bar.start(12)
        self.status_var.set("Running…")
        self.cancel_btn.configure(state="normal")

    def report(self, done, total):
        """Thread-safe: call from any thread with progress so far."""
        self._result_q.put(("progress", done, total))

    def finish(self):
        """Thread-safe: call from any thread when the run has ended."""
        self._result_q.put(("finish", None, None))

    def _poll(self):
        if not self.winfo_exists():
            return
        try:
            while True:
                kind, done, total = self._result_q.get_nowait()
                if kind == "finish":
                    self.bar.stop()
                    if self._visible:
                        self.pack_forget()
                        self._visible = False
                    self.cancel_btn.configure(state="disabled")
                    self.status_var.set("")
                    if self._finish_cb:
                        self._finish_cb()
                else:
                    if str(self.bar["mode"]) == "indeterminate":
                        self.bar.stop()
                        self.bar.configure(mode="determinate")
                    self.bar.configure(maximum=max(total, 1), value=done)
                    self.status_var.set(f"{done}/{total}")
        except queue.Empty:
            pass
        self.after(100, self._poll)
