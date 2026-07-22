# Meraki Tools

A consolidated command-line tool for common Meraki operations: discovering orgs
and networks, exporting configuration, and propagating settings across a chosen
set of networks. It replaces a collection of separate single-purpose scripts
with one tool built on a shared core.

The guiding idea: this tool **manages and propagates existing configuration; it
does not author it.** You configure one network in the dashboard (where you have
the full UI), export its config, and apply that same config to other networks.

## Design principles

- **One client.** All authentication goes through `merakicore/client.py`, using
  the `MERAKI_DASHBOARD_API_KEY` environment variable. The key is never stored
  in code or config.
- **Live data every run.** Commands fetch the org's current networks on each run,
  so newly added locations appear automatically — there is no snapshot file to
  regenerate.
- **Target by network ID.** Operations act on explicit network IDs, validated
  against the live org. IDs are unambiguous (unlike names, which can repeat or be
  renamed). Names are shown only for confirmation.
- **Fetch once, format locally.** JSON is the canonical, round-trippable format
  (used for export → apply). CSV is a read-only view for humans, not a write path.
- **One default output location.** When you don't pass `--output`, every
  export/apply-batch command writes into a shared `output/` tree at the repo
  root (`output/exports/`, `output/l3_migration/`, `output/l3_batches/`) —
  see `merakicore/paths.py`. Pass `--output <path>` any time you want it
  somewhere specific instead.

## Two tiers of commands (safety)

- **`export` — read-only.** Only GETs data and writes local files. Cannot change
  anything in the dashboard. Safe to run anytime.
- **`apply` — writes to live networks.** Changes configuration on the targeted
  networks. Every `apply` command **defaults to a dry run** (preview only) and
  requires both the `--apply` flag and an interactive confirmation before making
  any change. Each network is handled independently — one failure does not stop
  the rest — and per-network API errors are shown, not swallowed.

## Setup

```
pip install -r requirements.txt
export MERAKI_DASHBOARD_API_KEY=yourkeyhere       # Windows PS: $env:MERAKI_DASHBOARD_API_KEY="..."
export MERAKI_ORG_ID=yourorgid                    # optional; otherwise pass --org each time
```

The API key always comes from `MERAKI_DASHBOARD_API_KEY`. The org ID may be set
via `MERAKI_ORG_ID` or passed with `--org <ID>` on any command (except
`export orgs`, which needs no org ID).

## Commands

### Discovery

```
python cli.py export orgs                          # list orgs + IDs (no org ID needed)
python cli.py export orgs --format csv             # also write orgs.csv
python cli.py export networks                      # all networks in the org, with IDs
python cli.py export networks --networks L_1 L_2    # only these networks
python cli.py export networks --format csv         # as CSV instead of JSON
python cli.py export groups                        # policy object groups: name, ID, member count
```

### Export (read-only)

```
python cli.py export firewall                      # L3 firewall + switch ACL rules -> CSV
python cli.py export firewall --networks L_1        # just one network
python cli.py export l7 --source L_1                # one network's L7 rules -> JSON
python cli.py export content-filter --source L_1    # one network's content filtering -> JSON
python cli.py export policy-check --from-file entries.txt   # do these FQDNs/IPs exist? (1 org or all)
```

`export firewall` resolves OBJ()/GRP() policy-object tokens to names and also
expands them to the underlying IPs/FQDNs.

`export policy-check` audits whether given FQDNs/IPs already exist as policy
objects — in one org (`--org`) or across all orgs (no `--org`). Read-only.

### Apply (writes — dry run by default)

```
# Preview (no changes):
python cli.py apply l7 --from l7_L_1.json --networks L_2 L_3
python cli.py apply content-filter --from contentfilter_L_1.json --networks L_2 L_3

# Actually write (requires --apply AND confirmation):
python cli.py apply l7 --from l7_L_1.json --networks L_2 L_3 --apply
python cli.py apply content-filter --from contentfilter_L_1.json --networks L_2 L_3 --apply
```

Apply modes (both `apply` commands):
- `--mode replace` (default) — the target ends up matching the source config.
- `--mode append` — the source entries are merged into the target's existing
  config (deduplicated). For `apply l7`, `--position top|bottom` controls where
  appended rules go.

> The Meraki update endpoints replace the whole rules/settings object (there is
> no server-side "add"), so `append` works by reading the target's current config
> and sending the combined set.

### Policy objects → group (write)

Create policy objects (FQDN/IP) and add them to one chosen group. Org and group
are selected interactively from numbered lists. Inputs come from `--fqdn`,
`--ip`, and/or `--from-file` (one entry per line; a `fqdn,` or `ip,` prefix is
optional — the type is inferred otherwise). Scales to large lists.

```
# preview (dry run):
python cli.py apply policy-group --from-file entries.txt
python cli.py apply policy-group --fqdn cit.immy.bot --ip 10.0.0.5

# create + add for real:
python cli.py apply policy-group --from-file entries.txt --apply

# give new objects an organized name (Spamhaus IPs 1, Spamhaus IPs 2, ...):
python cli.py apply policy-group --from-file spamhaus.txt --name "Spamhaus IPs" --apply
```

