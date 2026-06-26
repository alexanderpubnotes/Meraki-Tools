"""
policyobjects.py — shared helpers for org-level policy objects and groups.

Policy objects are org-level building blocks (an FQDN or a CIDR) that firewall
rules reference, individually OBJ(id) or via groups GRP(id). This module holds
the logic shared by:
    export policy-check   (read-only: do these objects exist?)
    apply  policy-group   (create objects + add them to a group)

Matching is by VALUE (fqdn / cidr), which is exact. Name matching is NOT used to
decide membership, because sanitized names can collide. Naming collisions are
handled by the caller: warn and skip (never reuse a same-named object that holds
a different value).
"""

# Meraki object names allow alphanumerics, spaces, dashes, underscores only.
def sanitize_name(value):
    """Make a value safe to use as an object name (dots -> underscores, etc.)."""
    out = []
    for ch in value:
        out.append(ch if (ch.isalnum() or ch in " -_") else "_")
    return "".join(out)


def looks_like_ip(value):
    """Heuristic: does this entry look like an IP / CIDR rather than an FQDN?"""
    v = value.split("/")[0]
    parts = v.split(".")
    if len(parts) == 4 and all(p.isdigit() and 0 <= int(p) <= 255 for p in parts):
        return True
    if ":" in value:           # crude IPv6 check
        return True
    return False


def to_cidr(ip):
    """Normalize a bare IP to /32; leave existing CIDRs alone."""
    return ip if "/" in ip else f"{ip}/32"


def fetch_objects(dashboard, org_id):
    return dashboard.organizations.getOrganizationPolicyObjects(org_id, total_pages="all")


def fetch_groups(dashboard, org_id):
    return dashboard.organizations.getOrganizationPolicyObjectsGroups(org_id, total_pages="all")


def build_indexes(objects):
    """
    Build value-based lookups from existing policy objects.
      by_fqdn : lowercased fqdn -> object
      by_cidr : cidr string     -> object
      by_name : sanitized lower name -> object   (used ONLY to detect collisions)
    """
    by_fqdn, by_cidr, by_name = {}, {}, {}
    for obj in objects:
        if obj.get("type") == "fqdn" and obj.get("fqdn"):
            by_fqdn[obj["fqdn"].lower()] = obj
        if obj.get("type") == "cidr" and obj.get("cidr"):
            by_cidr[obj["cidr"]] = obj
        if obj.get("name"):
            by_name[sanitize_name(obj["name"]).lower()] = obj
    return by_fqdn, by_cidr, by_name


def find_existing(entry, entry_type, by_fqdn, by_cidr):
    """Return the existing object that exactly matches this VALUE, or None."""
    if entry_type == "fqdn":
        return by_fqdn.get(entry.lower())
    return by_cidr.get(to_cidr(entry))


def object_payload(entry, entry_type, name=None):
    """
    Args for createOrganizationPolicyObject for this entry.
    If `name` is given, it is used (sanitized) as the object name; otherwise the
    name is derived from the value (the default value-based naming).
    """
    obj_name = sanitize_name(name) if name else sanitize_name(entry)
    if entry_type == "fqdn":
        return dict(name=obj_name, category="network", type="fqdn", fqdn=entry)
    return dict(name=obj_name, category="network", type="cidr", cidr=to_cidr(entry))


def next_name_index(objects, base_name):
    """
    Given existing objects and a base name like 'Spamhaus IPs', find the highest
    existing 'Spamhaus IPs <N>' suffix and return the next free integer.

    So if 'Spamhaus IPs 1'..'Spamhaus IPs 10' exist, returns 11. If none exist,
    returns 1. This lets re-runs append cleanly instead of colliding.
    """
    import re
    safe_base = sanitize_name(base_name)
    pattern = re.compile(rf"^{re.escape(safe_base)}\s+(\d+)$", re.IGNORECASE)
    highest = 0
    for obj in objects:
        nm = obj.get("name", "")
        m = pattern.match(nm)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


# --- input parsing ----------------------------------------------------------

def parse_entries(fqdns=None, ips=None, from_file=None):
    """
    Build a de-duplicated list of (value, type) from flags and/or a file.

    File lines: blank lines and '#' comments ignored. A line may be:
      - plain value (type inferred): e.g.  cit.immy.bot   or   10.0.0.0/16
      - explicit:   fqdn,cit.immy.bot   or   ip,10.0.0.0/16
    """
    entries = []
    for f in (fqdns or []):
        entries.append((f, "fqdn"))
    for ip in (ips or []):
        entries.append((ip, "ip"))

    if from_file:
        with open(from_file) as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                if "," in line:
                    prefix, _, val = line.partition(",")
                    prefix, val = prefix.strip().lower(), val.strip()
                    if prefix in ("fqdn", "ip") and val:
                        entries.append((val, prefix))
                        continue
                    line = val or line          # not a real prefix; treat whole as value
                entries.append((line, "ip" if looks_like_ip(line) else "fqdn"))

    # de-dup, preserving order; normalize ip type via value
    seen, out = set(), []
    for val, typ in entries:
        key = (val.lower(), typ)
        if key in seen:
            continue
        seen.add(key)
        out.append((val, typ))
    return out
