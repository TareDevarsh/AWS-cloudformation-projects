"""Pure Merkle-tree functions for anti-entropy repair (step 5: permanent failures).

scripts/deploy.sh copies this file next to the kv handler before packaging, so the function
imports it as a top-level module (`from merkle import ...`), the same way ring_math.py and
vector_clock.py are shared.

A Merkle tree summarises one server's stored items for a key range so two servers can find the
keys that differ without shipping every key. Following docs/kv-3-failure-handling.md section 10:

1. Divide the key space into B buckets (B a power of two, so the tree above the buckets is a clean
   binary tree). A key goes to bucket `position(key) % B`, reusing the SHA-1 ring hash so placement
   is stable and independent of which server built the tree.
2. Each bucket (leaf) hash is a hash over the sorted (key, per-version content hash) of the keys in
   it. A per-version content hash covers the value AND the vector clock, so a differing value or a
   differing/extra sibling clock changes the bucket hash.
3. Parent hashes are built pairwise up the tree until a single root remains.

Comparing two trees: equal roots mean the ranges match and nothing else is read. Otherwise `diff`
walks down only the branches whose hashes differ and returns the differing bucket indices, so only
those buckets' keys are ever enumerated for repair.
"""
import hashlib
import json

from ring_math import position

DEFAULT_BUCKETS = 16


def is_power_of_two(n):
    return isinstance(n, int) and n >= 1 and (n & (n - 1)) == 0


def bucket_of(key, buckets):
    """Stable bucket index for a key: SHA-1 ring position modulo the bucket count."""
    return position(key) % buckets


def version_hash(version):
    """Content hash of one stored version: covers its value and its vector clock.

    Two versions with the same value but different clocks (or a missing sibling) hash differently,
    so a bucket's hash changes whenever its reconciled contents drift between servers.
    """
    payload = json.dumps([version.get("value"), version.get("clock", {})],
                         sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()


def key_hash(key, versions):
    """Content hash of one key: the key plus the sorted content hashes of all its versions."""
    parts = [key] + sorted(version_hash(v) for v in versions)
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()


def _hash_pair(left, right):
    return hashlib.sha1((left + right).encode("utf-8")).hexdigest()


EMPTY_LEAF = hashlib.sha1(b"").hexdigest()  # a bucket with no keys


def build_tree(items, buckets=DEFAULT_BUCKETS):
    """Build a Merkle tree over a server's items for a range.

    `items` is a list of {"key": str, "versions": [...]}; `buckets` is a power of two. Returns a dict
    {"buckets": B, "leaves": [hash per bucket], "levels": [[leaf hashes], ..., [root]], "root": hash}.
    An empty bucket hashes to EMPTY_LEAF so trees built from different item orders still match.
    """
    if not is_power_of_two(buckets):
        raise ValueError("buckets must be a power of two")
    # Collect keys per bucket, then hash each bucket over its sorted key hashes.
    per_bucket = [[] for _ in range(buckets)]
    for item in items:
        per_bucket[bucket_of(item["key"], buckets)].append((item["key"], item["versions"]))
    leaves = []
    for keys in per_bucket:
        if not keys:
            leaves.append(EMPTY_LEAF)
            continue
        digests = sorted(key_hash(k, v) for k, v in keys)
        leaves.append(hashlib.sha1("|".join(digests).encode("utf-8")).hexdigest())
    # Build parents pairwise up to the root.
    levels = [leaves]
    level = leaves
    while len(level) > 1:
        level = [_hash_pair(level[i], level[i + 1]) for i in range(0, len(level), 2)]
        levels.append(level)
    return {"buckets": buckets, "leaves": leaves, "levels": levels, "root": levels[-1][0]}


def diff(tree_a, tree_b):
    """Bucket indices whose leaf hash differs between two trees, found by walking differing branches.

    Both trees must share the same bucket count. Descends only the subtrees whose node hashes differ,
    so matching subtrees are never explored — the work is proportional to the number of differences.
    Returns a sorted list of differing bucket indices; empty when the roots match.
    """
    if tree_a["buckets"] != tree_b["buckets"]:
        raise ValueError("trees have different bucket counts")
    levels_a, levels_b = tree_a["levels"], tree_b["levels"]
    top = len(levels_a) - 1
    if levels_a[top][0] == levels_b[top][0]:
        return []
    # Walk down from the root. `frontier` holds node indices still differing at the current level.
    frontier = [0]
    for level in range(top, 0, -1):
        child_level = level - 1
        next_frontier = []
        for node in frontier:
            for child in (2 * node, 2 * node + 1):
                if levels_a[child_level][child] != levels_b[child_level][child]:
                    next_frontier.append(child)
        frontier = next_frontier
    return sorted(frontier)
