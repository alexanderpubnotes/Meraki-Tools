"""
networks.py — resolve which networks a command should act on.

Single source of truth for "which networks am I targeting?" Every command uses
this so targeting behaves identically everywhere.

Key behaviors:
  - Fetches the org's networks LIVE on every call. Newly added locations show
    up automatically; there is no snapshot file to regenerate.
  - Targets by network ID (unambiguous). Names can repeat or be renamed, so they
    are never used to select — only shown back to the user for confirmation.
  - Unknown IDs are warned about and skipped, not silently dropped or fatal.
  - Returned dicts are the full network objects from the API (they include
    'id', 'name', 'productTypes', 'tags', etc.).
"""


class NoNetworksResolved(Exception):
    """Raised when targeting produced an empty set (e.g. every ID was invalid)."""


def fetch_all_networks(dashboard, org_id):
    """Return every network in the org, fetched live (handles pagination)."""
    return dashboard.organizations.getOrganizationNetworks(org_id, total_pages="all")


def resolve_targets(dashboard, org_id, network_ids=None, product_type=None):
    """
    Resolve the list of networks to act on.

    Args:
        dashboard:     a Meraki client from merakicore.client.get_dashboard()
        org_id:        the organization ID to look in
        network_ids:   None  -> all networks in the org
                       list  -> only these IDs, validated against the live org
        product_type:  optional filter, e.g. "appliance" or "switch". When set,
                       only networks that include that product type are kept.
                       (Useful because, e.g., content filtering only applies to
                       appliance/MX networks.)

    Returns:
        list[dict]: matched network objects (full dicts from the API).

    Raises:
        NoNetworksResolved: if the result is empty.
    """
    all_networks = fetch_all_networks(dashboard, org_id)
    by_id = {n["id"]: n for n in all_networks}

    if network_ids is None:
        selected = list(all_networks)
    else:
        selected = []
        seen = set()
        for nid in network_ids:
            if nid in seen:
                continue                      # ignore duplicate IDs in the input
            seen.add(nid)
            net = by_id.get(nid)
            if net is None:
                print(f"  warning: network ID '{nid}' not found in org — skipping")
                continue
            selected.append(net)

    if product_type is not None:
        before = len(selected)
        selected = [n for n in selected if product_type in n.get("productTypes", [])]
        dropped = before - len(selected)
        if dropped:
            print(f"  note: skipped {dropped} network(s) without '{product_type}' capability")

    if not selected:
        raise NoNetworksResolved(
            "No networks to act on. Check the IDs you passed (and any product-type "
            "filter), then try again."
        )

    return selected


def confirmation_readout(networks, max_shown=10):
    """
    Build a short human-readable summary of the resolved targets, for printing
    before a write operation. Names are display-only.

        targeting 4 network(s):
          - Dallas (L_123)
          - Frankfurt (L_456)
          ...
    """
    lines = [f"targeting {len(networks)} network(s):"]
    for net in networks[:max_shown]:
        lines.append(f"  - {net.get('name', '(unnamed)')} ({net['id']})")
    if len(networks) > max_shown:
        lines.append(f"  ... and {len(networks) - max_shown} more")
    return "\n".join(lines)
