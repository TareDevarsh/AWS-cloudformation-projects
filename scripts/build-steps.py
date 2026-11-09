#!/usr/bin/env python3
"""Build step-by-step architecture visuals for a project from docs/steps.yaml.

Outputs (both derived from <project>/docs/steps.yaml):
  1. <project>/docs/images/architecture-steps.svg
     Animated SVG with real AWS icons. Each step fades in its resources and
     lists the CloudFormation resources it adds. Pure CSS animation, so it
     plays inside a GitHub README (<img> tags run CSS, not JS).
  2. The block between <!-- steps:start --> and <!-- steps:end --> in
     <project>/README.md: the SVG plus one <details> + Mermaid per step.

Usage: python3 scripts/build-steps.py 01-key-value-store [--check]
  --check  exit 1 if outputs are out of date (for CI), don't write.
Needs: pyyaml, diagrams (for the AWS icon PNGs). No graphviz needed.
"""
import base64
import html
import sys
from pathlib import Path

import yaml

START, END = "<!-- steps:start -->", "<!-- steps:end -->"
SVG_NAME = "architecture-steps.svg"

# ---------------------------------------------------------------- Mermaid

SHAPES = {"box": ('["', '"]'), "db": ('[("', '")]'), "round": ('("', '")')}
ARROWS = {"solid": "-->", "dotted": "-.->"}


def mermaid(spec: dict, n: int) -> str:
    """Mermaid flowchart for step n: done, new (step n), and future items."""
    lines = [f"flowchart {spec.get('direction', 'LR')}"]
    for node in spec["nodes"]:
        left, right = SHAPES[node.get("shape", "box")]
        lines.append(f"    {node['id']}{left}{node['label']}{right}")
    for edge in spec["edges"]:
        arrow = ARROWS[edge.get("style", "solid")]
        label = f"|{edge['label']}|" if edge.get("label") else ""
        lines.append(f"    {edge['from']} {arrow}{label} {edge['to']}")
    lines += [
        "    classDef new fill:#FFF4E5,stroke:#FF9900,stroke-width:3px,color:#232F3E",
        "    classDef future fill:#F7F7F7,stroke:#DDDDDD,color:#BBBBBB,stroke-dasharray:4 3",
    ]
    for cls, ids in (
        ("new", [x["id"] for x in spec["nodes"] if x["step"] == n]),
        ("future", [x["id"] for x in spec["nodes"] if x["step"] > n]),
    ):
        if ids:
            lines.append(f"    class {','.join(ids)} {cls}")
    future_edges = [str(i) for i, e in enumerate(spec["edges"]) if e["step"] > n]
    if future_edges:
        lines.append(f"    linkStyle {','.join(future_edges)} stroke:#DDDDDD,color:#CCCCCC")
    return "\n".join(lines)


# ---------------------------------------------------------------- SVG

W, H = 1000, 610
STEP_SECONDS = 3.0        # time each step is on screen
GHOST = 0.12              # opacity of not-yet-added resources
FADE = 1.5                # % of the cycle used for each fade
NODE_HW, NODE_HH = 78, 60  # half width/height of a node, for clipping arrows
PANEL_Y = 430             # top of the caption panel
SANS = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
MONO = "ui-monospace,SFMono-Regular,Menlo,Consolas,monospace"


def icon_root() -> Path:
    try:
        import diagrams
    except ImportError:
        sys.exit("Missing icons: pip install 'diagrams==0.24.4'")
    return Path(diagrams.__file__).resolve().parent.parent / "resources"


def esc(text: str) -> str:
    # Markdown `code` in captions becomes 'code' in the SVG.
    return html.escape(text.replace("`", "'"), quote=True)


