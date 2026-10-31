"""Render the phase 2 diagrams: a key-value store built on consistent hashing.
    pip install matplotlib
    python3 docs/diagram_kv.py   # writes docs/images/kv_*.png and prints example numbers
Reuses the style, colours and ring positions of diagram.py (s0..s3, k0..k3).
Ring positions are illustrative angles, not real SHA-1 values. The virtual node
load chart and the Merkle tree hashes are real SHA-1 calculations.
"""
import hashlib
import statistics

from diagram import (SERVERS, KEYS, NEW_SERVER, REMOVED_SERVER, S_COLOR, K_COLOR, HI_COLOR, NEW_COLOR,
                     plt, FancyArrowPatch, xy, owner, ring_axes, draw_nodes, draw_lookup, save)

GREY = "#999"
PHYS_COLOR = {"s0": S_COLOR, "s1": "#9467bd", "s2": "#8c564b", "s3": "#17becf"}

# Section 2: two physical servers, three virtual nodes each, interleaved on the ring.
VNODES = {"s0_0": 20, "s1_0": 80, "s0_1": 140, "s1_1": 200, "s0_2": 260, "s1_2": 320}
# Section 4: four physical servers, two vnodes each. Walking clockwise from k0 (60)
# meets s1_0, then s1_1 (same physical server), then s2_0, then s0_0.
REPL_VNODES = {"s3_1": 20, "s1_0": 80, "s1_1": 105, "s2_0": 150,
               "s0_0": 200, "s3_0": 240, "s2_1": 290, "s0_1": 330}
REPL_KEY = ("k0", KEYS["k0"])
N = 3


def phys(vnode):
    return vnode.split("_")[0]


def prev_pos(name, servers):
    """Position of the previous node counter-clockwise."""
    d = servers[name]
    return servers[min((s for s in servers if s != name), key=lambda s: (d - servers[s]) % 360)]


def arc(ax, start, end, color, r=1.0, lw=10, alpha=0.35):
    end = end if end > start else end + 360
    pts = [xy(d, r) for d in range(int(start), int(end) + 1)]
    ax.plot([p[0] for p in pts], [p[1] for p in pts], color=color, lw=lw, alpha=alpha,
            solid_capstyle="butt", zorder=1)


def draw_vnodes(ax, vnodes, colors=None, size=14):
    for name, deg in vnodes.items():
        c = (colors or {}).get(name, PHYS_COLOR[phys(name)])
        ax.plot(*xy(deg), "s", ms=size, color=c, zorder=3)
        ax.text(*xy(deg, 1.2), name, ha="center", va="center", fontsize=10, color=c, weight="bold")


def draw_keys(ax, keys, color=K_COLOR):
    for name, deg in keys.items():
        ax.plot(*xy(deg), "o", ms=11, color=color, zorder=3)
        ax.text(*xy(deg, 0.8), name, ha="center", va="center", fontsize=10, color=color)


def flow_axes(ax, title=None):
    ax.set_xlim(0, 1); ax.set_ylim(0, 1); ax.axis("off")
    if title:
        ax.set_title(title, fontsize=13)


def box(ax, x, y, text, fc="#e8f0fa", ec=S_COLOR, fs=10, **kw):
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, zorder=3,
            bbox=dict(boxstyle="round,pad=0.5", fc=fc, ec=ec, lw=1.5), **kw)


def arrow(ax, p, q, text=None, color="#555", rad=0.0, style="-|>", ls="-", off=(0, 0.03), fs=9):
    ax.add_patch(FancyArrowPatch(p, q, arrowstyle=style, mutation_scale=14, color=color, lw=1.5,
                                 linestyle=ls, connectionstyle=f"arc3,rad={rad}", zorder=2))
    if text:
        ax.text((p[0] + q[0]) / 2 + off[0], (p[1] + q[1]) / 2 + off[1], text, ha="center",
                fontsize=fs, color=color)


