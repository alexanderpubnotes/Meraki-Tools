"""
policy_objects.py — backup and restore of organization Policy Objects and
Policy Object Groups.

Why this is its own module (and not just a row in a generic settings table):
policy object IDs are scoped to an organization. When we recreate objects in
the destination org they get brand-new IDs, and groups reference objects BY
ID. So a restore must:

    1. Create/update every object in the destination org (matching by NAME).
    2. Build an id_map: {old object id -> new object id}.
    3. Create/update every group, rewriting its objectIds through the id_map.
    4. Save the id_map to disk — later phases (L3 firewall rules etc.) will
       use it to rewrite OBJ(...)/GRP(...) references inside rule payloads.

Matching by name means the restore is idempotent: run it twice and the second
run just updates objects to the backed-up values instead of creating dupes.
"""

from common import save_json, load_json, announce

# Fields returned by GET that must not be sent back on create/update.
OBJECT_READONLY_FIELDS = {"id", "groupIds", "networkIds", "createdAt", "updatedAt"}
GROUP_READONLY_FIELDS = {"id", "objectIds", "networkIds", "createdAt", "updatedAt"}


# ---------------------------------------------------------------- backup ---

def backup(dashboard, org_id, backup_dir):
    """Save all policy objects and groups of org_id to JSON files."""
    announce(f"Backing up policy objects from org {org_id}")

    objects = dashboard.organizations.getOrganizationPolicyObjects(
        organizationId=org_id, total_pages="all"
    )
    groups = dashboard.organizations.getOrganizationPolicyObjectsGroups(
        organizationId=org_id, total_pages="all"
    )

    save_json(f"{backup_dir}/organization/policy_objects.json", objects)
    save_json(f"{backup_dir}/organization/policy_object_groups.json", groups)
    print(f"  {len(objects)} objects, {len(groups)} groups backed up.")


# --------------------------------------------------------------- restore ---

