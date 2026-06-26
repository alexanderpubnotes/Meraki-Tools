"""
apply policy-bulk (module: apply_policy_bulk.py)

Create many policy objects from one file and distribute them across a series of
numbered groups (e.g. 'Spamhaus Group 1', 'Spamhaus Group 2', ...) because a
single group has a member cap.

PLACEMENT MODEL (Option B — stable / idempotent):
  - For each entry: if its VALUE is already in ANY of the prefix-named groups,
    it is left exactly where it is (skipped). File order does not matter and
    re-running never moves an already-placed entry.
  - Genuinely-new entries fill the first prefix group that has room (up to
    --group-size), creating the next numbered group when the current ones fill.
  - Objects are deduped by value (reused if they already exist), and newly
    created objects are named with the --name counter (continues from highest).

This makes the whole operation safe to re-run: run it, and if it dies partway,
just run it again — placed entries skip, new ones flow into remaining space.

Non-interactive: org is given via --org. A full plan is shown, then a single
confirmation, then execution (on --apply).
"""

import re
import sys

from merakicore import policyobjects as po
from merakicore import safety


def _prefix_groups(groups, prefix):
    """Return existing 'PREFIX N' groups, sorted by N ascending."""
    safe = po.sanitize_name(prefix)
    pat = re.compile(rf"^{re.escape(safe)}\s+(\d+)$", re.IGNORECASE)
    out = []
    for g in groups:
        m = pat.match(g.get("name", ""))
        if m:
            out.append((int(m.group(1)), g))
    out.sort(key=lambda t: t[0])
    return out


