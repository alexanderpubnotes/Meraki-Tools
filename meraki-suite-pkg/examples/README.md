# Example input files

Sample files for the functions that take a file input. Copy these, edit, and
point the function's file field at your copy.

## policy_entries.txt  —  you write this

Used by **Policy check**, **Policy -> group**, and **Policy -> bulk groups**.
A plain-text list of IPs, CIDRs, and FQDNs — one per line.

- A plain line is auto-typed (the tool decides IP/CIDR vs FQDN).
- Prefix with `ip,` or `fqdn,` to set the type explicitly.
- `#` lines and blank lines are ignored.

To use it: copy the file, replace the example values with your real ones, add as
many lines as you need, then select it in the function's "From file" field.

## sample_l7_export.json  —  the tool makes this (don't hand-write)

This is an example of what **Export L7 (from a network)** produces. You feed a
file like this to **Apply L7 rules**.

**You should not write this by hand.** The correct workflow is: configure one
network's L7 rules in the dashboard, run **Export L7** against it to get a real
file, then **Apply L7** that file onto other networks. This sample is here only
so you can recognize a valid file and understand what you're propagating. The
`value` field for `application`/`applicationCategory` rules is a Meraki object
with an `id` — those IDs come from Meraki and must not be guessed.

## sample_content_filter_export.json  —  the tool makes this (don't hand-write)

An example of what **Export content filter** produces, fed to **Apply content
filter**. Same rule as above: get the real file from Export, don't author it
by hand. It contains allowed/blocked URL patterns and blocked category IDs
(the category IDs are Meraki's own format).

---

**Note on example values:** all addresses/domains here use reserved
documentation ranges (192.0.2.x, 198.51.100.x, 203.0.113.x, and `.example`
domains), so they're safe placeholders. Replace them with your real values.
