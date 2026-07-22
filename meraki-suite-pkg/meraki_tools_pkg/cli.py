#!/usr/bin/env python3
"""
cli.py — entry point for the Meraki tools.

Commands are grouped by safety tier:
    export ...   read-only (only GETs, writes local files)
    apply  ...   writes to live networks (added later; dry-run by default)

Usage:
    python cli.py export networks
    python cli.py export networks --networks L_1 L_2 --format csv
    python cli.py export networks --org <ORG_ID> --output sites.json

The org ID may be passed with --org or set via the MERAKI_ORG_ID environment
variable. The API key always comes from MERAKI_DASHBOARD_API_KEY.
"""

import argparse
import os
import sys

from merakicore import client as client_mod
from merakicore import networks as net_mod
from merakicore import safety
from commands import export_networks
from commands import export_orgs
from commands import export_groups
from commands import apply_l3_insert
from commands import apply_l3_reinflate
from commands import apply_l3_flattened
from commands import apply_l3_insert_batch
from commands import apply_l3_restore_batch
from commands import export_l3_migration
from commands import export_firewall
from commands import export_l7
from commands import apply_l7
from commands import export_content_filter
from commands import apply_content_filter
from commands import export_policy_check
from commands import apply_policy_group
from commands import apply_policy_bulk
from merakicore import policyobjects as po


def _resolve_org_id(args):
    org = getattr(args, "org_local", None) or args.org or os.environ.get("MERAKI_ORG_ID")
    if not org:
        sys.exit(
            "Error: no organization ID. Pass --org <ID> or set MERAKI_ORG_ID.\n"
            "Tip: run with --org and an ID you can get from the Meraki dashboard."
        )
    return org


def _get_dashboard_or_exit():
    try:
        return client_mod.get_dashboard()
    except client_mod.MissingApiKey as e:
        sys.exit(f"Error: {e}")


def cmd_export_orgs(args):
    # Intentionally does NOT call _resolve_org_id — this is how you find the org ID.
    dashboard = _get_dashboard_or_exit()
    path = export_orgs.run(dashboard, fmt=args.format, output=args.output)
    if path:
        print(f"\nDone. Wrote {path}")


