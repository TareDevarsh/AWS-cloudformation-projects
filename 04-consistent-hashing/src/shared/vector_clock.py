"""Pure vector-clock functions, shared by the kv Lambda (step 3: versioning + conflict resolution).

scripts/deploy.sh copies this file next to the kv handler before packaging, so the function
imports it as a top-level module (`from vector_clock import ...`), the same way ring_math.py is shared.

A vector clock is `{serverId: {"counter": int, "updatedAt": "ISO-8601"}}`. It follows the Dynamo
rule from docs/kv-2-replication-consistency.md section 7: when server Si handles a write it
increments its own counter (or starts it at 1). `updatedAt` on each entry records when that entry
last changed, so truncation can drop the oldest entries deterministically.

Comparison treats a server missing from a clock as counter 0:
- X is an ancestor of Y (Y replaces X) if every counter in X is <= the matching counter in Y.
- X descends from Y if Y is an ancestor of X.
- X and Y are concurrent (siblings, a conflict) if neither is an ancestor of the other.
"""
import base64
import json

# Comparison outcomes between two clocks.
EQUAL = "equal"
ANCESTOR = "ancestor"      # self is an ancestor of other (other is newer)
DESCENDANT = "descendant"  # self descends from other (self is newer)
CONCURRENT = "concurrent"  # siblings: a genuine conflict

MAX_CONTEXT_BYTES = 4 * 1024  # size limit on a decoded client context, before validation


def counter(clock, server_id):
    """Counter for one server, 0 if the server is absent (a missing server counts as 0)."""
    entry = clock.get(server_id)
    return entry["counter"] if entry else 0


def servers(*clocks):
    """Union of server ids across any number of clocks."""
    out = set()
    for clock in clocks:
        out.update(clock)
    return out


def increment(clock, server_id, now):
    """Return a new clock with server_id's counter bumped by 1 and its updatedAt set to now."""
    out = {s: dict(e) for s, e in clock.items()}
    out[server_id] = {"counter": counter(clock, server_id) + 1, "updatedAt": now}
    return out


def compare(a, b):
    """Relationship of clock a to clock b: EQUAL, ANCESTOR, DESCENDANT or CONCURRENT."""
    a_greater = b_greater = False
    for s in servers(a, b):
        ca, cb = counter(a, s), counter(b, s)
        if ca > cb:
            a_greater = True
        elif cb > ca:
            b_greater = True
    if a_greater and b_greater:
        return CONCURRENT
    if a_greater:
        return DESCENDANT
    if b_greater:
        return ANCESTOR
    return EQUAL


def descends(a, b):
    """True if clock a descends from (or equals) clock b, so a supersedes b."""
    return compare(a, b) in (DESCENDANT, EQUAL)


def merge(*clocks):
    """Pointwise max of several clocks. Each entry keeps the latest updatedAt for the winning counter."""
    out = {}
    for clock in clocks:
        for s, entry in clock.items():
            cur = out.get(s)
            if cur is None or entry["counter"] > cur["counter"] or (
                    entry["counter"] == cur["counter"] and entry["updatedAt"] > cur["updatedAt"]):
                out[s] = {"counter": entry["counter"], "updatedAt": entry["updatedAt"]}
    return out


def truncate(clock, max_entries):
    """Cap a clock at max_entries, dropping the entries with the oldest updatedAt first.

    Truncation can make an ancestor look like a sibling, so reconciliation becomes inaccurate
    (false conflicts). Documented as a known trade-off, per the Dynamo paper.
    """
    if len(clock) <= max_entries:
        return {s: dict(e) for s, e in clock.items()}
    # Keep the newest by updatedAt; serverId breaks ties so the result is deterministic.
    keep = sorted(clock.items(), key=lambda kv: (kv[1]["updatedAt"], kv[0]), reverse=True)[:max_entries]
    return {s: dict(e) for s, e in keep}


def is_clock(obj):
    """Validate the shape of a decoded clock: {str: {"counter": int >= 0, "updatedAt": str}}."""
    if not isinstance(obj, dict):
        return False
    for s, entry in obj.items():
        if not isinstance(s, str) or not s:
            return False
        if not isinstance(entry, dict):
            return False
        c, u = entry.get("counter"), entry.get("updatedAt")
        if isinstance(c, bool) or not isinstance(c, int) or c < 0:
            return False
        if not isinstance(u, str) or not u:
            return False
    return True


def encode_context(clock):
    """Opaque context string: base64url(JSON of the clock), no padding."""
    raw = json.dumps(clock, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_context(context):
    """Decode a client context back to a clock. Returns {} for None/empty.

    Raises ValueError if it is not valid base64url, too large, not JSON, or not a clock shape.
    """
    if context is None or context == "":
        return {}
    if not isinstance(context, str):
        raise ValueError("context must be a string")
    padded = context + "=" * (-len(context) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    except (ValueError, TypeError):
        raise ValueError("context is not valid base64url")
    if len(raw) > MAX_CONTEXT_BYTES:
        raise ValueError(f"context exceeds {MAX_CONTEXT_BYTES} bytes")
    try:
        clock = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        raise ValueError("context is not valid JSON")
    if not is_clock(clock):
        raise ValueError("context is not a valid vector clock")
    return clock