def restore(dashboard, target_org_id, backup_dir, dry_run=True,
            progress_cb=None, cancel_event=None):
    """
    Recreate backed-up policy objects and groups inside target_org_id.
    Returns the id_map and also saves it to the backup directory.
    With dry_run=True, only GETs are performed: the plan (create/update/skip
    per item) is printed but nothing is written to the Dashboard or to disk.
    """
    mode = "DRY RUN — no changes will be made" if dry_run else "applying changes"
    announce(f"Restoring policy objects into org {target_org_id} ({mode})")

    objects = load_json(f"{backup_dir}/organization/policy_objects.json")
    groups = load_json(f"{backup_dir}/organization/policy_object_groups.json")

    id_map = {"objects": {}, "groups": {}}
    failures = []

    # Value of each backed-up object (cidr/fqdn/ip), used to de-duplicate
    # group members: Meraki rejects groups containing two objects with the
    # same value, even though some older orgs contain exactly that.
    value_by_old_id = {
        str(o["id"]): (o.get("cidr") or o.get("fqdn") or o.get("ip"))
        for o in objects
    }

    # ---- 1. Objects -------------------------------------------------------
    existing = dashboard.organizations.getOrganizationPolicyObjects(
        organizationId=target_org_id, total_pages="all"
    )
    existing_by_name = {o["name"]: o for o in existing}

    objects_cancelled = False
    total_objects = len(objects)
    for oi, obj in enumerate(objects, 1):
        if cancel_event is not None and cancel_event.is_set():
            print(f"\n  Cancelled — stopped before object {oi}/{total_objects}.")
            objects_cancelled = True
            break
        if obj.get("category") == "adaptivePolicy":
            print(f"  SKIP    object '{obj['name']}' (adaptive policy not supported)")
            if progress_cb:
                progress_cb(oi, total_objects)
            continue

        payload = {k: v for k, v in obj.items() if k not in OBJECT_READONLY_FIELDS}

        try:
            if obj["name"] in existing_by_name:
                new_id = existing_by_name[obj["name"]]["id"]
                if dry_run:
                    print(f"  would update object '{obj['name']}'")
                else:
                    dashboard.organizations.updateOrganizationPolicyObject(
                        organizationId=target_org_id, policyObjectId=new_id, **payload
                    )
                    print(f"  updated object '{obj['name']}'")
            else:
                if dry_run:
                    print(f"  would create object '{obj['name']}'")
                    new_id = f"(new id for {obj['name']})"
                else:
                    created = dashboard.organizations.createOrganizationPolicyObject(
                        organizationId=target_org_id, **payload
                    )
                    new_id = created["id"]
                    print(f"  created object '{obj['name']}'")
        except Exception as e:
            print(f"  ERROR   object '{obj['name']}': {e}")
            failures.append({"kind": "object", "name": obj["name"], "error": str(e)})
            if progress_cb:
                progress_cb(oi, total_objects)
            continue

        id_map["objects"][str(obj["id"])] = str(new_id)
        if progress_cb:
            progress_cb(oi, total_objects)

    # ---- 2. Groups (objectIds rewritten through the id_map) ---------------
    existing_groups = dashboard.organizations.getOrganizationPolicyObjectsGroups(
        organizationId=target_org_id, total_pages="all"
    )
    existing_groups_by_name = {g["name"]: g for g in existing_groups}

    groups_cancelled = False
    total_groups = len(groups)
    for gi, grp in enumerate(groups, 1):
        if cancel_event is not None and cancel_event.is_set():
            print(f"\n  Cancelled — stopped before group {gi}/{total_groups}.")
            groups_cancelled = True
            break
        payload = {k: v for k, v in grp.items() if k not in GROUP_READONLY_FIELDS}

        # Rewrite member object IDs from old org to new org, de-duplicating:
        #  - the same object listed twice
        #  - two different objects carrying the same value (Meraki rejects
        #    these with "duplicate values", even if the source org has them)
        new_object_ids, missing = [], []
        seen_ids, seen_values = set(), {}
        for old_id in grp.get("objectIds") or []:
            old_id = str(old_id)
            new_id = id_map["objects"].get(old_id)
            if not new_id:
                missing.append(old_id)
                continue
            if new_id in seen_ids:
                continue
            value = value_by_old_id.get(old_id)
            if value in seen_values:
                print(f"  WARN    group '{grp['name']}': dropping duplicate-value member "
                      f"(value {value} already covered by '{seen_values[value]}')")
                continue
            seen_ids.add(new_id)
            seen_values[value] = next(
                (o["name"] for o in objects if str(o["id"]) == old_id), old_id
            )
            new_object_ids.append(new_id)
        if missing:
            print(f"  WARN    group '{grp['name']}': {len(missing)} member object(s) "
                  f"not in backup or failed to restore, left out: {missing}")
        payload["objectIds"] = new_object_ids

        try:
            if grp["name"] in existing_groups_by_name:
                new_gid = existing_groups_by_name[grp["name"]]["id"]
                if dry_run:
                    print(f"  would update group '{grp['name']}' ({len(new_object_ids)} members)")
                else:
                    dashboard.organizations.updateOrganizationPolicyObjectsGroup(
                        organizationId=target_org_id, policyObjectGroupId=new_gid, **payload
                    )
                    print(f"  updated group '{grp['name']}' ({len(new_object_ids)} members)")
            else:
                if dry_run:
                    print(f"  would create group '{grp['name']}' ({len(new_object_ids)} members)")
                    new_gid = f"(new id for {grp['name']})"
                else:
                    created = dashboard.organizations.createOrganizationPolicyObjectsGroup(
                        organizationId=target_org_id, **payload
                    )
                    new_gid = created["id"]
                    print(f"  created group '{grp['name']}' ({len(new_object_ids)} members)")
        except Exception as e:
            print(f"  ERROR   group '{grp['name']}': {e}")
            failures.append({"kind": "group", "name": grp["name"], "error": str(e)})
            if progress_cb:
                progress_cb(gi, total_groups)
            continue

        id_map["groups"][str(grp["id"])] = str(new_gid)
        if progress_cb:
            progress_cb(gi, total_groups)

    cancelled = objects_cancelled or groups_cancelled

    # ---- Failure summary ---------------------------------------------------
    if failures:
        print(f"\n  {len(failures)} item(s) FAILED — every other item was still processed:")
        for f in failures:
            print(f"    - {f['kind']} '{f['name']}'")
        if not dry_run:
            save_json(f"{backup_dir}/failures_org_{target_org_id}.json", failures)
            print("  Details saved. Fix the cause and re-run the restore; "
                  "successful items will simply be updated in place.")
    elif cancelled:
        print("\n  Cancelled early — safe to re-run; already-restored items are idempotent.")

    # ---- 3. Save the id_map for later phases ------------------------------
    if dry_run:
        print("\n  Dry run complete — nothing was changed. Re-run with --apply to apply."
              + ("  (CANCELLED early)" if cancelled else ""))
        return id_map
    map_path = f"{backup_dir}/idmap_org_{target_org_id}.json"
    save_json(map_path, id_map)
    print(f"  ID map saved — later firewall-rule restores will use it to rewrite "
          f"OBJ()/GRP() references.")
    return id_map