def sha1(s):
    return hashlib.sha1(s.encode()).hexdigest()


# ---------------------------------------------------------------- 1. two issues
def uneven():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 5.5))
    left = {s: d for s, d in SERVERS.items() if s != REMOVED_SERVER}
    ring_axes(a1, f"Uneven partitions after removing {REMOVED_SERVER}")
    for s in left:
        arc(a1, prev_pos(s, left), left[s], HI_COLOR if s == "s2" else S_COLOR)
    draw_nodes(a1, left, {}, crossed={REMOVED_SERVER})
    a1.text(0, -0.05, "s2 now owns\nhalf the ring", ha="center", color=HI_COLOR, fontsize=11)
    clustered = {"k0": 35, "k1": 55, "k2": 75, "k3": 95}
    ring_axes(a2, "Uneven keys: all four land on s1")
    draw_nodes(a2, SERVERS, clustered, highlight=set(clustered))
    draw_lookup(a2, SERVERS, clustered, highlight=set(clustered))
    a2.text(0, -0.05, "s0, s2, s3 store\nnothing", ha="center", color=HI_COLOR, fontsize=11)
    save(fig, "kv_uneven.png")


# ---------------------------------------------------------------- 2. virtual nodes
def vnode_spread(n_servers=10, counts=(1, 10, 50, 100, 200, 500)):
    space = 2 ** 160
    out = []
    for v in counts:
        pos = sorted((int(sha1(f"s{i}_{j}"), 16), f"s{i}") for i in range(n_servers) for j in range(v))
        share = {f"s{i}": 0 for i in range(n_servers)}
        for i, (p, s) in enumerate(pos):
            share[s] += (p - pos[i - 1][0]) % space      # range (previous node, this node]
        vals = [x / space for x in share.values()]
        out.append((v, 100 * statistics.pstdev(vals) / statistics.mean(vals)))
    return out


def virtual_nodes():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5.5), gridspec_kw={"width_ratios": [1, 1.1]})
    ring_axes(a1, "Virtual nodes: s0 and s1, three each")
    for v in VNODES:
        arc(a1, prev_pos(v, VNODES), VNODES[v], PHYS_COLOR[phys(v)])
    draw_vnodes(a1, VNODES)
    draw_keys(a1, KEYS)
    draw_lookup(a1, VNODES, KEYS)
    spread = vnode_spread()
    print("\nvnodes per server -> std dev of load (% of mean), 10 servers")
    for v, sd in spread:
        print(f"  {v:>4}: {sd:5.1f}%")
    a2.plot([v for v, _ in spread], [sd for _, sd in spread], "o-", color=S_COLOR, lw=2)
    for v, sd in spread:
        a2.annotate(f"{sd:.1f}%", (v, sd), textcoords="offset points", xytext=(6, 6), fontsize=9)
    a2.set_xscale("log"); a2.set_xlabel("virtual nodes per server (log scale)")
    a2.set_ylabel("std dev of load, % of mean"); a2.grid(alpha=0.3)
    a2.set_title("10 servers, real SHA-1 positions", fontsize=12)
    save(fig, "kv_virtual_nodes.png")


# ---------------------------------------------------------------- 3. affected keys
def affected(name, title, servers_after, rng, mover, new=(), crossed=()):
    fig, ax = plt.subplots(figsize=(6, 6))
    ring_axes(ax, title)
    arc(ax, rng[0], rng[1], HI_COLOR, lw=14)
    moved = {k for k, d in KEYS.items() if owner(d, SERVERS) != owner(d, servers_after)}
    draw_nodes(ax, servers_after, KEYS, highlight=moved, new=new, crossed=crossed)
    draw_lookup(ax, servers_after, KEYS, highlight=moved)
    ax.text(0, -0.05, f"red range:\nkeys move to {mover}", ha="center", color=HI_COLOR, fontsize=11)
    save(fig, name)


