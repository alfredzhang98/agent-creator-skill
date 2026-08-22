#!/usr/bin/env python3
"""Generate draw.io diagrams for the README from a compact spec.

Emits two files per diagram:

  docs/diagrams/<name>.drawio      mxGraph XML — open in draw.io, the VS Code
                                   extension, or next-ai-draw-io for further
                                   AI-assisted editing
  docs/diagrams/<name>.drawio.svg  SVG with the same XML embedded in its
                                   `content` attribute — GitHub renders it as
                                   an image, and draw.io can re-open it for
                                   editing without losing structure

Two files rather than one because they answer different needs: GitHub will not
render `.drawio`, and `.drawio.svg` is awkward to hand-edit. Both are generated
from the same spec, so they cannot drift.

    python3 tools/make_diagrams.py
"""
from __future__ import annotations

import html
import os
import urllib.parse
from dataclasses import dataclass, field

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "docs", "diagrams")

# Palette chosen to survive GitHub's light AND dark themes: mid-tone fills with
# dark text read acceptably on both, unlike pale fills that vanish on dark.
PALETTE = {
    "input":   ("#E3F2FD", "#1565C0"),
    "brain":   ("#E8EAF6", "#3949AB"),
    "act":     ("#E0F2F1", "#00796B"),
    "guard":   ("#FFEBEE", "#C62828"),
    "verify":  ("#F1F8E9", "#558B2F"),
    "state":   ("#FFF8E1", "#F9A825"),
    "meta":    ("#F3E5F5", "#7B1FA2"),
    "out":     ("#ECEFF1", "#455A64"),
    "plain":   ("#FFFFFF", "#616161"),
}

FONT = "Helvetica"


@dataclass
class Node:
    id: str
    label: str
    x: int
    y: int
    w: int = 190
    h: int = 52
    kind: str = "plain"
    shape: str = "box"      # box | round | hex | ellipse
    fontsize: int = 12


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""
    style: str = "solid"    # solid | dashed
    exit_: str | None = None
    entry: str | None = None


@dataclass
class Group:
    label: str
    x: int
    y: int
    w: int
    h: int
    color: str = "#9E9E9E"


@dataclass
class Diagram:
    name: str
    title: str
    width: int
    height: int
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    groups: list[Group] = field(default_factory=list)


def _node_style(n: Node) -> str:
    fill, stroke = PALETTE[n.kind]
    base = {
        "box": "rounded=0;whiteSpace=wrap;html=1;",
        "round": "rounded=1;arcSize=40;whiteSpace=wrap;html=1;",
        "hex": "shape=hexagon;perimeter=hexagonPerimeter2;whiteSpace=wrap;html=1;",
        "ellipse": "ellipse;whiteSpace=wrap;html=1;",
    }[n.shape]
    return (
        f"{base}fillColor={fill};strokeColor={stroke};strokeWidth=2;"
        f"fontColor=#1A1A1A;fontSize={n.fontsize};fontFamily={FONT};"
        f"verticalAlign=middle;align=center;spacing=4;"
    )


def _edge_style(e: Edge) -> str:
    s = (
        "edgeStyle=orthogonalEdgeStyle;rounded=1;html=1;jettySize=auto;"
        "orthogonalLoop=1;strokeWidth=2;strokeColor=#546E7A;"
        f"fontSize=10;fontFamily={FONT};fontColor=#37474F;labelBackgroundColor=#FFFFFF;"
    )
    if e.style == "dashed":
        s += "dashed=1;strokeColor=#90A4AE;"
    if e.exit_:
        x, y = e.exit_.split(",")
        s += f"exitX={x};exitY={y};exitDx=0;exitDy=0;"
    if e.entry:
        x, y = e.entry.split(",")
        s += f"entryX={x};entryY={y};entryDx=0;entryDy=0;"
    return s


