# Meraki Suite — User Guide

A desktop tool for day-to-day Meraki management and cross-org migration. It is one
window with two sections — **Daily Management** and **Migration** — each backed by
its own independent engine. This guide explains setup, the concepts that apply
everywhere, and every function with its inputs and safety notes.

---

## 1. Setup & running

This tool runs as a Python script — there is no installer. Each machine needs a
one-time setup, then you launch it the same way every time.

### First time on a new machine (one-time)

1. **Install Python 3** (3.10 or newer). Get it from python.org. On Windows,
   during install, **check "Add Python to PATH"** — this avoids the most common
   "python is not recognized" error.
   - Verify it worked: open a terminal / Command Prompt and run `python --version`.
     You should see a version number.

2. **Install the Meraki library.** In a terminal, run:
   ```
   pip install meraki
   ```
   (If `pip` isn't found, try `python -m pip install meraki`.)

3. **Get a Meraki Dashboard API key:** Dashboard → your profile → API access →
   generate a key. Copy it somewhere safe — treat it like a password.

That's the whole one-time setup: Python, the meraki library, and an API key.

### Launching the app

Put the suite folder somewhere on the machine, open a terminal **in that folder**,
and run:
```
python suite_gui.py
```

**Easier option (Windows):** double-click `run.bat` in the folder instead of
typing the command. (Mac/Linux: `run.sh`.) These just launch the app and keep the
window open if there's an error so you can read it.

### Providing the API key

You can either:
- **Paste it into the top bar** of the window each time you launch, **or**
- **Set it once as an environment variable** so it pre-fills automatically:
  ```
  Windows (PowerShell):  $env:MERAKI_DASHBOARD_API_KEY="your-key-here"
  macOS / Linux:         export MERAKI_DASHBOARD_API_KEY="your-key-here"
  ```
  (Set this way, it lasts for that terminal session. To make it permanent, add it
  to your system environment variables / shell profile.)

**Dark mode** toggles in the top-right of the window.

> The API key has the same access your dashboard account does. Treat it like a
> password. It is never written to disk by this tool.

### If it doesn't start

- **"python is not recognized"** → Python isn't installed or wasn't added to PATH.
  Reinstall Python and check "Add Python to PATH", or use the full path to python.
- **"No module named 'meraki'"** → the library isn't installed; run
  `pip install meraki` (or `python -m pip install meraki`).
- **"No module named 'merakicore' / 'common'"** → you're not running from inside
  the suite folder. `cd` into the folder that contains `suite_gui.py` first, or
  use the `run.bat` / `run.sh` launcher (which handles this).
- **Window opens but operations fail with an auth error** → the API key is wrong,
  missing, or lacks permission. Re-check the key in the top bar.

---


## 2. Concepts that apply everywhere

**Organization ID.** Most functions need the org you're working in. Find it with
**List organizations**, then paste the ID into the Organization ID field (or set
`MERAKI_ORG_ID` to pre-fill it). **Always confirm the org ID before any write —
it is the one thing standing between a change to your test org and a change to
production.**

**Network IDs.** Functions that act on specific networks take network IDs (not
names — names can repeat or be renamed). Find them with **Export networks**.
Leave a "Networks" field blank to target all applicable networks in the org.

**Read-only vs. write.** Read-only functions (List orgs, all Exports, Policy
check, Backup) only fetch data and write local files — they never change the
dashboard. Write functions (Apply L7, Apply content filter, Policy -> bulk groups,
Restore, Restore switch ports) change live configuration.

**Dry run.** Every write function defaults to a **dry run** — it shows what it
*would* do and changes nothing. Read the Output box, confirm it's correct, then
uncheck "Dry run" and run again. A confirmation dialog appears before any real
write. **Make a habit of always dry-running first.**

**The propagate model.** This tool *manages and propagates* existing
configuration — it does not author it. The intended pattern: configure one
network/object the way you want in the dashboard, **export** it, then **apply** it
to others. The dashboard is where you build; the tool is how you replicate.

**Output box.** Every operation streams its progress into the Output box at the
bottom. Long operations (big backups, bulk policy runs) may pause briefly while
the Meraki API rate limit is respected — that is normal, not a freeze. Don't
close the window mid-operation.

---

## 3. Daily Management

### List organizations
- **Purpose:** Show every org your API key can see, with IDs. Run this first to
  find the org ID you'll use elsewhere.
- **Inputs:** none.
- **Safety:** read-only.

### Export networks
- **Purpose:** List the org's networks (with IDs, product types, tags) to a JSON
  or CSV file. Use it to find the network IDs other functions need.
- **Inputs:** Organization ID; optional list of network IDs to restrict to;
  format (json/csv).
- **Safety:** read-only.

### Export firewall rules
- **Purpose:** Export L3 firewall rules and switch ACL rules. Policy-object
  references (OBJ/GRP) are resolved to names and also expanded to the underlying
  IPs/FQDNs, so you can read exactly what each rule allows or blocks.
- **Inputs:** Organization ID; optional network IDs; format (csv/json). Produces
  one file for L3 rules and one for switch ACLs.
- **Safety:** read-only.

### Export L7 (from a network)
- **Purpose:** Copy one network's Layer-7 firewall rules to a JSON file. This is
  the "copy from" source for **Apply L7 rules**.
- **Inputs:** Organization ID; the source network ID.
- **Safety:** read-only.

### Export content filter
- **Purpose:** Copy one network's content-filtering settings (blocked/allowed URL
  patterns and categories) to a JSON file. The "copy from" source for **Apply
  content filter**. Category formats are normalized so the file can be applied
  directly.