# ---------------------------------------------------------------- 4. replication
def replication():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(11, 5.5))
    k, kd = REPL_KEY
    ring_axes(a1, f"N = {N}: first {N} servers clockwise from {k}")
    reps = sorted(SERVERS, key=lambda s: (SERVERS[s] - kd) % 360)[:N]
    arc(a1, kd, SERVERS[reps[-1]], HI_COLOR, r=0.9, lw=4, alpha=0.6)
    draw_nodes(a1, SERVERS, {k: kd}, highlight={k})
    for i, s in enumerate(reps, 1):
        a1.text(*xy(SERVERS[s], 1.38), f"replica {i}", ha="center", fontsize=9, color=HI_COLOR)
    ring_axes(a2, "With vnodes: skip a physical server already chosen")
    walk, chosen, colors = sorted(REPL_VNODES, key=lambda v: (REPL_VNODES[v] - kd) % 360), [], {}
    for v in walk:
        if len(chosen) == N:
            colors[v] = "#ccc"
            continue
        ok = phys(v) not in [phys(c) for c in chosen]
        if ok:
            chosen.append(v)
        else:
            colors[v] = GREY
        a2.text(*xy(REPL_VNODES[v], 1.38), "pick" if ok else "skip", ha="center", fontsize=9,
                color=HI_COLOR if ok else GREY, weight="bold")
    draw_vnodes(a2, REPL_VNODES, colors=colors)
    draw_keys(a2, {k: kd}, color=HI_COLOR)
    arc(a2, kd, REPL_VNODES[chosen[-1]], HI_COLOR, r=0.9, lw=4, alpha=0.6)
    a2.text(0, -0.05, "replicas: " + ", ".join(phys(c) for c in chosen), ha="center", fontsize=11,
            color=HI_COLOR)
    print("replication walk from k0:", walk, "-> chosen", chosen)
    save(fig, "kv_replication.png")


# ---------------------------------------------------------------- 7. vector clocks
def vector_clocks():
    fig, ax = plt.subplots(figsize=(13, 4.8))
    flow_axes(ax, "Vector clocks: D2 is an ancestor of D3 and D4; D3 and D4 conflict")
    box(ax, 0.06, 0.5, "D1\n([Sx,1])")
    box(ax, 0.25, 0.5, "D2\n([Sx,2])")
    box(ax, 0.50, 0.82, "D3\n([Sx,2],[Sy,1])")
    box(ax, 0.50, 0.18, "D4\n([Sx,2],[Sz,1])")
    box(ax, 0.86, 0.5, "D5\n([Sx,3],[Sy,1],[Sz,1])", fc="#e6f4e6", ec=K_COLOR)
    arrow(ax, (0.10, 0.5), (0.205, 0.5), "write by Sx")
    arrow(ax, (0.29, 0.56), (0.42, 0.78), "write by Sy", off=(-0.05, 0.03))
    arrow(ax, (0.29, 0.44), (0.42, 0.22), "write by Sz", off=(-0.05, -0.06))
    arrow(ax, (0.58, 0.78), (0.76, 0.56), "", color=K_COLOR)
    arrow(ax, (0.58, 0.22), (0.76, 0.44), "", color=K_COLOR)
    ax.text(0.68, 0.5, "client reads both,\nreconciles,\nwrite by Sx", ha="center", va="center",
            fontsize=9, color=K_COLOR)
    arrow(ax, (0.50, 0.68), (0.50, 0.32), style="<|-|>", color=HI_COLOR)
    ax.text(0.515, 0.5, "conflict\n(siblings)", ha="left", va="center", fontsize=10, color=HI_COLOR)
    save(fig, "kv_vector_clocks.png")


# ---------------------------------------------------------------- 8. gossip
GOSSIP_T = 100
GOSSIP_TIMEOUT = 20
GOSSIP = [("s0", 1025, 100), ("s1", 1310, 98), ("s2", 980, 72), ("s3", 1199, 95)]