def run(dashboard, entries, org_id, group_prefix, group_size=140,
        name_base=None, apply=False, assume_yes=False):
    if not entries:
        sys.exit("Nothing to add. Supply --fqdn / --ip / --from-file.")
    if not org_id:
        sys.exit("--org is required for policy-bulk.")
    if not group_prefix:
        sys.exit("--group-prefix is required (e.g. --group-prefix 'Spamhaus Group').")
    if group_size < 1:
        sys.exit("--group-size must be >= 1.")

    dry_run = not apply

    # Verify org exists / is visible.
    orgs = {o["id"]: o for o in dashboard.organizations.getOrganizations()}
    if org_id not in orgs:
        sys.exit(f"Org {org_id} not visible to this API key.")
    org_name = orgs[org_id]["name"]
    print(f"  org: {org_name} ({org_id})")

    # Live state: all objects, all groups.
    objects = po.fetch_objects(dashboard, org_id)
    by_fqdn, by_cidr, by_name = po.build_indexes(objects)
    obj_by_id = {o["id"]: o for o in objects}
    all_groups = po.fetch_groups(dashboard, org_id)

    # The prefix-named series, and the set of object IDs already in ANY of them.
    series = _prefix_groups(all_groups, group_prefix)
    placed_ids = set()
    for _, g in series:
        placed_ids.update(g.get("objectIds") or [])

    # Working capacity model: for each existing series group, how many free slots.
    # We model groups as mutable member-lists we will fill.
    work = []  # list of dicts: {num, id(optional), name, members(list of ids), existing(bool)}
    for num, g in series:
        work.append({"num": num, "id": g["id"], "name": g["name"],
                     "members": list(g.get("objectIds") or []), "existing": True})
    next_group_num = (series[-1][0] + 1) if series else 1

    def first_group_with_room():
        for w in work:
            if len(w["members"]) < group_size:
                return w
        # none with room -> create a new (planned) group
        nonlocal next_group_num
        neww = {"num": next_group_num, "id": None,
                "name": f"{po.sanitize_name(group_prefix)} {next_group_num}",
                "members": [], "existing": False}
        work.append(neww)
        next_group_num += 1
        return neww

    # Object-naming counter for new objects.
    next_idx = po.next_name_index(objects, name_base) if name_base else None

    # ---- PLAN (no writes) ----
    print(f"\n  planning {len(entries)} entr(y/ies), group size {group_size}, "
          f"prefix '{po.sanitize_name(group_prefix)}'...")

    plan_create = []     # (val, typ, assigned_name)
    plan_place = []      # (val, target_group_work, reused_existing_obj_id_or_None)
    skip_already = 0
    collided = 0
    failures = []

    # We need to simulate placement to build the plan. Track which planned-new
    # objects go where (by a placeholder id) so counts are right.
    placeholder = 0
    for val, typ in entries:
        existing = po.find_existing(val, typ, by_fqdn, by_cidr)
        if existing and existing["id"] in placed_ids:
            skip_already += 1
            continue

        # name-collision guard for new creates
        if not existing:
            assigned_name = f"{name_base} {next_idx}" if name_base else val
            nkey = po.sanitize_name(assigned_name).lower()
            if nkey in by_name:
                collided += 1
                failures.append((val, "name collision"))
                continue
        else:
            assigned_name = None

        target = first_group_with_room()
        if existing:
            target["members"].append(existing["id"])
            placed_ids.add(existing["id"])
            plan_place.append((val, target, existing["id"]))
        else:
            placeholder += 1
            ph = f"__NEW_{placeholder}__"
            target["members"].append(ph)
            plan_create.append((val, typ, assigned_name, ph, target))
            by_name[po.sanitize_name(assigned_name).lower()] = {"name": assigned_name}
            if name_base:
                next_idx += 1

    groups_existing = sum(1 for w in work if w["existing"])
    groups_new = sum(1 for w in work if not w["existing"])

    print("\n" + "=" * 64)
    print("PLAN")
    print(f"  entries given          : {len(entries)}")
    print(f"  already placed (skip)  : {skip_already}")
    print(f"  name collisions (skip) : {collided}")
    print(f"  objects to reuse       : {len(plan_place)}")
    print(f"  objects to create      : {len(plan_create)}")
    print(f"  groups (existing/new)  : {groups_existing} existing, {groups_new} new")
    for w in work:
        tag = "existing" if w["existing"] else "NEW"
        print(f"    [{tag}] {w['name']}: {len(w['members'])}/{group_size} members")
    print("=" * 64)

    if dry_run:
        print("\nDRY RUN — nothing created or modified. Re-run with --apply to write.")
        if failures:
            print("\nWould skip (name collision):")
            for v, r in failures:
                print(f"  - {v}: {r}")
        return

    if not plan_create and not plan_place:
        print("\nNothing new to do — everything already placed.")
        return

    print(f"\nThis will CREATE {len(plan_create)} object(s) and update/create "
          f"{len(work)} group(s) in org '{org_name}'.")
    print("NOTE: updating these groups affects every firewall rule that "
          "references them, across all networks.")
    if not assume_yes and not safety.confirm("Proceed? [y/N]: "):
        sys.exit("Aborted — no changes made.")

    # ---- EXECUTE ----
    # 1. Create new objects, mapping placeholder -> real id.
    ph_to_id = {}
    total_new = len(plan_create)
    for i, (val, typ, assigned_name, ph, target) in enumerate(plan_create, 1):
        try:
            payload = po.object_payload(val, typ, name=(assigned_name if name_base else None))
            obj = dashboard.organizations.createOrganizationPolicyObject(org_id, **payload)
            ph_to_id[ph] = obj["id"]
            namehint = f" as '{po.sanitize_name(assigned_name)}'" if name_base else ""
            print(f"  [create {i}/{total_new}] {val}{namehint} -> ID {obj['id']}")
        except Exception as e:
            ph_to_id[ph] = None
            failures.append((val, str(e)))
            print(f"  [create {i}/{total_new}] {val} -> FAILED: {e}")

    # 2. Resolve each group's final member list (placeholders -> real ids, drop failures).
    for w in work:
        resolved = []
        for m in w["members"]:
            if isinstance(m, str) and m.startswith("__NEW_"):
                rid = ph_to_id.get(m)
                if rid:
                    resolved.append(rid)
            else:
                resolved.append(m)
        w["members"] = resolved

    # 3. Create/update groups (one call each).
    for w in work:
        try:
            if w["existing"]:
                dashboard.organizations.updateOrganizationPolicyObjectsGroup(
                    org_id, w["id"], objectIds=w["members"])
                print(f"  [group] updated {w['name']} -> {len(w['members'])} member(s)")
            else:
                dashboard.organizations.createOrganizationPolicyObjectsGroup(
                    org_id, name=w["name"], category="NetworkObjectGroup",
                    objectIds=w["members"])
                print(f"  [group] created {w['name']} -> {len(w['members'])} member(s)")
        except Exception as e:
            print(f"  [group] FAILED {w['name']}: {e}")
            failures.append((w["name"], f"group write: {e}"))

    print("\nDone.")
    if failures:
        print("\nEntries/groups that failed (safe to re-run — placed entries skip):")
        for v, r in failures:
            print(f"  - {v}: {r}")