def keyframes(spec: dict) -> tuple[str, float]:
    """CSS keyframes. a<k>: fade in at step k and stay. w<k>: show only during step k."""
    n = len(spec["steps"])
    total = STEP_SECONDS * (n + 1)  # +1 = final "done" frame

    def pct(step_index: int) -> float:
        return round(100 * step_index / (n + 1), 2)

    css = []
    for k in range(1, n + 1):
        a = pct(k - 1)
        if k == 1:
            frames = f"0%,97%{{opacity:1}}100%{{opacity:{GHOST}}}"
        else:
            frames = (f"0%,{a - FADE}%{{opacity:{GHOST}}}"
                      f"{a}%,97%{{opacity:1}}100%{{opacity:{GHOST}}}")
        css.append(f"@keyframes a{k}{{{frames}}}")
    for k in range(1, n + 2):
        a, b = pct(k - 1), pct(k)
        if k == 1:
            frames = f"0%,{b - FADE}%{{opacity:1}}{b}%,100%{{opacity:0}}"
        elif k == n + 1:
            frames = f"0%,{a - FADE}%{{opacity:0}}{a}%,97%{{opacity:1}}100%{{opacity:0}}"
        else:
            frames = (f"0%,{a - FADE}%{{opacity:0}}{a}%,{b - FADE}%{{opacity:1}}"
                      f"{b}%,100%{{opacity:0}}")
        css.append(f"@keyframes w{k}{{{frames}}}")
    for k in range(1, n + 1):
        css.append(f".a{k}{{animation:a{k} {total}s linear infinite}}")
    for k in range(1, n + 2):
        # Base opacity = the final frame, shown when animation is off.
        base = 1 if k == n + 1 else 0
        css.append(f".w{k}{{opacity:{base};animation:w{k} {total}s linear infinite}}")
    css.append("@media (prefers-reduced-motion:reduce){*{animation:none!important}}")
    return "\n".join(css), total


def clip(x1, y1, x2, y2):
    """Point where the line from (x1,y1) to (x2,y2) leaves the node box at (x1,y1)."""
    dx, dy = x2 - x1, y2 - y1
    t = min(NODE_HW / abs(dx) if dx else 1e9, NODE_HH / abs(dy) if dy else 1e9)
    return x1 + dx * t, y1 + dy * t


def svg(spec: dict) -> str:
    root = icon_root()
    n = len(spec["steps"])
    css, total = keyframes(spec)
    nodes = {x["id"]: x for x in spec["nodes"]}
    out = [
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
        f'role="img" aria-labelledby="t d">',
        f'<title id="t">{esc(spec["title"])}</title>',
        '<desc id="d">' + esc(" ".join(
            f"Step {i}: {s['label']}. {s['caption']}" for i, s in enumerate(spec["steps"], 1)
        )) + "</desc>",
        f"<style>\n{css}\n"
        f"text{{font-family:{SANS};fill:#232F3E}}"
        f".mono{{font-family:{MONO}}}.sub{{fill:#5F6B7A}}"
        f".edge{{stroke:#545B64;stroke-width:1.6;fill:none}}"
        f".dotted{{stroke-dasharray:5 4}}</style>",
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" '
        'markerHeight="7" orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" '
        'fill="#545B64"/></marker></defs>',
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="12" fill="#FFFFFF" '
        'stroke="#D0D7DE"/>',
        f'<text x="24" y="36" font-size="18" font-weight="700">{esc(spec["title"])}</text>',
    ]

    # Progress dots (top right): dot k fills when step k starts.
    for k in range(1, n + 1):
        cx = W - 24 - (n - k) * 22
        out.append(f'<circle cx="{cx}" cy="30" r="7" fill="none" stroke="#FF9900" stroke-width="1.5"/>')
        out.append(f'<circle class="a{k}" cx="{cx}" cy="30" r="7" fill="#FF9900"/>')

    # Edges first so icons sit on top.
    for e in spec["edges"]:
        a, b = nodes[e["from"]], nodes[e["to"]]
        x1, y1 = clip(a["x"], a["y"], b["x"], b["y"])
        x2, y2 = clip(b["x"], b["y"], a["x"], a["y"])
        dotted = " dotted" if e.get("style") == "dotted" else ""
        out.append(f'<g class="a{e["step"]}">')
        out.append(f'<line class="edge{dotted}" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" '
                   f'y2="{y2:.1f}" marker-end="url(#arrow)"/>')
        if e.get("label"):
            # Place the label beside the line (above, or left if vertical), not on it.
            length = ((x2 - x1) ** 2 + (y2 - y1) ** 2) ** 0.5
            nx, ny = (y2 - y1) / length, -(x2 - x1) / length
            if ny > 0 or (ny == 0 and nx > 0):
                nx, ny = -nx, -ny
            half = 3.4 * len(e["label"])
            off = 9 + abs(nx) * half
            mx, my = (x1 + x2) / 2 + nx * off, (y1 + y2) / 2 + ny * off
            out.append(f'<text class="mono sub" x="{mx:.1f}" y="{my + 4:.1f}" font-size="11" '
                       'text-anchor="middle" stroke="#FFFFFF" stroke-width="4" '
                       f'paint-order="stroke">{esc(e["label"])}</text>')
        out.append("</g>")

    # Nodes: highlight ring (only during their step), icon, label lines.
    for node in spec["nodes"]:
        x, y, k = node["x"], node["y"], node["step"]
        out.append(f'<rect class="w{k}" x="{x - NODE_HW}" y="{y - NODE_HH + 4}" '
                   f'width="{2 * NODE_HW}" height="{2 * NODE_HH}" rx="12" fill="#FFF4E5" '
                   'stroke="#FF9900" stroke-width="2.5"/>')
        png = base64.b64encode((root / node["icon"]).read_bytes()).decode()
        out.append(f'<g class="a{k}">')
        out.append(f'<image x="{x - 28}" y="{y - 52}" width="56" height="56" '
                   f'href="data:image/png;base64,{png}"/>')
        for i, line in enumerate(node["label"].split("<br/>")):
            weight, size, cls = ("700", 14, "") if i == 0 else ("400", 12, ' class="sub"')
            out.append(f'<text{cls} x="{x}" y="{y + 22 + i * 16}" font-size="{size}" '
                       f'font-weight="{weight}" text-anchor="middle">{esc(line)}</text>')
        out.append("</g>")

    # Caption panel: one group per step, plus the final frame.
    out.append(f'<line x1="24" y1="{PANEL_Y}" x2="{W - 24}" y2="{PANEL_Y}" stroke="#D0D7DE"/>')
    frames = [(f"Step {i}/{n}: {s['label']}", s["caption"], s.get("adds", []))
              for i, s in enumerate(spec["steps"], 1)]
    frames.append((spec["final"]["label"], spec["final"]["caption"], []))
    for k, (title, caption, adds) in enumerate(frames, 1):
        out.append(f'<g class="w{k}">')
        out.append(f'<text x="24" y="{PANEL_Y + 32}" font-size="17" font-weight="700">'
                   f'{esc(title)}</text>')
        out.append(f'<text class="sub" x="24" y="{PANEL_Y + 56}" font-size="14">{esc(caption)}</text>')
        for i, add in enumerate(adds):
            out.append(f'<text class="mono" x="24" y="{PANEL_Y + 84 + i * 18}" font-size="12.5" '
                       f'xml:space="preserve"><tspan fill="#1A7F37" font-weight="700">+ </tspan>'
                       f'{esc(add)}</text>')
        out.append("</g>")
    out.append("</svg>")
    return "\n".join(out) + "\n"