def gossip():
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5), gridspec_kw={"width_ratios": [1, 1.2]})
    flow_axes(a1, "s0 gossips its list to random nodes")
    pos = {"s0": (0.2, 0.8), "s1": (0.8, 0.8), "s2": (0.8, 0.2), "s3": (0.2, 0.2)}
    for n, (x, y) in pos.items():
        down = n == "s2"
        box(a1, x, y, n + ("\n(no new\nheartbeats)" if down else ""), fs=12,
            fc="#eee" if down else "#e8f0fa", ec=HI_COLOR if down else S_COLOR)
    arrow(a1, (0.27, 0.8), (0.73, 0.8), "heartbeats + list", color=S_COLOR)
    arrow(a1, (0.2, 0.72), (0.2, 0.28), "heartbeats + list", color=S_COLOR, off=(-0.0, 0.0))
    arrow(a1, (0.27, 0.25), (0.73, 0.75), "passes it on", color=GREY, ls="--", off=(0.08, -0.05))
    a2.axis("off")
    rows = []
    for n, hb, t in GOSSIP:
        status = "offline" if GOSSIP_T - t > GOSSIP_TIMEOUT else "online"
        rows.append([n, str(hb), str(t), f"{GOSSIP_T - t} s", status])
    tbl = a2.table(cellText=rows, colLabels=["member", "heartbeat\ncounter", "last updated\n(s0 time)",
                                             "since update", "status"], loc="center", cellLoc="center")
    tbl.scale(1, 2.0)
    for i, r in enumerate(rows, 1):
        if r[4] == "offline":
            for j in range(5):
                tbl[i, j].set_facecolor("#f8c4c4")
    a2.set_title(f"s0's membership list at t = {GOSSIP_T} s (timeout {GOSSIP_TIMEOUT} s)", fontsize=12)
    save(fig, "kv_gossip.png")


# ---------------------------------------------------------------- 9. hinted handoff
def hinted_handoff():
    fig, ax = plt.subplots(figsize=(6.5, 6.5))
    k, kd = REPL_KEY
    ring_axes(ax, f"Sloppy quorum: s2 is down, s0 stands in for {k}")
    healthy = {s: d for s, d in SERVERS.items() if s != "s2"}
    arc(ax, kd, SERVERS["s0"] + 360, HI_COLOR, r=0.9, lw=4, alpha=0.6)
    draw_nodes(ax, healthy, {k: kd}, highlight={k}, new={"s0"}, crossed={"s2"},
               crossed_label="down")
    ax.text(*xy(110, 1.42), "replica 1", ha="center", fontsize=9, color=HI_COLOR)
    ax.text(*xy(290, 1.42), "replica 2", ha="center", fontsize=9, color=HI_COLOR)
    ax.text(*xy(30, 1.45), "replica 3 (stand-in)\nhint: belongs to s2", ha="center", fontsize=9,
            color=NEW_COLOR)
    ax.add_patch(FancyArrowPatch(xy(28, 0.85), xy(192, 0.85), connectionstyle="arc3,rad=0.35",
                                 arrowstyle="-|>", mutation_scale=14, color=NEW_COLOR, ls="--", lw=1.5))
    ax.text(0.3, -0.25, "hand back when\ns2 is back", ha="center", fontsize=10, color=NEW_COLOR)
    save(fig, "kv_hinted_handoff.png")


# ---------------------------------------------------------------- 10. Merkle trees
def merkle_tree(values):
    """Keys 1..12 in 4 buckets of 3. Returns (leaf key hashes, bucket hashes, parents, root)."""
    keyh = {k: sha1(f"{k}:{v}") for k, v in values.items()}
    buckets = [sha1("".join(keyh[k] for k in range(3 * b + 1, 3 * b + 4))) for b in range(4)]
    parents = [sha1(buckets[0] + buckets[1]), sha1(buckets[2] + buckets[3])]
    return keyh, buckets, parents, sha1(parents[0] + parents[1])