def to_mxfile(d: Diagram) -> str:
    cells: list[str] = []
    for i, g in enumerate(d.groups):
        cells.append(
            f'<mxCell id="g{i}" value="{html.escape(g.label)}" '
            f'style="rounded=1;arcSize=6;fillColor=none;strokeColor={g.color};'
            f'dashed=1;strokeWidth=1;verticalAlign=top;align=left;spacingLeft=8;'
            f'spacingTop=4;fontSize=11;fontStyle=1;fontColor={g.color};'
            f'fontFamily={FONT};" vertex="1" parent="1">'
            f'<mxGeometry x="{g.x}" y="{g.y}" width="{g.w}" height="{g.h}" as="geometry"/>'
            f"</mxCell>"
        )
    for n in d.nodes:
        cells.append(
            f'<mxCell id="{n.id}" value="{html.escape(n.label)}" '
            f'style="{_node_style(n)}" vertex="1" parent="1">'
            f'<mxGeometry x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" as="geometry"/>'
            f"</mxCell>"
        )
    for i, e in enumerate(d.edges):
        cells.append(
            f'<mxCell id="e{i}" value="{html.escape(e.label)}" '
            f'style="{_edge_style(e)}" edge="1" parent="1" '
            f'source="{e.src}" target="{e.dst}">'
            f'<mxGeometry relative="1" as="geometry"/>'
            f"</mxCell>"
        )
    body = "".join(cells)
    return (
        f'<mxfile host="agent-creator-skill" type="device">'
        f'<diagram name="{html.escape(d.title)}" id="{d.name}">'
        f'<mxGraphModel dx="{d.width}" dy="{d.height}" grid="0" gridSize="10" '
        f'guides="1" tooltips="1" connect="1" arrows="1" fold="1" page="1" '
        f'pageScale="1" pageWidth="{d.width}" pageHeight="{d.height}" math="0" shadow="0">'
        f'<root><mxCell id="0"/><mxCell id="1" parent="0"/>{body}</root>'
        f"</mxGraphModel></diagram></mxfile>"
    )


def _wrap(text: str, width: int, fontsize: int) -> list[str]:
    """Greedy wrap using an average-glyph-width estimate."""
    per_char = fontsize * 0.55
    budget = max(1, int((width - 14) / per_char))
    lines, cur = [], ""
    for word in text.split():
        trial = f"{cur} {word}".strip()
        if len(trial) <= budget:
            cur = trial
        else:
            if cur:
                lines.append(cur)
            cur = word
    if cur:
        lines.append(cur)
    return lines