- **Inputs:** Organization ID; the source network ID.
- **Safety:** read-only.

### Policy check (audit)
- **Purpose:** Check whether specific FQDNs/IPs already exist as policy objects —
  in one org or across all orgs. Useful before adding objects.
- **Inputs:** FQDNs and/or IPs (typed in), or a file of entries. Leave
  Organization ID blank to check **all** orgs; set it to check just one.
- **Safety:** read-only.

### Apply L7 rules  *(write)*
- **Purpose:** Push L7 firewall rules from a JSON file (made by Export L7) onto
  target networks.
- **Inputs:** Organization ID; the source JSON file; optional target network IDs
  (blank = all appliance networks); **Mode** (replace = target ends up matching
  the source; append = add the source's rules to what's already there); append
  **position** (top/bottom).
- **Safety:** dry run by default; confirmation before writing. Replace overwrites
  the target's L7 rules.

### Apply content filter  *(write)*
- **Purpose:** Push content-filtering settings from a JSON file (made by Export
  content filter) onto target networks.
- **Inputs:** Organization ID; source JSON file; optional target network IDs;
  **Mode** (replace = match source; append = merge source into existing, deduped).
- **Safety:** dry run by default; confirmation before writing.

### Policy -> group  *(write)*
- **Purpose:** Create policy objects (FQDN/IP) and add them to **one** group.
- **Inputs:** FQDNs/IPs and/or a file; optional object name base.
- **Note / limitation:** In the GUI this panel runs **dry-run preview only**,
  because choosing the org and group is interactive. For a live single-group
  apply, use the command line, **or** use **Policy -> bulk groups**, which is fully
  GUI-driven.
- **Safety:** dry run only in GUI.

### Policy -> bulk groups  *(write)*
- **Purpose:** Create many policy objects from a file and spread them across a
  numbered series of groups (`PREFIX 1`, `PREFIX 2`, ...), because a single group
  has a member cap.
- **Inputs:** Organization ID; a file of entries; **Group prefix** (the group
  series name); **Object name base** (objects become `BASE 1`, `BASE 2`, ...);
  **Group size** (max members per group).
- **Group size cap:** A policy object group holds a maximum of **150** objects.
  Keep Group size at or below 150 (140 is a safe default); lower it for lower-end
  MX models.
- **How placement works:** An entry already present in any `PREFIX N` group is
  left where it is; genuinely-new entries fill the first group with room, creating
  the next numbered group as needed. **This makes it idempotent** — re-running
  after the list grows only adds the new entries, and a run that's interrupted can
  simply be re-run.
- **Safety:** dry run shows the full plan (objects to create, groups to
  create/update, per-group counts); confirmation before writing. Updating a group
  affects every firewall rule that references it, across all networks.

---

## 4. Migration

The Migration tool is for moving an org's configuration to another org (e.g.
across accounts). It is heavier and broader than Daily Management — treat it with
extra care.