Behavior: existing objects (matched by value) are reused; an object whose
sanitized NAME already exists with a DIFFERENT value is warned-and-skipped;
objects are created first, then the group is updated ONCE at the end, so a
mid-run failure never half-updates the group. Updating a group affects every
firewall rule that references it, across all networks — the tool warns before
the group write. With `--name "BASE"`, newly-created objects are named `BASE 1`,
`BASE 2`, ... continuing from the highest existing `BASE N` (re-runs append
cleanly); value-based dedup still comes first, so existing objects are reused,
not renamed or duplicated.

### Policy objects → many groups, bulk (write)

For large lists (e.g. thousands of CIDRs) that exceed a single group's member
cap. Creates the objects and distributes them across a numbered series of groups
(`PREFIX 1`, `PREFIX 2`, ...). Non-interactive (`--org` required); prints a full
plan, then asks once to confirm.

```
# preview the plan (no writes):
python cli.py apply policy-bulk --org <ID> --from-file spamhaus.txt \
    --group-prefix "Spamhaus Group" --group-size 140 --name "Spamhaus IPs"

# execute:
python cli.py apply policy-bulk --org <ID> --from-file spamhaus.txt \
    --group-prefix "Spamhaus Group" --group-size 140 --name "Spamhaus IPs" --apply
```

Placement is stable and idempotent: an entry already present in any `PREFIX N`
group is left where it is; genuinely-new entries fill the first group with room,
creating the next numbered group as needed. So re-running after the list grows
only adds the new entries — existing placements never move, and a run that dies
partway can simply be re-run. `--group-size` is the per-group cap (default 140;
keep headroom under Meraki's limit, and lower it for lower-end MX models).

### L3 Rule Tools (org-to-org migration / staged rollout)

Heavier-weight than `apply l7`/`apply content-filter` — for moving an L3
ruleset between orgs, or rolling one rule out across a large org in batches.

```
# Source-side export: name-referenced (for reinflate) + flattened (for online migration)
python cli.py export l3-migration --network L_TEMPLATE

# Insert one rule at position 1 (dest refs are BY NAME, resolved per-org):
python cli.py apply l3-insert --rule-file deny_spamhaus.json --networks L_2 L_3 --apply

# Same insert, sliced across a large org with a batch backup written first:
python cli.py apply l3-insert-batch --rule-file deny_spamhaus.json --limit 250 --apply
python cli.py apply l3-insert-batch --rule-file deny_spamhaus.json --skip 250 --limit 250 --apply

# Roll a batch back from its backup file:
python cli.py apply l3-restore-batch --backup-file l3_batch_backup_ORG1_20260101_120000.json --apply

# Rebuild object/group refs against a target org's live IDs (after recreating
# the same-named objects/groups there), or push a self-contained ruleset:
python cli.py apply l3-reinflate --rule-file l3_migration_L1_named.json --apply
python cli.py apply l3-flattened --rule-file l3_migration_L1_flattened.json --apply
```

`apply l3-insert`/`l3-insert-batch`/`l3-reinflate`/`l3-flattened` all refuse to
write (no partial/broken rule) if a referenced object/group name doesn't exist
in the target org yet — create it there first. `l3-insert`/`l3-insert-batch`
dedupe by rule comment, so re-running never stacks duplicates.

## Typical workflow (e.g. regional settings)

```
python cli.py export orgs                                  # find your org ID
python cli.py export networks                              # find the network IDs
# ...configure ONE network in the dashboard the way you want it...
python cli.py export content-filter --source L_TEMPLATE     # copy that config to JSON
python cli.py apply content-filter --from contentfilter_L_TEMPLATE.json \
    --networks L_A L_B L_C                                  # DRY RUN: preview
python cli.py apply content-filter --from contentfilter_L_TEMPLATE.json \
    --networks L_A L_B L_C --apply                          # apply for real
```

## Layout

```
merakicore/            shared foundation
  client.py            single Meraki API client (MERAKI_DASHBOARD_API_KEY)
  networks.py          resolve/validate target networks from the live org
  resolve.py           OBJ/GRP policy-object token resolution
  contentfilter.py     content-filtering field normalization + merge
  policyobjects.py     policy-object/group helpers + entry parsing
  safety.py            write harness: dry-run, confirmation, per-network errors, progress/cancel
  paths.py             default output/ locations (exports, l3_migration, l3_batches)
  io.py                JSON / CSV save + JSON load
commands/              one module per operation (export_*, apply_*)
cli.py                 entry point; groups commands as `export` / `apply`
requirements.txt       depends on: meraki
```

## Notes

- Run `export orgs` and `export networks` against any org freely — they are
  read-only.
- Always run an `apply` as a dry run first and read the preview before adding
  `--apply`.
- `apply` only ever writes to the network IDs you pass (or all appliance networks
  if you pass none), so scoping with `--networks` limits the blast radius.
- L7 `application`/`applicationCategory` rules and content-filter categories must
  be in Meraki's exact format on write. Because this tool propagates Meraki's own
  GET output, that format is preserved automatically.

## Tests

There's an offline `pytest` suite for this engine's core logic at the repo
root's `tests/` folder (covers `merakicore/*`, the migration engine, and the
GUI widgets) — see the root `README.md`'s "Running the tests" section.
