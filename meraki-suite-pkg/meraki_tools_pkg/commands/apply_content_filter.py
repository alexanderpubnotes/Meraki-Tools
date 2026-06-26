"""
apply_content_filter.py — propagate content-filtering settings from a JSON file.

WRITE command, same model as apply_l7:
  - Never authors settings; takes JSON from `export content-filter`.
  - mode=replace : push the source settings as-is (target matches source).
  - mode=append  : merge source into the target's current settings (union of
                   URL patterns and categories, dedup).
  - Dry run is the default; apply=True writes.

The update endpoint replaces the whole content-filtering config, so replace
sends source fields directly and append builds a merged dict first.
"""

from merakicore import networks as net_mod
from merakicore import io as io_mod
from merakicore import contentfilter as cf
from merakicore import safety


def _get_current(dashboard, network_id):
    raw = dashboard.appliance.getNetworkApplianceContentFiltering(network_id)
    return cf.normalize_settings(raw)


def run(dashboard, org_id, source_file, network_ids=None, mode="replace", apply=False):
    data = io_mod.load_json(source_file)
    source = data.get("settings") or cf.normalize_settings(data.get("raw", {}))
    src_name = data.get("source_network_name", "?")
    print(f"  source file: {source_file}  ({cf.summarize(source)} from '{src_name}')")
    print(f"  mode: {mode}")

    targets = net_mod.resolve_targets(
        dashboard, org_id, network_ids=network_ids, product_type="appliance"
    )
    src_id = data.get("source_network_id")
    targets = [t for t in targets if t["id"] != src_id] or targets

    print("\n" + net_mod.confirmation_readout(targets))
    dry_run = not apply

    def action(net, is_dry):
        if mode == "replace":
            new_settings = source
            detail = "replace"
        else:
            current = _get_current(dashboard, net["id"])
            new_settings = cf.merge_settings(current, source)
            # If merge changed nothing, report unchanged.
            if new_settings == current:
                return "unchanged", "all source entries already present"
            detail = "merge"

        if is_dry:
            return "changed", f"{detail} -> would set [{cf.summarize(new_settings)}]"
        dashboard.appliance.updateNetworkApplianceContentFiltering(net["id"], **new_settings)
        return "changed", f"{detail} -> set [{cf.summarize(new_settings)}]"

    result = safety.run_write(targets, action, dry_run=dry_run)
    result.print_summary(dry_run)
    return result
