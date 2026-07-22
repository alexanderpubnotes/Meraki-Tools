"""
export_l3_migration.py — export a network's L3 ruleset in a MIGRATION-READY,
name-referenced form (for the flatten/reinflate round-trip).

Read-only. For each rule, srcCidr/destCidr are decomposed into ordered tokens:
    {"grp": "<group name>"}   for GRP(id)  (id -> name, resolved now)
    {"obj": "<object name>"}  for OBJ(id)
    {"lit": "<value>"}        for literals, VLAN(...) refs, and "Any"
Order is preserved. The API's implicit "Default rule" is dropped.

This is the SOURCE-side export. It records NAMES (not the source org's IDs), so
the file is portable: after recreating the objects/groups in a target org (same
names), `apply l3-reinflate` resolves those names to the target org's live IDs.

It also emits the FLATTENED literal ruleset (for the online-migration step where
the appliance needs self-contained rules), so both forms come from one export.
"""

import re

from merakicore import policyobjects as po
from merakicore import io as io_mod
from merakicore import paths

GRP = re.compile(r"GRP\((\d+)\)")
OBJ = re.compile(r"OBJ\((\d+)\)")


def _obj_value(o):
    return o.get("fqdn") if o.get("type") == "fqdn" else o.get("cidr")


def _tokenize(field_value, id_to_gname, id_to_oname):
    """Return ordered name-token list for a src/dest string."""
    if not field_value or field_value == "Any":
        return [{"lit": "Any"}]
    tokens = []
    for raw in field_value.split(","):
        raw = raw.strip()
        mg = GRP.fullmatch(raw)
        mo = OBJ.fullmatch(raw)
        if mg:
            gid = mg.group(1)
            name = id_to_gname.get(gid)
            tokens.append({"grp": name} if name else {"lit": raw})
        elif mo:
            oid = mo.group(1)
            name = id_to_oname.get(oid)
            tokens.append({"obj": name} if name else {"lit": raw})
        else:
            tokens.append({"lit": raw})   # literal / VLAN(...) / CIDR / FQDN
    return tokens


def _flatten(field_value, id_to_gmembers, id_to_oval):
    """Return literal comma-joined string (groups/objects expanded, VLAN kept)."""
    if not field_value or field_value == "Any":
        return field_value
    out = []
    for raw in field_value.split(","):
        raw = raw.strip()
        mg = GRP.fullmatch(raw)
        mo = OBJ.fullmatch(raw)
        if mg:
            for m in id_to_gmembers.get(mg.group(1), []):
                v = id_to_oval.get(m)
                if v:
                    out.append(v)
        elif mo:
            v = id_to_oval.get(mo.group(1))
            if v:
                out.append(v)
        else:
            out.append(raw)
    seen, ded = set(), []
    for t in out:
        if t and t not in seen:
            seen.add(t); ded.append(t)
    return ",".join(ded)


def run(dashboard, org_id, network_id, output_prefix=None):
    groups = po.fetch_groups(dashboard, org_id)
    objects = po.fetch_objects(dashboard, org_id)
    id_to_gname = {str(g["id"]): g["name"] for g in groups}
    id_to_oname = {str(o["id"]): o["name"] for o in objects}
    id_to_gmembers = {str(g["id"]): [str(x) for x in (g.get("objectIds") or [])] for g in groups}
    id_to_oval = {str(o["id"]): _obj_value(o) for o in objects}

    rules = dashboard.appliance.getNetworkApplianceFirewallL3FirewallRules(network_id)["rules"]

    name_rules, flat_rules = [], []
    for r in rules:
        if r.get("comment") == "Default rule":
            continue
        base = {k: r.get(k) for k in ("comment", "policy", "protocol",
                                      "srcPort", "destPort", "syslogEnabled")}
        nr = dict(base)
        nr["src"] = _tokenize(r.get("srcCidr", "Any"), id_to_gname, id_to_oname)
        nr["dest"] = _tokenize(r.get("destCidr", "Any"), id_to_gname, id_to_oname)
        name_rules.append(nr)

        fr = dict(base)
        fr["srcCidr"] = _flatten(r.get("srcCidr", "Any"), id_to_gmembers, id_to_oval)
        fr["destCidr"] = _flatten(r.get("destCidr", "Any"), id_to_gmembers, id_to_oval)
        flat_rules.append(fr)

    prefix = output_prefix or paths.default_path("l3_migration", f"l3_migration_{network_id}")
    name_path = f"{prefix}_named.json"
    flat_path = f"{prefix}_flattened.json"
    io_mod.save_json(name_path, {"rules": name_rules})
    io_mod.save_json(flat_path, {"rules": flat_rules})

    print(f"  exported {len(name_rules)} rule(s) from {network_id}")
    print(f"  name-referenced (for reinflate) -> {name_path}")
    print(f"  flattened literals (for online migration) -> {flat_path}")
    return name_path, flat_path
