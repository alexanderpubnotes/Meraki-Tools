"""
export_content_filter.py — export a network's content-filtering settings to JSON.

Read-only. The "copy from" step: configure one network's content filtering in
the dashboard, export it here, then `apply content-filter --from` onto others.

The exported JSON stores the raw GET (for reference) and a normalized block
(the four updatable fields, categories flattened to IDs) ready to push.
"""

from merakicore import networks as net_mod
from merakicore import io as io_mod
from merakicore import contentfilter as cf
from merakicore import paths


def _get(dashboard, network_id):
    return dashboard.appliance.getNetworkApplianceContentFiltering(network_id)


def run(dashboard, org_id, source_network_id, output=None):
    networks = net_mod.resolve_targets(
        dashboard, org_id, network_ids=[source_network_id], product_type="appliance"
    )
    source = networks[0]
    print(f"  source: {source['name']} ({source['id']})")

    raw = _get(dashboard, source["id"])
    normalized = cf.normalize_settings(raw)
    print(f"  {cf.summarize(normalized)}")

    payload = {
        "source_network_id": source["id"],
        "source_network_name": source["name"],
        "settings": normalized,     # ready to push
        "raw": raw,                 # kept for reference / audit
    }
    out = output or paths.default_path("exports", f"contentfilter_{source['id']}.json")
    io_mod.save_json(out, payload)
    return out