def to_svg(d: Diagram) -> str:
    """Hand-render the same spec as SVG, with the mxfile embedded for re-editing."""
    parts: list[str] = []

    for g in d.groups:
        parts.append(
            f'<rect x="{g.x}" y="{g.y}" width="{g.w}" height="{g.h}" rx="6" '
            f'fill="none" stroke="{g.color}" stroke-width="1" stroke-dasharray="4 3"/>'
            f'<text x="{g.x + 9}" y="{g.y + 16}" font-family="{FONT},sans-serif" '
            f'font-size="11" font-weight="600" fill="{g.color}">{html.escape(g.label)}</text>'
        )

    pos = {n.id: n for n in d.nodes}
    for e in d.edges:
        a, b = pos[e.src], pos[e.dst]
        ax, ay = a.x + a.w / 2, a.y + a.h / 2
        bx, by = b.x + b.w / 2, b.y + b.h / 2
        # Leave the source box on the dominant axis and enter the target likewise;
        # an orthogonal dogleg keeps the picture readable without a router.
        if abs(bx - ax) > abs(by - ay):
            ax = a.x + a.w if bx > ax else a.x
            bx2 = b.x if bx > ax else b.x + b.w
            mid = (ax + bx2) / 2
            path = f"M {ax} {ay} L {mid} {ay} L {mid} {by} L {bx2} {by}"
            lx, ly = mid, (ay + by) / 2 - 5
        else:
            ay = a.y + a.h if by > ay else a.y
            by2 = b.y if by > ay else b.y + b.h
            mid = (ay + by2) / 2
            path = f"M {ax} {ay} L {ax} {mid} L {bx} {mid} L {bx} {by2}"
            lx, ly = (ax + bx) / 2, mid - 5
        dash = ' stroke-dasharray="5 4"' if e.style == "dashed" else ""
        color = "#90A4AE" if e.style == "dashed" else "#546E7A"
        parts.append(
            f'<path d="{path}" fill="none" stroke="{color}" stroke-width="2"{dash} '
            f'marker-end="url(#arrow)"/>'
        )
        if e.label:
            w = len(e.label) * 5.6 + 8
            parts.append(
                f'<rect x="{lx - w/2:.0f}" y="{ly - 9:.0f}" width="{w:.0f}" height="14" '
                f'rx="3" fill="#FFFFFF" fill-opacity="0.92"/>'
                f'<text x="{lx:.0f}" y="{ly + 2:.0f}" text-anchor="middle" '
                f'font-family="{FONT},sans-serif" font-size="10" fill="#37474F">'
                f"{html.escape(e.label)}</text>"
            )

    for n in d.nodes:
        fill, stroke = PALETTE[n.kind]
        rx = 22 if n.shape == "round" else 3
        if n.shape == "ellipse":
            parts.append(
                f'<ellipse cx="{n.x + n.w/2}" cy="{n.y + n.h/2}" rx="{n.w/2}" '
                f'ry="{n.h/2}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
            )
        elif n.shape == "hex":
            i = n.h / 2
            pts = (f"{n.x+i},{n.y} {n.x+n.w-i},{n.y} {n.x+n.w},{n.y+n.h/2} "
                   f"{n.x+n.w-i},{n.y+n.h} {n.x+i},{n.y+n.h} {n.x},{n.y+n.h/2}")
            parts.append(f'<polygon points="{pts}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        else:
            parts.append(
                f'<rect x="{n.x}" y="{n.y}" width="{n.w}" height="{n.h}" rx="{rx}" '
                f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
            )
        lines = _wrap(n.label, n.w, n.fontsize)
        lh = n.fontsize + 3
        top = n.y + n.h / 2 - (len(lines) - 1) * lh / 2 + n.fontsize / 3
        for i, line in enumerate(lines):
            weight = "600" if i == 0 else "400"
            parts.append(
                f'<text x="{n.x + n.w/2}" y="{top + i*lh:.0f}" text-anchor="middle" '
                f'font-family="{FONT},sans-serif" font-size="{n.fontsize}" '
                f'font-weight="{weight}" fill="#1A1A1A">{html.escape(line)}</text>'
            )

    embedded = urllib.parse.quote(to_mxfile(d), safe="")
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" '
        f'xmlns:xlink="http://www.w3.org/1999/xlink" '
        f'width="{d.width}" height="{d.height}" '
        f'viewBox="0 0 {d.width} {d.height}" '
        f'content="{embedded}">'
        f"<defs><marker id=\"arrow\" viewBox=\"0 0 10 10\" refX=\"9\" refY=\"5\" "
        f'markerWidth="7" markerHeight="7" orient="auto-start-reverse">'
        f'<path d="M 0 1 L 9 5 L 0 9 z" fill="#546E7A"/></marker></defs>'
        f'<rect width="100%" height="100%" fill="#FFFFFF"/>'
        f'<title>{html.escape(d.title)}</title>'
        + "".join(parts)
        + "</svg>"
    )


def write(d: Diagram) -> None:
    os.makedirs(OUT, exist_ok=True)
    for ext, content in ((".drawio", to_mxfile(d)), (".drawio.svg", to_svg(d))):
        path = os.path.join(OUT, d.name + ext)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        print(f"  {os.path.relpath(path, ROOT)}  ({len(content):,} bytes)")


# ==========================================================================
# The diagrams
# ==========================================================================

