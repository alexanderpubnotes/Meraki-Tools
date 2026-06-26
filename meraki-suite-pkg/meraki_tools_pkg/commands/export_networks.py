"""
export_networks.py — export the org's networks (optionally a chosen subset).

Read-only: only GETs the network list and writes a local file. This is the
consolidation of the old getNetworkIDs / getNetworkNameAndAddress scripts —
one command, with a format choice, operating on the live org.
"""

from merakicore import networks as net_mod
from merakicore import io as io_mod

# Columns for the CSV view. Tags/productTypes are lists, so they're joined.
CSV_FIELDS = ["id", "name", "productTypes", "tags", "timeZone", "url"]


def _row(network):
    """Flatten one network dict into CSV-friendly scalar values."""
    return {
        "id": network.get("id", ""),
        "name": network.get("name", ""),
        "productTypes": ", ".join(network.get("productTypes", [])),
        "tags": ", ".join(network.get("tags", [])),
        "timeZone": network.get("timeZone", ""),
        "url": network.get("url", ""),
    }


def run(dashboard, org_id, network_ids=None, fmt="json", output=None):
    """
    Args:
        dashboard:   Meraki client
        org_id:      organization ID
        network_ids: None for all, or a list of IDs to restrict to
        fmt:         "json" or "csv"
        output:      output file path; defaults to networks.<fmt>
    """
    networks = net_mod.resolve_targets(dashboard, org_id, network_ids=network_ids)
    print(f"  found {len(networks)} network(s)")

    if output is None:
        output = f"networks.{fmt}"

    if fmt == "csv":
        io_mod.save_csv(output, [_row(n) for n in networks], CSV_FIELDS)
    else:
        io_mod.save_json(output, networks)

    return output