def cmd_export_networks(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        path = export_networks.run(
            dashboard,
            org_id,
            network_ids=args.networks,
            fmt=args.format,
            output=args.output,
        )
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")
    print(f"\nDone. Wrote {path}")


def cmd_export_firewall(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        paths = export_firewall.run(
            dashboard,
            org_id,
            network_ids=args.networks,
            fmt=args.format,
            output=args.output,
        )
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")
    print(f"\nDone. Wrote {', '.join(paths)}")


def cmd_export_l7(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        path = export_l7.run(dashboard, org_id, args.source, output=args.output)
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")
    print(f"\nDone. Wrote {path}")


def cmd_apply_l7(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    # On a real apply, require explicit confirmation first.
    if args.apply:
        print("You are about to APPLY L7 rule changes to live networks.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    try:
        apply_l7.run(
            dashboard, org_id, args.source_file,
            network_ids=args.networks, mode=args.mode,
            position=args.position, apply=args.apply,
        )
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")


def cmd_export_content_filter(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        path = export_content_filter.run(dashboard, org_id, args.source, output=args.output)
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")
    print(f"\nDone. Wrote {path}")


def cmd_apply_content_filter(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    if args.apply:
        print("You are about to APPLY content-filtering changes to live networks.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    try:
        apply_content_filter.run(
            dashboard, org_id, args.source_file,
            network_ids=args.networks, mode=args.mode, apply=args.apply,
        )
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")


def cmd_export_policy_check(args):
    dashboard = _get_dashboard_or_exit()
    entries = po.parse_entries(fqdns=args.fqdn, ips=args.ip, from_file=args.from_file)
    # org optional: --org or MERAKI_ORG_ID limits to one org; otherwise all orgs.
    org_id = args.org or os.environ.get("MERAKI_ORG_ID")
    export_policy_check.run(dashboard, entries, org_id=org_id)


def cmd_apply_policy_group(args):
    dashboard = _get_dashboard_or_exit()
    entries = po.parse_entries(fqdns=args.fqdn, ips=args.ip, from_file=args.from_file)
    apply_policy_group.run(dashboard, entries, apply=args.apply, name_base=args.name)


def cmd_apply_policy_bulk(args):
    dashboard = _get_dashboard_or_exit()
    entries = po.parse_entries(fqdns=args.fqdn, ips=args.ip, from_file=args.from_file)
    org_id = getattr(args, "org_local", None) or args.org or os.environ.get("MERAKI_ORG_ID")
    apply_policy_bulk.run(
        dashboard, entries, org_id=org_id,
        group_prefix=args.group_prefix, group_size=args.group_size,
        name_base=args.name, apply=args.apply,
    )


def cmd_export_groups(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    export_groups.run(dashboard, org_id, fmt=args.format, output=args.output,
                      show_members=args.show_members)


def cmd_apply_l3_insert(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        spec = apply_l3_insert.load_rule_file(args.rule_file)
    except Exception as e:
        sys.exit(f"Error reading rule file: {e}")
    if args.comment:
        spec["comment"] = args.comment.strip()
    if args.apply:
        print("You are about to INSERT an L3 firewall rule at position 1 on live networks.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    try:
        apply_l3_insert.run(dashboard, org_id, spec,
                            network_ids=args.networks, apply=args.apply)
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")


def cmd_export_l3_migration(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    export_l3_migration.run(dashboard, org_id, args.network, output_prefix=args.output)


def cmd_apply_l3_reinflate(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        ruleset = apply_l3_reinflate.load_ruleset(args.rule_file)
    except Exception as e:
        sys.exit(f"Error reading ruleset file: {e}")
    if args.apply:
        print("You are about to REPLACE the L3 ruleset on live networks with reinflated rules.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    try:
        apply_l3_reinflate.run(dashboard, org_id, ruleset,
                               network_ids=args.networks, apply=args.apply)
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")


def cmd_apply_l3_flattened(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        ruleset = apply_l3_flattened.load_ruleset(args.rule_file)
    except Exception as e:
        sys.exit(f"Error reading ruleset file: {e}")
    if args.apply:
        print("You are about to REPLACE the L3 ruleset on live networks with a flattened ruleset.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    try:
        apply_l3_flattened.run(dashboard, org_id, ruleset,
                               network_ids=args.networks, apply=args.apply)
    except net_mod.NoNetworksResolved as e:
        sys.exit(f"Error: {e}")


def cmd_apply_l3_insert_batch(args):
    dashboard = _get_dashboard_or_exit()
    org_id = _resolve_org_id(args)
    try:
        spec = apply_l3_insert.load_rule_file(args.rule_file)
    except Exception as e:
        sys.exit(f"Error reading rule file: {e}")
    if args.comment:
        spec["comment"] = args.comment.strip()
    if args.apply:
        print("You are about to INSERT an L3 rule at position 1 across a BATCH of live networks.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    apply_l3_insert_batch.run(dashboard, org_id, spec, skip=args.skip, limit=args.limit,
                              network_ids=args.networks, apply=args.apply,
                              backup_prefix=args.backup_prefix)


def cmd_apply_l3_restore_batch(args):
    dashboard = _get_dashboard_or_exit()
    try:
        backup = apply_l3_restore_batch.load_backup(args.backup_file)
    except Exception as e:
        sys.exit(f"Error reading backup file: {e}")
    if args.apply:
        print(f"You are about to RESTORE L3 rules to {len(backup.get('networks',{}))} network(s) from backup.")
        if not safety.confirm():
            sys.exit("Aborted. No changes made.")
    apply_l3_restore_batch.run(dashboard, backup, apply=args.apply)


def build_parser():
    parser = argparse.ArgumentParser(prog="meraki-tools", description="Meraki operations toolkit.")
    parser.add_argument("--org", help="Organization ID (or set MERAKI_ORG_ID)")

    sub = parser.add_subparsers(dest="group", required=True)

    # ---- export group (read-only) ----
    export = sub.add_parser("export", help="Read-only data exports")
    export_sub = export.add_subparsers(dest="command", required=True)

    p_orgs = export_sub.add_parser("orgs", help="List organizations the API key can see (no org ID needed)")
    p_orgs.add_argument("--format", choices=["json", "csv"],
                        help="Also write a file in this format (default: print only)")
    p_orgs.add_argument("--output", help="Output file path (default: orgs.<format>)")
    p_orgs.set_defaults(func=cmd_export_orgs)

    p_grp = export_sub.add_parser("groups", help="List an org's policy object groups (name, ID, member count)")
    p_grp.add_argument("--org", dest="org_local", metavar="ID", help="Organization ID (or set MERAKI_ORG_ID)")
    p_grp.add_argument("--format", choices=["json", "csv"], default="json")
    p_grp.add_argument("--output", help="Output file (default: groups_<org>.<fmt>)")
    p_grp.add_argument("--show-members", action="store_true", help="Include object IDs in JSON output")
    p_grp.set_defaults(func=cmd_export_groups)

    p_l3m = export_sub.add_parser("l3-migration",
        help="Export a network's L3 ruleset in migration form (name-referenced + flattened)")
    p_l3m.add_argument("--org", dest="org_local", metavar="ID", help="Organization ID (or set MERAKI_ORG_ID)")
    p_l3m.add_argument("--network", required=True, metavar="ID", help="Source network ID")
    p_l3m.add_argument("--output", metavar="PREFIX", help="Output file prefix")
    p_l3m.set_defaults(func=cmd_export_l3_migration)

    p_net = export_sub.add_parser("networks", help="Export the org's networks")
    p_net.add_argument("--networks", nargs="+", metavar="ID",
                       help="Restrict to these network IDs (default: all)")
    p_net.add_argument("--format", choices=["json", "csv"], default="json",
                       help="Output format (default: json)")
    p_net.add_argument("--output", help="Output file path (default: networks.<format>)")
    p_net.set_defaults(func=cmd_export_networks)

    p_fw = export_sub.add_parser("firewall", help="Export L3 firewall + switch ACL rules")
    p_fw.add_argument("--networks", nargs="+", metavar="ID",
                      help="Restrict to these network IDs (default: all)")
    p_fw.add_argument("--format", choices=["json", "csv"], default="csv",
                      help="Output format (default: csv)")
    p_fw.add_argument("--output", metavar="BASE",
                      help="Output filename base (default: firewall -> firewall_l3.csv, firewall_switch_acl.csv)")
    p_fw.set_defaults(func=cmd_export_firewall)

    p_xl7 = export_sub.add_parser("l7", help="Export one network's L7 firewall rules")
    p_xl7.add_argument("--source", required=True, metavar="NETWORK_ID",
                       help="Network ID to copy L7 rules FROM")
    p_xl7.add_argument("--output", help="Output file (default: l7_<networkid>.json)")
    p_xl7.set_defaults(func=cmd_export_l7)

    p_xcf = export_sub.add_parser("content-filter", help="Export one network's content-filtering settings")
    p_xcf.add_argument("--source", required=True, metavar="NETWORK_ID",
                       help="Network ID to copy content filtering FROM")
    p_xcf.add_argument("--output", help="Output file (default: contentfilter_<networkid>.json)")
    p_xcf.set_defaults(func=cmd_export_content_filter)

    p_pchk = export_sub.add_parser("policy-check",
                                   help="Audit whether FQDNs/IPs exist as policy objects (1 org or all)")
    p_pchk.add_argument("--fqdn", nargs="+", metavar="FQDN", help="FQDN(s) to check")
    p_pchk.add_argument("--ip", nargs="+", metavar="IP", help="IP/CIDR(s) to check")
    p_pchk.add_argument("--from-file", metavar="FILE",
                        help="File of entries (one per line; 'fqdn,'/'ip,' prefix optional)")
    p_pchk.set_defaults(func=cmd_export_policy_check)

    # ---- apply group (writes to live networks) ----
    apply_p = sub.add_parser("apply", help="Write changes to live networks (dry-run by default)")
    apply_sub = apply_p.add_subparsers(dest="command", required=True)

    p_al7 = apply_sub.add_parser("l7", help="Propagate L7 rules from a JSON file onto networks")
    p_al7.add_argument("--from", dest="source_file", required=True, metavar="FILE",
                       help="JSON file produced by `export l7`")
    p_al7.add_argument("--networks", nargs="+", metavar="ID",
                       help="Target network IDs (default: all appliance networks)")
    p_al7.add_argument("--mode", choices=["replace", "append"], default="replace",
                       help="replace (target matches source) or append (add source rules)")
    p_al7.add_argument("--position", choices=["top", "bottom"], default="bottom",
                       help="For append: where to add the rules (default: bottom)")
    p_al7.add_argument("--apply", action="store_true",
                       help="Actually write changes. Without this, runs as a dry run.")
    p_al7.set_defaults(func=cmd_apply_l7)

    p_acf = apply_sub.add_parser("content-filter", help="Propagate content filtering from a JSON file onto networks")
    p_acf.add_argument("--from", dest="source_file", required=True, metavar="FILE",
                       help="JSON file produced by `export content-filter`")
    p_acf.add_argument("--networks", nargs="+", metavar="ID",
                       help="Target network IDs (default: all appliance networks)")
    p_acf.add_argument("--mode", choices=["replace", "append"], default="replace",
                       help="replace (target matches source) or append (merge source in)")
    p_acf.add_argument("--apply", action="store_true",
                       help="Actually write changes. Without this, runs as a dry run.")
    p_acf.set_defaults(func=cmd_apply_content_filter)

    p_pg = apply_sub.add_parser("policy-group",
                                help="Create policy objects and add them to a chosen group")
    p_pg.add_argument("--fqdn", nargs="+", metavar="FQDN", help="FQDN(s) to add")
    p_pg.add_argument("--ip", nargs="+", metavar="IP", help="IP/CIDR(s) to add")
    p_pg.add_argument("--from-file", metavar="FILE",
                      help="File of entries (one per line; 'fqdn,'/'ip,' prefix optional)")
    p_pg.add_argument("--name", metavar="BASE",
                      help="Base name for new objects; they become 'BASE 1', 'BASE 2', ... "
                           "(continues from the highest existing number). Default: name by value.")
    p_pg.add_argument("--apply", action="store_true",
                      help="Actually create objects and update the group. Without this, dry run.")
    p_pg.set_defaults(func=cmd_apply_policy_group)

    p_pb = apply_sub.add_parser("policy-bulk",
                                help="Create many objects and spread them across numbered groups")
    p_pb.add_argument("--fqdn", nargs="+", metavar="FQDN", help="FQDN(s) to add")
    p_pb.add_argument("--ip", nargs="+", metavar="IP", help="IP/CIDR(s) to add")
    p_pb.add_argument("--from-file", metavar="FILE",
                      help="File of entries (one per line; 'fqdn,'/'ip,' prefix optional)")
    p_pb.add_argument("--org", dest="org_local", metavar="ID",
                      help="Organization ID (or set MERAKI_ORG_ID, or use the global --org)")
    p_pb.add_argument("--group-prefix", required=True, metavar="PREFIX",
                      help="Base name for the group series, e.g. 'Spamhaus Group' -> 'Spamhaus Group 1', ...")
    p_pb.add_argument("--group-size", type=int, default=140, metavar="N",
                      help="Max members per group before spilling to the next (default: 140)")
    p_pb.add_argument("--name", metavar="BASE",
                      help="Base name for new objects ('BASE 1', 'BASE 2', ...). Default: name by value.")
    p_pb.add_argument("--apply", action="store_true",
                      help="Actually create objects/groups. Without this, prints the plan only.")
    p_pb.set_defaults(func=cmd_apply_policy_bulk)

    p_l3 = apply_sub.add_parser("l3-insert", help="Insert one L3 firewall rule at position 1 across networks")
    p_l3.add_argument("--org", dest="org_local", metavar="ID", help="Organization ID (or set MERAKI_ORG_ID)")
    p_l3.add_argument("--rule-file", required=True, metavar="FILE",
                      help="JSON rule file (dest references by NAME)")
    p_l3.add_argument("--comment", help="Override the rule name/comment")
    p_l3.add_argument("--networks", nargs="+", metavar="ID",
                      help="Target network IDs (default: all appliance networks)")
    p_l3.add_argument("--apply", action="store_true",
                      help="Actually write. Without this, dry run.")
    p_l3.set_defaults(func=cmd_apply_l3_insert)

    p_l3r = apply_sub.add_parser("l3-reinflate",
        help="Rebuild object/group references in an L3 ruleset (name -> target org IDs)")
    p_l3r.add_argument("--org", dest="org_local", metavar="ID", help="Target organization ID")
    p_l3r.add_argument("--rule-file", required=True, metavar="FILE",
                       help="Name-referenced ruleset JSON (from export l3-migration)")
    p_l3r.add_argument("--networks", nargs="+", metavar="ID",
                       help="Target network IDs (default: all appliance networks)")
    p_l3r.add_argument("--apply", action="store_true", help="Actually write. Without this, dry run.")
    p_l3r.set_defaults(func=cmd_apply_l3_reinflate)

    p_l3f = apply_sub.add_parser("l3-flattened",
        help="Apply a flattened (self-contained) L3 ruleset onto networks (migration online step)")
    p_l3f.add_argument("--org", dest="org_local", metavar="ID", help="Target organization ID")
    p_l3f.add_argument("--rule-file", required=True, metavar="FILE",
                       help="Flattened ruleset JSON (the *_flattened.json from export l3-migration)")
    p_l3f.add_argument("--networks", nargs="+", metavar="ID",
                       help="Target network IDs (default: all appliance networks)")
    p_l3f.add_argument("--apply", action="store_true", help="Actually write. Without this, dry run.")
    p_l3f.set_defaults(func=cmd_apply_l3_flattened)

    p_l3b = apply_sub.add_parser("l3-insert-batch",
        help="Insert an L3 rule at position 1 across a BATCH of networks (with backup)")
    p_l3b.add_argument("--org", dest="org_local", metavar="ID", help="Organization ID")
    p_l3b.add_argument("--rule-file", required=True, metavar="FILE", help="JSON rule file (dest by NAME)")
    p_l3b.add_argument("--comment", help="Override the rule name/comment")
    p_l3b.add_argument("--skip", type=int, default=0, metavar="M", help="Skip the first M networks")
    p_l3b.add_argument("--limit", type=int, default=None, metavar="N", help="Process at most N networks this batch")
    p_l3b.add_argument("--networks", nargs="+", metavar="ID", help="Restrict to these network IDs before slicing")
    p_l3b.add_argument("--backup-prefix", metavar="PREFIX", help="Backup file prefix")
    p_l3b.add_argument("--apply", action="store_true", help="Actually write. Without this, dry run.")
    p_l3b.set_defaults(func=cmd_apply_l3_insert_batch)

    p_l3rb = apply_sub.add_parser("l3-restore-batch",
        help="Roll back a batch: restore each network's L3 rules from a batch backup file")
    p_l3rb.add_argument("--backup-file", required=True, metavar="FILE", help="Batch backup JSON from l3-insert-batch")
    p_l3rb.add_argument("--apply", action="store_true", help="Actually write. Without this, dry run.")
    p_l3rb.set_defaults(func=cmd_apply_l3_restore_batch)

    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if not hasattr(args, "func"):
        parser.print_help()
        sys.exit(1)
    args.func(args)


if __name__ == "__main__":
    main()