def agent_architecture() -> Diagram:
    """What a complete agent built with this skill actually contains.

    Five strict columns, and one deliberate accuracy choice: the finish path
    leaves from the LOOP, not from the model's decision. The loop is what
    returns a named exit; the model only stops emitting tool calls. Drawing it
    that way also removes every edge crossing.
    """
    N, E, G = [], [], []

    # --- column 1: what is always in context -----------------------------
    ctx = [
        ("prompt", "System prompt", "ref 06 · cached prefix, dynamic tail", "brain"),
        ("skills", "Skills + tool schemas", "ref 11 · index → body → files", "meta"),
        ("memory", "Memory", "ref 14 · manifest → recall ≤5", "state"),
        ("plan", "Planner", "ref 15 · read-only mode, approval", "meta"),
    ]
    for i, (nid, title, sub, kind) in enumerate(ctx):
        N.append(Node(nid, f"{title}\n{sub}", 30, 66 + i * 74, 210, 58, kind, fontsize=11))
    G.append(Group("ALWAYS IN CONTEXT", 18, 44, 234, 300, "#7B1FA2"))
    N.append(Node("task", "Task / user turn", 30, 392, 210, 46, "input", "round"))

    # --- column 2: the model and the loop --------------------------------
    N += [
        Node("cost", "Cost meter\nref 07 · cap before + after", 300, 60, 190, 58, "guard", fontsize=11),
        Node("llm", "LLM / Provider\nref 05 · retries, fallback", 300, 150, 190, 58, "brain", fontsize=11),
        Node("decide", "tool calls?", 300, 252, 190, 46, "brain", "hex", 12),
        Node("loop", "Loop  ref 01\ntyped Stop / Continue", 300, 600, 190, 58, "brain", fontsize=11),
    ]

    # --- column 3: the gauntlet, as an actual sequence -------------------
    steps = [
        ("g1", "1 · schema"), ("g2", "2 · validate"), ("g3", "3 · strip reserved"),
        ("g4", "4 · backfill onto a clone"), ("g5", "5 · hooks   ref 12"),
        ("g6", "6 · permission   ref 13"), ("g7", "7 · execute"),
    ]
    for i, (nid, label) in enumerate(steps):
        kind = "guard" if i in (4, 5) else "act"
        N.append(Node(nid, label, 560, 250 + i * 40, 210, 32, kind, fontsize=11))
        if i:
            E.append(Edge(steps[i - 1][0], nid))
    G.append(Group("EVERY TOOL CALL  ref 02 · any failure returns as data", 548, 228, 234, 306, "#00796B"))

    # --- column 4: what comes back ---------------------------------------
    N += [
        Node("sandbox", "Sandbox\nref 04 · OS boundary", 840, 400, 190, 58, "guard", fontsize=11),
        Node("verify", "Verifier\nref 03 · gating or advisory", 840, 490, 190, 58, "verify", fontsize=11),
        Node("results", "Typed signals\ncapped · overflow to disk", 840, 580, 190, 58, "out", fontsize=11),
    ]

    # --- column 5: sinks --------------------------------------------------
    N += [
        Node("orch", "Orchestration\nref 09 · subagents, worktrees", 1090, 250, 190, 58, "meta", fontsize=11),
        Node("state", "State\nref 08 · transcript, staging", 1090, 580, 190, 58, "state", fontsize=11),
        Node("out", "Answer + trace", 1090, 680, 190, 46, "out", "round"),
    ]

    E += [
        Edge("task", "llm"),
        Edge("prompt", "llm", style="dashed"),
        Edge("memory", "llm", style="dashed"),
        Edge("plan", "loop", style="dashed"),
        Edge("cost", "llm", style="dashed"),
        Edge("orch", "g1", style="dashed"),
        Edge("llm", "decide"),
        Edge("decide", "g1", "yes"),
        Edge("decide", "loop", "no"),
        Edge("g7", "sandbox", "generated code"),
        Edge("g7", "verify"),
        Edge("verify", "results"),
        Edge("results", "state", style="dashed"),
        Edge("results", "loop"),
        Edge("loop", "llm", "next turn"),
        Edge("loop", "out", "named exit"),
    ]
    return Diagram("agent-architecture", "Complete agent architecture", 1320, 770, N, E, G)


