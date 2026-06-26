"""
resolve.py — turn OBJ()/GRP() tokens in firewall rules into human-readable
names and underlying IPs/FQDNs.

Meraki firewall rules reference policy objects and groups by ID, e.g.
    srcCidr = "GRP(100000000000000001),OBJ(100000000000000002)"
This module resolves those IDs against the org's policy objects so exports are
readable. It lives in the shared core because both EXPORT (display) and future
APPLY/restore commands need the same resolution.

Two views are provided:
  - resolve_tokens_for_display(): GRP(<id>) -> GRP(<name>)         (readable)
  - expand_cidr_field():          GRP(<id>) -> underlying IPs/FQDNs (for audit)

Build the lookups once per org (they are org-wide), then pass them in per rule.
"""

import re

GRP_TOKEN = re.compile(r"GRP\((\w+)\)")
OBJ_TOKEN = re.compile(r"OBJ\((\w+)\)")


# --- lookups (build once per org) ------------------------------------------

def build_object_lookup(dashboard, org_id):
    """id(str) -> full policy-object dict (has name, type, cidr/fqdn/ip)."""
    try:
        objects = dashboard.organizations.getOrganizationPolicyObjects(org_id, total_pages="all")
        return {str(o["id"]): o for o in objects}
    except Exception:
        return {}


def build_group_lookup(dashboard, org_id):
    """id(str) -> full policy-object-group dict (has name, objectIds)."""
    try:
        groups = dashboard.organizations.getOrganizationPolicyObjectsGroups(org_id, total_pages="all")
        return {str(g["id"]): g for g in groups}
    except Exception:
        return {}


def build_network_group_policy_lookup(dashboard, network_id):
    """Network-level group policies: short id -> name (these have no IP objects)."""
    try:
        policies = dashboard.networks.getNetworkGroupPolicies(network_id)
        return {p["groupPolicyId"]: p["name"] for p in policies}
    except Exception:
        return {}


# --- single-object value ----------------------------------------------------

def object_value(obj):
    """The IP/CIDR or FQDN string for one policy object; falls back to name."""
    return obj.get("cidr") or obj.get("fqdn") or obj.get("ip") or obj.get("name", "?")


# --- display view: IDs -> names --------------------------------------------

def resolve_tokens_for_display(value, net_gp, org_grp, org_obj):
    """Replace GRP(<id>)/OBJ(<id>) with GRP(<name>)/OBJ(<name>)."""
    def grp_repl(m):
        gid = m.group(1)
        if gid in net_gp:
            return f"GRP({net_gp[gid]})"
        grp = org_grp.get(gid)
        return f"GRP({grp['name']})" if grp else f"GRP({gid})"

    def obj_repl(m):
        oid = m.group(1)
        obj = org_obj.get(oid)
        return f"OBJ({obj['name']})" if obj else f"OBJ({oid})"

    value = GRP_TOKEN.sub(grp_repl, value or "")
    value = OBJ_TOKEN.sub(obj_repl, value)
    return value


# --- audit view: IDs -> underlying IPs/FQDNs --------------------------------

def _resolve_obj_id(obj_id, org_obj):
    obj = org_obj.get(obj_id)
    return [object_value(obj)] if obj else [f"OBJ_ID:{obj_id}"]


def _resolve_grp_id(grp_id, net_gp, org_grp, org_obj):
    if grp_id in net_gp:
        return [f"NET_GP:{net_gp[grp_id]}"]    # network group policy: no IP objects
    grp = org_grp.get(grp_id)
    if not grp:
        return [f"GRP_ID:{grp_id}"]
    out = []
    for oid in grp.get("objectIds", []):
        out.extend(_resolve_obj_id(str(oid), org_obj))
    return out or [f"GRP:{grp['name']}(empty)"]


def expand_cidr_field(value, net_gp, org_grp, org_obj):
    """
    Expand a raw cidr field (which may contain OBJ/GRP tokens, plain CIDRs,
    VLAN refs, or 'Any') into a pipe-separated string of underlying values.
    """
    parts = [p.strip() for p in (value or "").split(",") if p.strip()]
    out = []
    for part in parts:
        gm = re.fullmatch(r"GRP\((\w+)\)", part)
        om = re.fullmatch(r"OBJ\((\w+)\)", part)
        if gm:
            out.extend(_resolve_grp_id(gm.group(1), net_gp, org_grp, org_obj))
        elif om:
            out.extend(_resolve_obj_id(om.group(1), org_obj))
        else:
            out.append(part)
    return " | ".join(out)
