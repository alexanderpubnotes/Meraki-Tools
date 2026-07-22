"""
safety.py — the shared write-safety harness for every `apply` command.

Goals (uniform across all write commands):
  - Dry run is the DEFAULT. Nothing changes unless the caller passes apply=True.
  - Before any change, show a readout of what will happen per network.
  - Each network is handled independently: one failure doesn't stop the rest.
  - The API error message for a failed network is surfaced, not swallowed.
  - A clear summary at the end (succeeded / failed / unchanged).

A command provides a per-network callable that performs the actual write and
returns a short result string. The harness handles dry-run gating, ordering,
error capture, and reporting around it.
"""


class WriteResult:
    """Tally of what happened across the targeted networks."""

    def __init__(self):
        self.succeeded = []   # (name, id, detail)
        self.unchanged = []   # (name, id, detail)
        self.failed = []      # (name, id, error)
        self.cancelled = False

    def print_summary(self, dry_run):
        mode = "DRY RUN (no changes made)" if dry_run else "APPLIED"
        print("\n" + "=" * 60)
        print(f"Summary — {mode}" + ("  (CANCELLED early)" if self.cancelled else ""))
        print("=" * 60)
        print(f"  would change / changed : {len(self.succeeded)}")
        print(f"  unchanged              : {len(self.unchanged)}")
        print(f"  failed                 : {len(self.failed)}")
        if self.failed:
            print("\n  Failures:")
            for name, nid, err in self.failed:
                print(f"    - {name} ({nid}): {err}")


def run_write(networks, action, dry_run=True, describe=None,
              progress_cb=None, cancel_event=None):
    """
    Execute a write action across networks with dry-run gating and reporting.

    Args:
        networks: list of network dicts (from the resolver).
        action:   callable(network, dry_run) -> (status, detail)
                  status is one of: "changed", "unchanged".
                  In dry_run mode the callable MUST NOT write; it returns what it
                  *would* do (status "changed" + a description, or "unchanged").
                  Raising from the callable is treated as a failure for that
                  network only.
        dry_run:  if True (default), action is told not to write.
        describe: optional callable(network) -> str, printed before each action
                  (e.g. a per-network preview line).
        progress_cb: optional callable(done, total), called after each network.
        cancel_event: optional threading.Event; checked BEFORE each network so
                  a cancellation never interrupts a network mid-write — it only
                  stops the loop from starting the next one.

    Returns:
        WriteResult
    """
    result = WriteResult()
    total = len(networks)
    for i, net in enumerate(networks, 1):
        if cancel_event is not None and cancel_event.is_set():
            print(f"\n  Cancelled — stopped before network {i}/{total}.")
            result.cancelled = True
            break
        name, nid = net.get("name", "(unnamed)"), net["id"]
        if describe:
            print(f"  {describe(net)}")
        try:
            status, detail = action(net, dry_run)
            if status == "changed":
                result.succeeded.append((name, nid, detail))
                verb = "would change" if dry_run else "changed"
                print(f"    {verb}: {name} ({nid}) — {detail}")
            else:
                result.unchanged.append((name, nid, detail))
                print(f"    unchanged: {name} ({nid}) — {detail}")
        except Exception as e:
            result.failed.append((name, nid, str(e)))
            print(f"    FAILED: {name} ({nid}) — {e}")
        if progress_cb:
            progress_cb(i, total)
    return result


def confirm(prompt="Proceed with applying these changes? [y/N]: "):
    """
    Ask for explicit confirmation on a real (non-dry-run) apply.
    Returns True only on an explicit yes. Defaults to No.
    Headless callers (e.g. a GUI) should not use this; they confirm their own way.
    """
    try:
        answer = input(prompt).strip().lower()
    except EOFError:
        return False
    return answer in ("y", "yes")
