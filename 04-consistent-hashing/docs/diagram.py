"""Render the concept diagrams for consistent hashing (phase 1: docs only).
    pip install matplotlib
    python3 docs/diagram.py   # writes docs/images/*.png and prints the rehashing table
Ring positions are illustrative angles, not real SHA-1 values. The same
s0..s3 / k0..k3 positions are used in every ring diagram so they compare directly.
"""
import math
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyArrowPatch

OUT = Path(__file__).parent / "images"
OUT.mkdir(exist_ok=True)

# Degrees clockwise from the top of the ring (0 = 12 o'clock = x0 / xn).
SERVERS = {"s0": 20, "s1": 110, "s2": 200, "s3": 290}
KEYS = {"k0": 60, "k1": 150, "k2": 250, "k3": 340}
NEW_SERVER = ("s4", 270)            # between k2 and s3
REMOVED_SERVER = "s1"

# Rehashing example: illustrative hash values for 8 keys.
HASHES = {"key0": 18358617, "key1": 26143584, "key2": 18131146, "key3": 35863496,
          "key4": 34085809, "key5": 27581703, "key6": 38164978, "key7": 22530351}

S_COLOR, K_COLOR, HI_COLOR, NEW_COLOR = "#1f77b4", "#2ca02c", "#d62728", "#ff7f0e"


def xy(deg, r=1.0):
    a = math.radians(90 - deg)      # clockwise from top
    return r * math.cos(a), r * math.sin(a)


def owner(key_deg, servers):
    """First server clockwise from the key."""
    return min(servers, key=lambda s: (servers[s] - key_deg) % 360)


def ring_axes(ax, title):
    ax.add_patch(plt.Circle((0, 0), 1, fill=False, lw=2, color="#555"))
    ax.set_xlim(-1.5, 1.5); ax.set_ylim(-1.5, 1.5)
    ax.set_aspect("equal"); ax.axis("off")
    ax.set_title(title, fontsize=13)
    # clockwise direction hint
    ax.add_patch(FancyArrowPatch(xy(-25, 1.32), xy(15, 1.32), connectionstyle="arc3,rad=-0.2",
                                 arrowstyle="-|>", mutation_scale=14, color="#999"))
    ax.text(*xy(-5, 1.45), "clockwise", ha="center", fontsize=8, color="#999")


def draw_nodes(ax, servers, keys, highlight=(), new=(), crossed=()):
    for name, deg in servers.items():
        c = NEW_COLOR if name in new else S_COLOR
        ax.plot(*xy(deg), "s", ms=16, color=c, zorder=3)
        ax.text(*xy(deg, 1.2), name, ha="center", va="center", fontsize=12, color=c, weight="bold")
    for name in crossed:
        deg = SERVERS[name]
        ax.plot(*xy(deg), "s", ms=16, color="#bbb", zorder=3)
        ax.plot(*xy(deg), "x", ms=18, mew=3, color=HI_COLOR, zorder=4)
        ax.text(*xy(deg, 1.2), name + "\n(removed)", ha="center", va="center", fontsize=10, color="#999")
    for name, deg in keys.items():
        c = HI_COLOR if name in highlight else K_COLOR
        ax.plot(*xy(deg), "o", ms=12, color=c, zorder=3)
        ax.text(*xy(deg, 0.8), name, ha="center", va="center", fontsize=11, color=c)


def draw_lookup(ax, servers, keys, highlight=()):
    for k, kdeg in keys.items():
        s = owner(kdeg, servers)
        sdeg = servers[s] if servers[s] >= kdeg else servers[s] + 360
        c = HI_COLOR if k in highlight else K_COLOR
        pts = [xy(d, 0.92) for d in range(kdeg, int(sdeg) + 1, 2)]
        ax.plot([p[0] for p in pts[:-2]], [p[1] for p in pts[:-2]], color=c, lw=2)
        ax.add_patch(FancyArrowPatch(pts[-3], pts[-1], arrowstyle="-|>", mutation_scale=14, color=c))


def save(fig, name):
    fig.savefig(OUT / name, dpi=110, bbox_inches="tight")
    plt.close(fig)
    print("wrote", OUT / name)