# ---------------------------------------------------------------- README

def section(spec: dict) -> str:
    steps = spec["steps"]
    alt = f"Animated diagram: the stack built in {len(steps)} steps: " + ", ".join(
        s["label"] for s in steps)
    out = [
        START,
        "<!-- Generated by scripts/build-steps.py from docs/steps.yaml. Edit the YAML, not this block. -->",
        "",
        "### Build it step by step",
        "",
        f"![{alt}](docs/images/{SVG_NAME})",
        "",
        "Each step shows what gets added and which CloudFormation resources it creates. "
        "To go at your own pace, open a step below. 🟧 = added in this step, grey = comes later.",
        "",
    ]
    for n, step in enumerate(steps, start=1):
        out += [
            "<details>",
            f"<summary><b>Step {n}/{len(steps)}: {step['label']}</b></summary>",
            "",
            step["caption"],
            "",
        ]
        if step.get("adds"):
            out += ["CloudFormation resources added:", "", "```text"]
            out += [f"+ {a}" for a in step["adds"]]
            out += ["```", ""]
        out += ["```mermaid", mermaid(spec, n), "```", "", "</details>", ""]
    out.append(END)
    return "\n".join(out)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if len(args) != 1:
        print(__doc__)
        return 2
    check = "--check" in sys.argv
    project = Path(args[0])
    spec = yaml.safe_load((project / "docs" / "steps.yaml").read_text())

    readme = project / "README.md"
    text = readme.read_text()
    if START not in text or END not in text:
        print(f"{readme}: add {START} and {END} markers where the section goes")
        return 1
    head, rest = text.split(START, 1)
    _, tail = rest.split(END, 1)
    outputs = {
        readme: head + section(spec) + tail,
        project / "docs" / "images" / SVG_NAME: svg(spec),
    }

    stale = [p for p, body in outputs.items() if not p.exists() or p.read_text() != body]
    if check:
        for p in stale:
            print(f"{p} is stale. Run: python3 scripts/build-steps.py {project}")
        return 1 if stale else 0
    for p in stale:
        p.write_text(outputs[p])
        print(f"Wrote {p}")
    if not stale:
        print("Up to date.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