def turn_lifecycle() -> Diagram:
    """One turn, end to end — what actually happens and what can refuse."""
    N, E = [], []
    y = 30
    step = 70
    col = 300
    seq = [
        ("s1", "Budget check\nturns · USD, before the call", "guard"),
        ("s2", "Context ladder\nresult budget → snip → microcompact\n→ collapse → autocompact", "state"),
        ("s3", "LLM call (streaming)\ntools begin executing as blocks arrive", "brain"),
        ("s4", "Typed error?\ncontext_too_long · output_truncated", "guard"),
        ("s5", "Tool calls?", "brain"),
        ("s6", "Gauntlet per call\nschema → validate → hooks → permission", "act"),
        ("s7", "Execute · verify · cap results", "act"),
        ("s8", "Append results + attachments", "out"),
    ]
    ids = []
    for i, (nid, label, kind) in enumerate(seq):
        shape = "hex" if "?" in label else "box"
        h = 62 if "\n" in label and label.count("\n") > 1 else 52
        N.append(Node(nid, label.replace("\n", " "), col, y + i * step, 300, h, kind, shape, 11))
        ids.append(nid)
    for a, b in zip(ids, ids[1:]):
        E.append(Edge(a, b))

    N += [
        Node("rec", "Recovery ladder\neach rung fires once, then surface", 660, y + 3 * step, 220, 62, "verify", fontsize=11),
        Node("stop", "Stop hooks\nmay block → another turn", 660, y + 4 * step, 220, 52, "guard", fontsize=11),
        Node("done", "Stop.COMPLETED\n+ 9 other named exits", 660, y + 5 * step + 10, 220, 52, "out", "round", 11),
        Node("loopback", "next turn", 40, y + 4 * step, 180, 46, "brain", "round"),
    ]
    E += [
        Edge("s4", "rec", "yes"),
        Edge("s5", "stop", "no"),
        Edge("stop", "done"),
        Edge("rec", "s1", "retry"),
        Edge("s8", "loopback"),
        Edge("loopback", "s1"),
    ]
    return Diagram("turn-lifecycle", "One turn, end to end", 940, y + 8 * step + 40, N, E)


def distillation() -> Diagram:
    """How knowledge enters the library."""
    N, E = [], []
    N += [
        Node("src", "Production agent\ncodebase", 40, 130, 170, 56, "input", "round"),
        Node("scope", "Scope subsystems\nexclude what teaches nothing", 250, 40, 190, 56, "meta", fontsize=11),
        Node("read", "Deep-read per subsystem\nmechanisms · decisions · constants", 250, 130, 190, 62, "brain", fontsize=11),
        Node("verify", "Verify every file:line\nanchors → citations.lock.json", 250, 226, 190, 62, "verify", fontsize=11),
        Node("refs", "References\n15 modules, one contract", 490, 40, 190, 56, "out", fontsize=11),
        Node("case", "Case study\n+ tool catalogue", 490, 130, 190, 56, "out", fontsize=11),
        Node("code", "agentkit\nworking code + selftest", 490, 220, 190, 56, "act", fontsize=11),
        Node("skill", "skills/agent-creator", 730, 130, 190, 56, "meta", "round"),
        Node("next", "your next agent", 730, 226, 190, 46, "input", "round"),
        Node("more", "next codebase\nsame pipeline", 40, 250, 170, 56, "plain", "round", 11),
    ]
    E += [
        Edge("src", "scope"), Edge("scope", "read"), Edge("read", "verify"),
        Edge("read", "refs"), Edge("read", "case"), Edge("read", "code"),
        Edge("refs", "skill"), Edge("case", "skill"), Edge("code", "skill"),
        Edge("skill", "next", "npx skills add"),
        Edge("more", "scope", style="dashed"),
    ]
    return Diagram("distillation", "How knowledge enters the library", 960, 320, N, E)


if __name__ == "__main__":
    print("generating draw.io diagrams:")
    for d in (agent_architecture(), turn_lifecycle(), distillation()):
        write(d)