def rehashing():
    rows = [(k, h, h % 4, h % 3, h % 5) for k, h in HASHES.items()]
    print("\n| Key | hash | % 4 | % 3 | % 5 |\n|---|---|---|---|---|")
    for k, h, a, b, c in rows:
        print(f"| {k} | {h} | s{a} | s{b} | s{c} |")
    moved3 = sum(a != b for _, _, a, b, _ in rows)
    moved5 = sum(a != c for _, _, a, _, c in rows)
    print(f"moved n=4->3: {moved3}/8, n=4->5: {moved5}/8\n")
    fig, ax = plt.subplots(figsize=(8, 3.6)); ax.axis("off")
    cells = [[k, f"{h:,}", f"s{a}", f"s{b}", f"s{c}"] for k, h, a, b, c in rows]
    t = ax.table(cellText=cells, colLabels=["key", "hash(key)", "n=4  (before)", "n=3  (s3 removed)", "n=5  (s4 added)"],
                 loc="center", cellLoc="center")
    t.scale(1, 1.4)
    for i, (_, _, a, b, c) in enumerate(rows, start=1):
        if b != a: t[i, 3].set_facecolor("#f8c4c4")
        if c != a: t[i, 4].set_facecolor("#f8c4c4")
    ax.set_title(f"server = hash(key) % n   ·   red = key now on a server without its data "
                 f"({moved3}/8 and {moved5}/8 move)", fontsize=10)
    save(fig, "rehashing.png")


def hash_space_to_ring():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 4.2), gridspec_kw={"width_ratios": [1.3, 1]})
    a1.axis("off"); a1.set_xlim(-0.1, 1.1); a1.set_ylim(-1, 1)
    a1.plot([0, 1], [0, 0], lw=4, color="#555")
    for x, lbl in [(0, "x0\n0"), (0.25, "x1"), (0.5, "x2 ..."), (1, "xn\n2^160 - 1")]:
        a1.plot(x, 0, "|", ms=18, mew=2, color="#555")
        a1.text(x, -0.25, lbl, ha="center", va="top", fontsize=11)
    a1.set_title("Hash space (SHA-1): a line from 0 to 2^160 - 1", fontsize=12)
    a1.annotate("", xy=(0.95, 0.35), xytext=(0.05, 0.35),
                arrowprops=dict(arrowstyle="<->", connectionstyle="arc3,rad=-0.4", color=HI_COLOR, lw=1.5))
    a1.text(0.5, 0.75, "join both ends", ha="center", color=HI_COLOR, fontsize=11)
    ring_axes(a2, "Hash ring")
    a2.plot(*xy(0), "o", ms=10, color=HI_COLOR)
    a2.text(*xy(0, 0.62), "x0 and xn\nmeet here", ha="center", fontsize=10, color=HI_COLOR)
    for deg, lbl in [(90, "x1 ..."), (180, "2^159"), (270, "...")]:
        a2.text(*xy(deg, 0.78), lbl, ha="center", va="center", fontsize=9, color="#555")
    save(fig, "hash_space_to_ring.png")


def single(name, title, **kw):
    fig, ax = plt.subplots(figsize=(5.5, 5.5))
    ring_axes(ax, title)
    draw_nodes(ax, SERVERS, KEYS if kw.get("keys") else {})
    if kw.get("lookup"):
        draw_lookup(ax, SERVERS, KEYS)
    save(fig, name)


def before_after(name, title_after, servers_after, highlight, new=(), crossed=()):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 5.5))
    ring_axes(a1, "Before")
    draw_nodes(a1, SERVERS, KEYS, highlight=highlight)
    draw_lookup(a1, SERVERS, KEYS, highlight=highlight)
    ring_axes(a2, title_after)
    draw_nodes(a2, servers_after, KEYS, highlight=highlight, new=new, crossed=crossed)
    draw_lookup(a2, servers_after, KEYS, highlight=highlight)
    save(fig, name)


def moved(servers_after):
    return {k for k, d in KEYS.items() if owner(d, SERVERS) != owner(d, servers_after)}


if __name__ == "__main__":
    rehashing()
    hash_space_to_ring()
    single("ring_servers.png", "Servers hashed onto the ring")
    single("ring_keys.png", "Servers and keys on the same ring", keys=True)
    single("ring_lookup.png", "Lookup: go clockwise to the first server", keys=True, lookup=True)
    added = {**SERVERS, NEW_SERVER[0]: NEW_SERVER[1]}
    before_after("ring_add_server.png", f"After adding {NEW_SERVER[0]}", added, moved(added), new={NEW_SERVER[0]})
    removed = {s: d for s, d in SERVERS.items() if s != REMOVED_SERVER}
    before_after("ring_remove_server.png", f"After removing {REMOVED_SERVER}", removed, moved(removed),
                 crossed={REMOVED_SERVER})
    for k, d in KEYS.items():
        print(k, owner(d, SERVERS), "| +s4:", owner(d, added), "| -s1:", owner(d, removed))