def draw_merkle(ax, title, tree, other):
    flow_axes(ax, title)
    _, b, p, r = tree
    _, ob, op, orr = other
    red = lambda a, c: dict(fc="#f8c4c4", ec=HI_COLOR) if a != c else {}
    box(ax, 0.5, 0.88, f"root\n{r[:4]}", **red(r, orr))
    for i, x in enumerate((0.27, 0.73)):
        box(ax, x, 0.62, f"{p[i][:4]}", **red(p[i], op[i]))
        arrow(ax, (0.5, 0.82), (x, 0.68), style="-")
    for i, x in enumerate((0.13, 0.38, 0.62, 0.87)):
        box(ax, x, 0.36, f"bucket {i + 1}\n{b[i][:4]}", **red(b[i], ob[i]))
        arrow(ax, ((0.27, 0.73)[i // 2], 0.57), (x, 0.42), style="-")
        ax.text(x, 0.14, "keys\n" + ",".join(str(k) for k in range(3 * i + 1, 3 * i + 4)), ha="center",
                fontsize=9, color=K_COLOR)


def merkle():
    a = {k: f"v{k}" for k in range(1, 13)}
    b = {**a, 7: "v7-new"}                      # one key differs, in bucket 3
    ta, tb = merkle_tree(a), merkle_tree(b)
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(12, 5))
    draw_merkle(a1, "Server 1", ta, tb)
    draw_merkle(a2, "Server 2 (key 7 newer)", tb, ta)
    print("merkle roots:", ta[3][:4], tb[3][:4], "| buckets 1:", [h[:4] for h in ta[1]],
          "2:", [h[:4] for h in tb[1]])
    save(fig, "kv_merkle.png")


# ---------------------------------------------------------------- 12. coordinator
def coordinator():
    fig, ax = plt.subplots(figsize=(8, 6))
    ring_axes(ax, "Any node can coordinate: s0 handles put(k0)")
    ax.set_xlim(-2.4, 1.5)
    draw_nodes(ax, SERVERS, {}, new={"s0"})
    box(ax, -1.9, 1.0, "client", fs=11, fc="#e6f4e6", ec=K_COLOR)
    arrow(ax, (-1.6, 1.0), xy(20, 1.05), "1. put(k0)", color=K_COLOR, off=(0, 0.06))
    for s in ("s1", "s2", "s3"):
        ax.add_patch(FancyArrowPatch(xy(20, 0.95), xy(SERVERS[s], 0.95), arrowstyle="<|-|>",
                                     mutation_scale=12, color=NEW_COLOR, lw=1.5))
    ax.text(0, 0.0, "2. forward to the\nN = 3 replicas of k0\n(s1, s2, s3)\n3. reply to client\nafter W acks",
            ha="center", va="center", fontsize=10, color=NEW_COLOR)
    save(fig, "kv_coordinator.png")


# ---------------------------------------------------------------- 13. node
NODE_PARTS = [("Client API", "get(key) / put(key, value)"),
              ("Failure detection", "gossip heartbeats"),
              ("Conflict resolution", "vector clocks"),
              ("Failure repair", "hinted handoff, Merkle trees"),
              ("Replication", "copy to N servers"),
              ("Storage engine", "commit log, memtable, SSTables")]


def node():
    fig, ax = plt.subplots(figsize=(10, 5))
    flow_axes(ax)
    ax.add_patch(plt.Rectangle((0.02, 0.03), 0.96, 0.85, fill=False, ec=S_COLOR, lw=2))
    ax.text(0.5, 0.93, "A node (for example s0)", ha="center", fontsize=13, color=S_COLOR, weight="bold")
    for i, (name, line) in enumerate(NODE_PARTS):
        x, y = (0.18, 0.5, 0.82)[i % 3], (0.65, 0.25)[i // 3]
        box(ax, x, y, f"{name}\n{line}", fs=10)
    save(fig, "kv_node.png")


# ---------------------------------------------------------------- 14/15. write and read path
def zones(ax):
    ax.add_patch(plt.Rectangle((0.25, 0.55), 0.73, 0.4, fc="#fff6e6", ec="none"))
    ax.add_patch(plt.Rectangle((0.25, 0.03), 0.73, 0.47, fc="#eef2f7", ec="none"))
    ax.text(0.97, 0.91, "memory", ha="right", fontsize=11, color=NEW_COLOR)
    ax.text(0.97, 0.06, "disk", ha="right", fontsize=11, color=S_COLOR)


def write_path():
    fig, ax = plt.subplots(figsize=(10, 5))
    flow_axes(ax, "Write path")
    zones(ax)
    box(ax, 0.1, 0.5, "write\nrequest", fc="#e6f4e6", ec=K_COLOR)
    box(ax, 0.45, 0.28, "commit log\n(append only)")
    box(ax, 0.45, 0.75, "memtable\n(sorted, in memory)", ec=NEW_COLOR, fc="#fff")
    box(ax, 0.82, 0.28, "SSTable 1\nSSTable 2\nSSTable 3", fs=9)
    arrow(ax, (0.16, 0.46), (0.36, 0.3), "1", off=(0, 0.03))
    arrow(ax, (0.16, 0.54), (0.33, 0.73), "2", off=(0, 0.03))
    arrow(ax, (0.57, 0.72), (0.78, 0.38), "3. flush when full", off=(0.09, 0.03))
    save(fig, "kv_write_path.png")


def read_path():
    fig, ax = plt.subplots(figsize=(10, 5))
    flow_axes(ax, "Read path")
    zones(ax)
    box(ax, 0.1, 0.5, "read\nrequest", fc="#e6f4e6", ec=K_COLOR)
    box(ax, 0.45, 0.75, "memtable\nkey here? return it", ec=NEW_COLOR, fc="#fff")
    box(ax, 0.45, 0.28, "Bloom filters\nwhich SSTables\nmay hold the key?")
    for y, (t, hit) in zip((0.4, 0.27, 0.14), (("SSTable 1: no", False), ("SSTable 2: maybe", True),
                                              ("SSTable 3: no", False))):
        box(ax, 0.84, y, t, fs=9, fc="#f8c4c4" if hit else "#eee", ec=HI_COLOR if hit else GREY)
    arrow(ax, (0.16, 0.54), (0.32, 0.73), "1", off=(0, 0.03))
    arrow(ax, (0.45, 0.69), (0.45, 0.38), "2. not found", off=(0.07, 0))
    arrow(ax, (0.56, 0.28), (0.73, 0.27), "3. read", off=(0, 0.03))
    arrow(ax, (0.84, 0.08), (0.1, 0.42), "4. return result", color=K_COLOR, rad=-0.25, off=(0.05, -0.2))
    save(fig, "kv_read_path.png")


if __name__ == "__main__":
    uneven()
    virtual_nodes()
    added = {**SERVERS, NEW_SERVER[0]: NEW_SERVER[1]}
    affected("kv_affected_add.png", f"Add {NEW_SERVER[0]}: range s2 to s4 moves", added,
             (SERVERS["s2"], NEW_SERVER[1]), NEW_SERVER[0], new={NEW_SERVER[0]})
    removed = {s: d for s, d in SERVERS.items() if s != REMOVED_SERVER}
    affected("kv_affected_remove.png", f"Remove {REMOVED_SERVER}: range s0 to s1 moves", removed,
             (SERVERS["s0"], SERVERS[REMOVED_SERVER]), "s2", crossed={REMOVED_SERVER})
    replication()
    vector_clocks()
    gossip()
    hinted_handoff()
    merkle()
    coordinator()
    node()
    write_path()
    read_path()
