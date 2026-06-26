"""
apply policy-group (module: apply_policy_group.py)

Create policy objects (FQDN/IP) as needed and add them to ONE chosen policy
object group. Designed to scale to many entries (e.g. 1000):

  - Existing objects (matched by VALUE) are reused, not recreated.
  - Name collision (an object with the same sanitized NAME exists but holds a
    DIFFERENT value) -> warn and SKIP that entry (never reuse the wrong object).
  - Objects are created first (with progress), IDs collected; the group is
    updated ONCE at the end with everything that succeeded. A mid-run failure
    therefore never leaves the group half-updated, and failed entries are listed
    so you can re-run just those (re-running is safe — existing objects skip).
  - Dry run is the default; apply=True writes.

Org and group are chosen interactively from numbered lists (IDs shown). Group
membership is the unit of change — note this affects EVERY firewall rule that
references the group, across all networks.
"""

import sys

from merakicore import policyobjects as po
from merakicore import safety


def _choose(items, label, render):
    print(f"\nAvailable {label}:")
    for i, it in enumerate(items, 1):
        print(f"  {i:>3}. {render(it)}")
    while True:
        raw = input(f"\nSelect {label} number: ").strip()
        if raw.isdigit() and 1 <= int(raw) <= len(items):
            return items[int(raw) - 1]
        print(f"  Enter a number between 1 and {len(items)}.")


def run(dashboard, entries, apply=False, name_base=None, assume_yes=False):
    if not entries:
        sys.exit("Nothing to add. Supply --fqdn / --ip / --from-file.")

    dry_run = not apply

    # 1. Choose org
    orgs = dashboard.organizations.getOrganizations()
    if not orgs:
        sys.exit("No organizations visible to this API key.")
    orgs = sorted(orgs, key=lambda o: o["name"].lower())
    org = _choose(orgs, "organization", lambda o: f"{o['name']}  (ID: {o['id']})")
    org_id = org["id"]
    print(f"  selected org: {org['name']} ({org_id})")

    # 2. Choose group
    groups = po.fetch_groups(dashboard, org_id)
    if not groups:
        sys.exit("No policy object groups in this organization.")
    groups = sorted(groups, key=lambda g: g["name"].lower())
    group = _choose(groups, "policy object group",
                    lambda g: f"{g['name']}  ({len(g.get('objectIds') or [])} object(s))  (ID: {g['id']})")
    print(f"  selected group: {group['name']} ({group['id']})")

    # 3. Index existing objects
    objects = po.fetch_objects(dashboard, org_id)
    by_fqdn, by_cidr, by_name = po.build_indexes(objects)
    current_ids = list(group.get("objectIds") or [])
    current_set = set(current_ids)

    print(f"\n  {len(entries)} entr(y/ies) to process into '{group['name']}'")
    if name_base:
        next_idx = po.next_name_index(objects, name_base)
        print(f"  naming new objects: '{po.sanitize_name(name_base)} {next_idx}', "
              f"'{po.sanitize_name(name_base)} {next_idx + 1}', ...")
    else:
        next_idx = None
    if dry_run:
        print("  DRY RUN — no objects created, group not modified.\n")

    to_add_ids = []          # object IDs to add to the group
    created = skipped = collided = already = 0
    failures = []            # (entry, reason)

    total = len(entries)
    for i, (val, typ) in enumerate(entries, 1):
        prefix = f"  [{i}/{total}] {val}"
        existing = po.find_existing(val, typ, by_fqdn, by_cidr)

        if existing:
            oid = existing["id"]
            if oid in current_set:
                print(f"{prefix} — already in group, skip")
                already += 1
            else:
                print(f"{prefix} — object exists, will add to group")
                to_add_ids.append(oid); current_set.add(oid)
            continue

        # No value match. Determine the name this new object would get.
        assigned_name = (f"{name_base} {next_idx}" if name_base else val)
        name_key = po.sanitize_name(assigned_name).lower()
        if name_key in by_name:
            print(f"{prefix} — WARN: name '{po.sanitize_name(assigned_name)}' exists with a "
                  f"different value; skipping to stay safe")
            collided += 1
            failures.append((val, "name collision"))
            continue

        # Needs creating.
        if dry_run:
            label = f" as '{po.sanitize_name(assigned_name)}'" if name_base else ""
            print(f"{prefix} — would create ({typ}){label} and add to group")
            created += 1
            if name_base:
                next_idx += 1
            continue

        try:
            payload = po.object_payload(val, typ, name=(assigned_name if name_base else None))
            obj = dashboard.organizations.createOrganizationPolicyObject(org_id, **payload)
            oid = obj["id"]
            to_add_ids.append(oid); current_set.add(oid)
            by_name[name_key] = obj      # keep index current to catch dup inputs
            created += 1
            namehint = f" as '{po.sanitize_name(assigned_name)}'" if name_base else ""
            print(f"{prefix} — created{namehint} (ID: {oid}), will add to group")
            if name_base:
                next_idx += 1
        except Exception as e:
            print(f"{prefix} — FAILED to create: {e}")
            failures.append((val, str(e)))

    # 4. Single group update at the end
    print("\n" + "=" * 60)
    print(f"Prepared: create/added {created}, already-in-group {already}, "
          f"name-collisions {collided}, failures {len(failures)}")
    new_total = len(current_ids) + len(to_add_ids)

    if dry_run:
        print(f"DRY RUN — group '{group['name']}' would go to {new_total} object(s). "
              f"Re-run with --apply to write.")
    elif not to_add_ids:
        print("Nothing new to add to the group.")
    else:
        print(f"\nAbout to update group '{group['name']}' ({group['id']}) in org "
              f"'{org['name']}'.")
        print("NOTE: this changes EVERY firewall rule that references this group, "
              "across all networks.")
        if not assume_yes and not safety.confirm(f"Add {len(to_add_ids)} object(s) to the group? [y/N]: "):
            sys.exit("Aborted — no objects added to the group (created objects remain).")
        try:
            dashboard.organizations.updateOrganizationPolicyObjectsGroup(
                org_id, group["id"], objectIds=current_ids + to_add_ids
            )
            print(f"  group updated -> {new_total} object(s) total")
        except Exception as e:
            sys.exit(f"  FAILED to update group: {e}\n  "
                     f"(objects were created; re-run to retry the group update.)")

    if failures:
        print("\nEntries that did not get added:")
        for val, reason in failures:
            print(f"  - {val}: {reason}")
        print("Re-running is safe — existing objects are detected and skipped.")
