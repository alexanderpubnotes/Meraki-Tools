"""
main.py — entry point for the Meraki org-to-org migration tool.

Usage:
    python main.py orgs
        List every organization your API key can see (IDs and names).

    python main.py backup --org <SOURCE_ORG_ID>
        Back up the source org's policy objects and groups to a new
        timestamped folder under ./backups/.

    python main.py restore --org <TARGET_ORG_ID> --backup <BACKUP_FOLDER>
        Recreate the backed-up policy objects/groups in the target org.
        If --backup is omitted, the most recent backup folder is used.

The API key is read from the MERAKI_DASHBOARD_API_KEY environment variable.
"""

import argparse
import os
import sys
from datetime import datetime

import common
import network_settings
import policy_objects
import switch_settings


def cmd_orgs(dashboard, args):
    orgs = dashboard.organizations.getOrganizations()
    common.announce(f"Your API key has access to {len(orgs)} organization(s)")
    width = max(len(o["id"]) for o in orgs)
    for o in sorted(orgs, key=lambda o: o["name"].lower()):
        print(f"  {o['id']:<{width}}  {o['name']}")


def cmd_backup(dashboard, args):
    org_id = args.org
    # Confirm the org exists / we can see it, and grab its name for the folder.
    org = dashboard.organizations.getOrganization(organizationId=org_id)
    stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in org["name"])
    backup_dir = f"{common.BACKUP_ROOT}/{org_id}_{safe_name}_{stamp}"

    nets = args.networks.split(",") if args.networks else None
    common.announce(f"Backup of '{org['name']}' ({org_id}) -> {backup_dir}")
    if args.scope in ("objects", "all"):
        policy_objects.backup(dashboard, org_id, backup_dir)
    if args.scope in ("networks", "all"):
        network_settings.backup(dashboard, org_id, backup_dir, network_filter=nets)
    print(f"\nDone. Backup folder: {backup_dir}")


def find_latest_backup():
    if not os.path.isdir(common.BACKUP_ROOT):
        return None
    folders = [
        os.path.join(common.BACKUP_ROOT, d)
        for d in os.listdir(common.BACKUP_ROOT)
        if os.path.isdir(os.path.join(common.BACKUP_ROOT, d))
    ]
    return max(folders, default=None)  # timestamped names sort chronologically


def cmd_restore(dashboard, args):
    backup_dir = args.backup or find_latest_backup()
    if not backup_dir or not os.path.isdir(backup_dir):
        sys.exit("Error: no backup folder found. Run a backup first or pass --backup <folder>.")

    org = dashboard.organizations.getOrganization(organizationId=args.org)
    nets = args.networks.split(",") if args.networks else None
    common.announce(f"Restore from {backup_dir} -> '{org['name']}' ({args.org})")
    if not args.dry_run:
        print(f"This will CREATE/UPDATE settings (scope: {args.scope}) in the target org.")
        if input("Proceed? (y/N): ").strip().lower() != "y":
            sys.exit("Aborted.")

    if args.scope == "vpn":
        network_settings.restore_vpn_only(dashboard, args.org, backup_dir,
                                          network_filter=nets, dry_run=args.dry_run)
        return
    id_map = None
    if args.scope in ("objects", "all"):
        id_map = policy_objects.restore(dashboard, args.org, backup_dir, dry_run=args.dry_run)
    if args.scope in ("networks", "all"):
        # Pass the in-memory id map so a combined dry run previews the
        # OBJ()/GRP() rewrite instead of reporting false l3_firewall errors.
        network_settings.restore(dashboard, args.org, backup_dir,
                                 dry_run=args.dry_run, network_filter=nets,
                                 id_map=id_map)
    print("\nDone.")


def cmd_restore_ports(dashboard, args):
    backup_dir = args.backup or find_latest_backup()
    if not backup_dir or not os.path.isdir(backup_dir):
        sys.exit("Error: no backup folder found. Run a backup first or pass --backup <folder>.")
    org = dashboard.organizations.getOrganization(organizationId=args.org)
    nets = args.networks.split(",") if args.networks else None
    common.announce(f"Switch port restore from {backup_dir} -> '{org['name']}' ({args.org})")
    if not args.dry_run:
        print("This will OVERWRITE port configs on name-matched switches in the target org.")
        if input("Proceed? (y/N): ").strip().lower() != "y":
            sys.exit("Aborted.")
    switch_settings.restore_ports(dashboard, args.org, backup_dir,
                                  network_filter=nets, dry_run=args.dry_run)


def main():
    parser = argparse.ArgumentParser(description="Meraki org-to-org migration tool")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("orgs", help="List organizations visible to your API key")

    p_backup = sub.add_parser("backup", help="Back up an organization")
    p_backup.add_argument("--org", required=True, help="Source organization ID")
    p_backup.add_argument("--scope", choices=["objects", "networks", "all"], default="all",
                          help="What to back up (default: all)")
    p_backup.add_argument("--networks", help="Comma-separated network names (default: all networks)")

    p_restore = sub.add_parser("restore", help="Restore a backup into an organization")
    p_restore.add_argument("--org", required=True, help="Target organization ID")
    p_restore.add_argument("--backup", help="Backup folder (default: most recent)")
    p_restore.add_argument("--dry-run", action="store_true",
                           help="Show what would be created/updated without changing anything")
    p_restore.add_argument("--scope", choices=["objects", "networks", "vpn", "all"], default="all",
                           help="What to restore (default: all; 'vpn' re-runs only the site-to-site pass)")
    p_restore.add_argument("--networks", help="Comma-separated network names (default: all in backup)")

    p_ports = sub.add_parser("restore-ports",
                             help="Apply switch port configs (run AFTER new switches are claimed; matches by device name)")
    p_ports.add_argument("--org", required=True, help="Target organization ID")
    p_ports.add_argument("--backup", help="Backup folder (default: most recent)")
    p_ports.add_argument("--networks", help="Comma-separated network names (default: all in backup)")
    p_ports.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    dashboard = common.get_dashboard()

    {"orgs": cmd_orgs, "backup": cmd_backup, "restore": cmd_restore,
     "restore-ports": cmd_restore_ports}[args.command](dashboard, args)


if __name__ == "__main__":
    main()