### List networks in a backup
- **Purpose:** Show the network NAMES and IDs stored in a backup folder, so you can
  see exactly what a restore would target before running it.
- **Inputs:** the backup folder.
- **Safety:** read-only (reads the local backup file only).
- **Why it matters:** Migration restore matches networks by **name**. Use this to
  confirm the exact names to type into a restore's Networks field.

### Backup an org
- **Purpose:** Save an org's policy objects and/or network settings (VLANs,
  routes, firewall, content filtering, VPN, etc.) to a timestamped folder.
- **Inputs:** Organization ID; **Scope** (objects / networks / all); optional
  network IDs; backup location (a folder you choose).
- **Safety:** read-only — it only reads from the org and writes local files.

### Restore to an org  *(write)*
- **Purpose:** Write configuration from a backup folder into a target org.
  Name-matched settings are updated; missing ones are created.
- **Inputs:** Organization ID (the **target**); **Scope** (objects / networks /
  all / vpn); optional network IDs; the backup folder to restore from.
- **Order matters:** for a full migration, restore **objects** before
  **networks**, so firewall rules can resolve their object references. ("all" does
  this in the right order.)
- **Networks field:** uses network **names** (not IDs). Leave blank for all. The
  dry-run output lists exactly which networks matched — read it to confirm scope.
- **Safety:** dry run by default; a strong confirmation dialog before writing. A
  live restore changes configuration across the target org — always dry-run first
  and double-check the target org ID.

### Restore switch ports  *(write)*
- **Purpose:** Apply per-port switch configuration from a backup onto the target
  org's switches. Matching is by **device name** — a new switch named exactly like
  the backed-up one inherits its port config.
- **Inputs:** Organization ID (target); optional network IDs; the backup folder.
- **Run order:** do this **after** the new switches are claimed into the target
  networks and named to match the originals.
- **Safety:** dry run by default; confirmation before writing. A live run
  overwrites port configs on name-matched switches.

---

## 5. A typical regional-settings workflow (example)

You want APAC sites to block certain content the others don't:

1. **List organizations** -> note your org ID.
2. **Export networks** -> find the network IDs of your APAC sites and a
   well-configured "template" site.
3. In the **dashboard**, configure the template site's content filtering exactly
   as you want it.
4. **Export content filter** from the template site -> produces a JSON file.
5. **Apply content filter**, From file = that JSON, Networks = the APAC site IDs,
   Dry run **on** -> review the Output.
6. Uncheck Dry run, run again, confirm -> the APAC sites now match the template.

---

## 6. Command-line use (optional)

Each engine also works from the command line if you prefer scripting:
- Daily Management: `python meraki_tools_pkg/cli.py ...`
  (e.g. `export orgs`, `export networks`, `apply policy-bulk ...`)
- Migration: `python migration_pkg/main.py ...`
  (e.g. `backup`, `restore`, `restore-ports`)

Run either with `-h` to see all options. The GUI and CLI use the same engines —
the GUI is just an alternative front end.

---

## 7. Input files (see the `examples/` folder)

Some functions take a file. The `examples/` folder has ready-to-use samples.

**Files you write yourself:**
- **`policy_entries.txt`** — the list of IPs / CIDRs / FQDNs for **Policy check**,
  **Policy → group**, and **Policy → bulk groups**. One entry per line; plain
  lines are auto-typed, or prefix with `ip,` / `fqdn,` to be explicit; `#` lines
  are comments. Copy it, put in your real values, and select it in the "From
  file" field.

**Files the tool generates (don't hand-write these):**
- **`sample_l7_export.json`** / **`sample_content_filter_export.json`** — examples
  of what **Export L7** and **Export content filter** produce. You feed files like
  these to the matching **Apply** function. **Always get the real file from the
  Export function** — configure one network in the dashboard, export it, then
  apply it elsewhere. The samples are only so you can recognize a valid file; the
  Meraki-specific IDs inside (application IDs, category IDs) must come from a real
  export, not be typed by hand.

See `examples/README.md` for details on each file.

---

## 8. Safety checklist (read before any live write)

- Is the **Organization ID** the one I intend (test vs. production)?
- Have I run it as a **dry run** and read the Output?
- For Restore: am I restoring **into** the right target, from the right backup?
- For bulk policy: is **Group size** <= 150?
- For switch ports: are the new switches **claimed and named** to match first?
