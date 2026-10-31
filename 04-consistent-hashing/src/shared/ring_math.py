"""Pure consistent-hashing ring functions, shared by the ring and kv Lambdas.

scripts/deploy.sh copies this file next to each handler before packaging, so both
functions import it as a top-level module (`from ring_math import ...`).

SHA-1 positions are 0 .. 2^160 - 1. A key belongs to the first vnode clockwise whose
position is >= the key's position, wrapping to the first vnode. The ring is a sorted
list of (position, serverId, vnodeIndex).
"""
import bisect
import hashlib

RING_SIZE = 2 ** 160


def position(text):
    """SHA-1 position of a string on the ring, as an int in 0 .. 2^160 - 1."""
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest(), 16)


def to_hex(pos):
    return format(pos, "040x")


def vnodes_for(server_id, count):
    """Virtual nodes of one server: list of (position, serverId, vnodeIndex)."""
    return [(position(f"{server_id}#{i}"), server_id, i) for i in range(count)]


def build_ring(vnodes):
    """Sort vnodes by position. The ring is a sorted list of (position, serverId, vnodeIndex)."""
    return sorted(vnodes)


def lookup(ring, pos):
    """Index of the first vnode clockwise with position >= pos (wraps to 0)."""
    i = bisect.bisect_left(ring, (pos,))
    return i % len(ring)


def preference_list(ring, pos, n):
    """First n distinct physical servers clockwise from pos, skipping repeat servers."""
    start = lookup(ring, pos)
    chosen = []
    for step in range(len(ring)):
        _, server_id, _ = ring[(start + step) % len(ring)]
        if server_id not in chosen:
            chosen.append(server_id)
            if len(chosen) == n:
                break
    return chosen


def shares(ring):
    """Share of the ring (percent) owned by each server."""
    owned = {}
    for i, (pos, server_id, _) in enumerate(ring):
        prev = ring[i - 1][0]
        length = (pos - prev) % RING_SIZE or (RING_SIZE if len(ring) == 1 else 0)
        owned[server_id] = owned.get(server_id, 0) + length
    return {s: round(100 * v / RING_SIZE, 2) for s, v in owned.items()}


def changed_ranges(old_ring, new_ring):
    """Key ranges (start, end] whose owner differs between two rings.

    Every vnode position from both rings is a boundary. Within one segment between
    consecutive boundaries, each ring has a single owner: the vnode at the segment's end.
    Adjacent segments with the same (from, to) pair are merged.
    """
    points = sorted({v[0] for v in old_ring} | {v[0] for v in new_ring})
    out = []
    for i, end in enumerate(points):
        start = points[i - 1]
        old_owner = old_ring[lookup(old_ring, end)][1] if old_ring else None
        new_owner = new_ring[lookup(new_ring, end)][1] if new_ring else None
        if old_owner == new_owner:
            continue
        if out and out[-1]["end"] == start and (out[-1]["from"], out[-1]["to"]) == (old_owner, new_owner):
            out[-1]["end"] = end
        else:
            out.append({"start": start, "end": end, "from": old_owner, "to": new_owner})
    return [{"start": to_hex(r["start"]), "end": to_hex(r["end"]),
             "fromServer": r["from"], "toServer": r["to"]} for r in out]
