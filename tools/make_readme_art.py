"""Generate the animated SVG artwork used in the README (no third-party packages needed).

    python tools/make_readme_art.py

Writes docs/assets/hero-light.svg, hero-dark.svg and cell-cycle.svg. The cell animation is a
schematic top view of the real three-arm cell: arm motion, conveyor travel, proximity sensors,
DB1 signals and step names follow the timeline the robot controller and PLC actually run.
"""

import math
from pathlib import Path

OUT = Path(__file__).resolve().parents[1] / "docs" / "assets"

FONT = "'Segoe UI', 'Helvetica Neue', Helvetica, Arial, sans-serif"
MONO = "'Cascadia Mono', 'SF Mono', Menlo, Consolas, 'Liberation Mono', monospace"

NAVY, PETROL, MINT, CYAN, SAND = "#000028", "#009999", "#00ffb9", "#00e6dc", "#f3f3f0"

# ---------------------------------------------------------------------------
# Cell animation
# ---------------------------------------------------------------------------

T = 16.0      # loop length in seconds
FPS = 25
N = int(T * FPS)
L1 = L2 = 105  # link lengths (px)

HOLDER_TRAY, PCB_TRAY = (95, 100), (520, 100)
A, B, C, D = (185, 190), (430, 190), (430, 400), (185, 400)
P = (610, 270)  # hand-over point where arms 2 and 3 mount the PCB

ARMS = {
    1: {"base": (85, 300), "side": -1, "home": (150, 300), "keys": [
        (0.0, "home"), (0.5, "home"), (1.9, HOLDER_TRAY), (2.3, HOLDER_TRAY), (3.9, A), (4.3, A), (5.4, "home")],
        "tool": [(2.1, 4.2)]},
    2: {"base": (530, 300), "side": 1, "home": (470, 300), "keys": [
        (0.0, "home"), (6.7, "home"), (7.7, B), (8.1, B), (9.2, P), (11.8, P), (13.0, C), (13.4, C), (14.4, "home")],
        "tool": [(7.9, 13.3)]},
    3: {"base": (610, 130), "side": 1, "home": (600, 200), "keys": [
        (0.0, "home"), (9.1, "home"), (9.9, PCB_TRAY), (10.2, PCB_TRAY), (11.1, P), (11.4, P), (12.4, "home")],
        "tool": [(10.1, 11.3)]},
}

STEPS = {
    1: [(0.5, "MOVING_TO_ORIGIN", 10), (1.0, "PICKUP_POSITION", 20), (2.0, "ACTIVATING_PUMP", 30), (2.2, "PICKING_UP", 40),
        (2.5, "LIFTING", 50), (3.0, "TO_CONVEYOR", 70), (3.8, "PLACING", 85), (4.1, "RELEASING", 95),
        (4.5, "RETURNING", 98), (5.4, "IDLE", 0)],
    2: [(6.8, "INITIAL_POS", 15), (7.2, "PICKUP_POINT", 25), (7.8, "GRIPPING_OBJECT", 40), (8.4, "LIFTING", 55),
        (8.7, "TO_ASSEMBLY", 70), (9.5, "WAITING_ARM3", 85), (11.0, "ASSEMBLING", 95), (12.0, "TO_CONVEYOR2", 75),
        (12.8, "PLACING", 85), (13.2, "RELEASING", 95), (13.5, "RETURNING", 98), (14.4, "IDLE", 0)],
    3: [(9.2, "PCB_PICKUP", 15), (9.8, "PICKING_PCB", 30), (10.4, "TO_ASSEMBLY", 50), (10.9, "WAITING_ARM2", 70),
        (11.1, "ASSEMBLING", 85), (11.5, "TO_STORAGE", 95), (11.9, "STORING", 98), (12.4, "IDLE", 0)],
}

CONVEYOR_RUNS = {1: (4.6, 6.6), 2: (13.6, 15.4)}

CAPTIONS = [
    (0.0, "Start · PLC sets Arm_1 (DB1.DBX10.0)"),
    (0.6, "Arm 1 picks a PCB holder with its vacuum pump"),
    (4.4, "Prox 1 detects the holder → conveyor 1 runs"),
    (6.7, "Prox 2 at point B → Arm 2 grips the holder"),
    (9.1, "Arm 2 reaches its trigger position → Arm 3 brings the PCB"),
    (11.0, "Arms 2 + 3 mount the PCB on the holder"),
    (12.1, "Arm 2 places the assembly on conveyor 2 (Prox 3)"),
    (13.7, "Conveyor 2 carries the board to the exit"),
    (15.4, "Prox 4 · cycle complete → Total_Prod + 1"),
]


def smooth(u):
    u = min(max(u, 0.0), 1.0)
    return u * u * (3 - 2 * u)


def polar(base, point):
    dx, dy = point[0] - base[0], point[1] - base[1]
    return math.atan2(dy, dx), math.hypot(dx, dy)


def arm_target(arm, t):
    """Interpolate the end-effector target in polar coordinates around the base (looks like a rotating arm)."""
    spec = ARMS[arm]
    keys = [(kt, spec["home"] if p == "home" else p) for kt, p in spec["keys"]]
    if t >= keys[-1][0]:
        return keys[-1][1]
    for (t0, p0), (t1, p1) in zip(keys, keys[1:]):
        if t0 <= t <= t1:
            u = smooth((t - t0) / (t1 - t0)) if t1 > t0 else 1.0
            a0, r0 = polar(spec["base"], p0)
            a1, r1 = polar(spec["base"], p1)
            da = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
            a, r = a0 + da * u, r0 + (r1 - r0) * u
            return spec["base"][0] + r * math.cos(a), spec["base"][1] + r * math.sin(a)
    return keys[0][1]


def ik(arm, target):
    """Two-link inverse kinematics: absolute angle of link 1 and relative angle of link 2 (degrees)."""
    bx, by = ARMS[arm]["base"]
    dx, dy = target[0] - bx, target[1] - by
    d = min(max(math.hypot(dx, dy), 25.0), L1 + L2 - 1)
    phi = math.atan2(dy, dx)
    alpha = math.acos((L1 ** 2 + d ** 2 - L2 ** 2) / (2 * L1 * d))
    th1 = phi + ARMS[arm]["side"] * alpha
    ex, ey = L1 * math.cos(th1), L1 * math.sin(th1)
    th_abs = math.atan2(dy - ey, dx - ex)
    return math.degrees(th1), math.degrees(th_abs - th1)


def tool_on(arm, t):
    return any(a <= t < b for a, b in ARMS[arm]["tool"])


def lerp(p, q, u):
    return p[0] + (q[0] - p[0]) * u, p[1] + (q[1] - p[1]) * u


def holder_state(t):
    """Position + visibility of the moving holder (becomes the finished assembly after 11.3 s)."""
    if t < 2.1:
        return None
    if t < 4.2:
        return arm_target(1, t)
    if t < 4.6:
        return A
    if t < 6.6:
        return lerp(A, B, (t - 4.6) / 2.0)
    if t < 7.9:
        return B
    if t < 13.3:
        return arm_target(2, t)
    if t < 13.6:
        return C
    if t < 15.4:
        return lerp(C, D, (t - 13.6) / 1.8)
    return D


def pcb_state(t):
    if t < 10.1:
        return None
    if t < 11.3:
        return arm_target(3, t)
    return holder_state(t)


def holder_opacity(t):
    if holder_state(t) is None:
        return 0.0
    if t > 15.55:
        return max(0.0, 1 - (t - 15.55) / 0.35)
    return 1.0


SENSORS = {
    1: {"pos": (215, 160), "on": [(4.3, 5.2)]},
    2: {"pos": (430, 160), "on": [(6.6, 8.2)]},
    3: {"pos": (430, 430), "on": [(13.3, 14.0)]},
    4: {"pos": (185, 430), "on": [(15.4, 16.0)]},
}


def is_on(intervals, t):
    return any(a <= t < b for a, b in intervals)


def fmt(v):
    return f"{v:.1f}".rstrip("0").rstrip(".") if isinstance(v, float) else str(v)


def anim(attr, values, discrete=False, extra=""):
    mode = ' calcMode="discrete"' if discrete else ""
    return (f'<animate attributeName="{attr}" dur="{T:g}s" repeatCount="indefinite"{mode} '
            f'values="{";".join(values)}"{extra}/>')


def rotate_anim(values):
    return (f'<animateTransform attributeName="transform" type="rotate" dur="{T:g}s" repeatCount="indefinite" '
            f'values="{";".join(values)}"/>')


def translate_anim(values):
    return (f'<animateTransform attributeName="transform" type="translate" dur="{T:g}s" repeatCount="indefinite" '
            f'values="{";".join(values)}"/>')


def unwrap(degrees):
    out = [degrees[0]]
    for d in degrees[1:]:
        prev = out[-1]
        d += 360 * round((prev - d) / 360)
        out.append(d)
    return out


def interval_visibility(start, end):
    """Discrete opacity animation that shows an element between start and end (seconds)."""
    times, values = [0.0], ["0"]
    if start <= 0:
        values = ["1"]
    else:
        times.append(start / T)
        values.append("1")
    if end < T:
        times.append(end / T)
        values.append("0")
    key_times = ";".join(f"{x:.4f}" for x in times)
    return anim("opacity", values, discrete=True, extra=f' keyTimes="{key_times}"')


def sampled(fn):
    return [fn(i * T / N) for i in range(N + 1)]


def build_cell():
    W, H = 1000, 560
    s = []
    s.append(f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" '
             f'font-family="{FONT}" role="img" aria-label="Animated schematic of one assembly cycle">')
    s.append("<title>One assembly cycle: three uArm Swift Pro robots, two conveyors, four proximity sensors</title>")
    s.append(f"""<defs>
  <pattern id="dots" width="18" height="18" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#ffffff" fill-opacity=".06"/></pattern>
  <linearGradient id="bold" x1="0" x2="1"><stop offset="0" stop-color="{MINT}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>
  <filter id="glow" x="-80%" y="-80%" width="260%" height="260%"><feGaussianBlur stdDeviation="3.2"/></filter>
</defs>""")
    s.append(f'<rect width="{W}" height="{H}" rx="22" fill="{NAVY}"/>')
    s.append(f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="21" fill="url(#dots)" stroke="#26265a"/>')
    s.append(f'<rect x="0" y="{H - 4}" width="{W}" height="4" fill="url(#bold)"/>')
    s.append(f'<line x1="668" y1="24" x2="668" y2="{H - 24}" stroke="#26265a"/>')
    s.append(f'<text x="40" y="50" font-family="{MONO}" font-size="12.5" fill="{MINT}" letter-spacing="1.6">ONE ASSEMBLY CYCLE · TOP VIEW</text>')
    s.append(f'<text x="644" y="544" text-anchor="end" font-family="{MONO}" font-size="10.5" fill="#66667e">schematic · not to scale</text>')

    # trays
    for (cx, cy), label, kind in [(HOLDER_TRAY, "HOLDERS", "holder"), (PCB_TRAY, "PCBs", "pcb")]:
        s.append(f'<rect x="{cx - 44}" y="{cy - 26}" width="88" height="52" rx="10" fill="#0e0e38" stroke="#37376b" stroke-dasharray="4 4"/>')
        s.append(f'<text x="{cx}" y="{cy + 42}" text-anchor="middle" font-family="{MONO}" font-size="10" fill="#9999b3" letter-spacing="1">{label}</text>')
        for off in (-20, 20):
            s.append(part_svg(kind, cx + off, cy, ghost=True))

    # conveyors
    for idx, (y, label) in enumerate([(190, "CONVEYOR 1  →"), (400, "←  CONVEYOR 2")], start=1):
        s.append(f'<rect x="150" y="{y - 22}" width="310" height="44" rx="22" fill="#13133f" stroke="#37376b"/>')
        s.append(f'<circle cx="172" cy="{y}" r="12" fill="#1d1d4b" stroke="#37376b"/><circle cx="438" cy="{y}" r="12" fill="#1d1d4b" stroke="#37376b"/>')
        start, end = CONVEYOR_RUNS[idx]
        direction = -1 if idx == 1 else 1

        def offset(t, start=start, end=end, direction=direction):
            travel = 0.0
            if t > start:
                travel = (min(t, end) - start) * 122.0
            return fmt(round(direction * travel, 1))

        s.append(f'<line x1="172" y1="{y}" x2="438" y2="{y}" stroke="#4c4c74" stroke-width="18" stroke-dasharray="3 9">'
                 + anim("stroke-dashoffset", sampled(offset)) + "</line>")
        s.append(f'<text x="305" y="{y + 40 if idx == 1 else y - 32}" text-anchor="middle" font-family="{MONO}" font-size="10.5" fill="#66667e" letter-spacing="1.2">{label}</text>')

    # sensors
    for sid, spec in SENSORS.items():
        x, y = spec["pos"]
        fills = ";".join(MINT if is_on(spec["on"], i * T / N) else "#1d1d4b" for i in range(N + 1))
        glow = ";".join("0.9" if is_on(spec["on"], i * T / N) else "0" for i in range(N + 1))
        s.append(f'<circle cx="{x}" cy="{y}" r="11" fill="{MINT}" filter="url(#glow)" opacity="0">'
                 f'<animate attributeName="opacity" dur="{T:g}s" repeatCount="indefinite" calcMode="discrete" values="{glow}"/></circle>')
        s.append(f'<circle cx="{x}" cy="{y}" r="6.5" stroke="#9999b3" stroke-width="1.5" fill="#1d1d4b">'
                 f'<animate attributeName="fill" dur="{T:g}s" repeatCount="indefinite" calcMode="discrete" values="{fills}"/></circle>')
        lx = x + 12 if sid in (1, 4) else x - 12
        anchor = "start" if sid in (1, 4) else "end"
        ly = y - 8 if sid in (1, 2) else y + 16
        s.append(f'<text x="{lx}" y="{ly}" text-anchor="{anchor}" font-family="{MONO}" font-size="10.5" fill="#d1d1de">S{sid}</text>')

    # moving parts (drawn under the arms)
    holder_xy = sampled(lambda t: holder_state(t) or HOLDER_TRAY)
    pcb_xy = sampled(lambda t: pcb_state(t) or PCB_TRAY)
    s.append('<g>' + translate_anim([f"{x:.1f},{y:.1f}" for x, y in holder_xy])
             + anim("opacity", [f"{holder_opacity(i * T / N):.2f}" for i in range(N + 1)])
             + part_svg("holder", 0, 0) + "</g>")
    pcb_op = [f"{(0 if pcb_state(i * T / N) is None else holder_opacity(i * T / N) if i * T / N > 11.3 else 1):.2f}" for i in range(N + 1)]
    s.append('<g>' + translate_anim([f"{x:.1f},{y:.1f}" for x, y in pcb_xy]) + anim("opacity", pcb_op)
             + part_svg("pcb", 0, 0) + "</g>")

    # arms
    for arm, spec in ARMS.items():
        bx, by = spec["base"]
        angles = sampled(lambda t, arm=arm: ik(arm, arm_target(arm, t)))
        a1 = unwrap([a for a, _ in angles])
        a2 = unwrap([b for _, b in angles])
        tool = ";".join(MINT if tool_on(arm, i * T / N) else "#33334e" for i in range(N + 1))
        s.append(f'<g transform="translate({bx},{by})">')
        s.append('<g>' + rotate_anim([f"{a:.1f}" for a in a1]))
        s.append(f'<line x1="0" y1="0" x2="{L1}" y2="0" stroke="{NAVY}" stroke-width="18" stroke-linecap="round"/>')
        s.append(f'<line x1="0" y1="0" x2="{L1}" y2="0" stroke="#c9cad6" stroke-width="12" stroke-linecap="round"/>')
        s.append(f'<g transform="translate({L1},0)"><g>' + rotate_anim([f"{b:.1f}" for b in a2]))
        s.append(f'<line x1="0" y1="0" x2="{L2}" y2="0" stroke="{NAVY}" stroke-width="15" stroke-linecap="round"/>')
        s.append(f'<line x1="0" y1="0" x2="{L2}" y2="0" stroke="#e6e6ee" stroke-width="9" stroke-linecap="round"/>')
        s.append(f'<circle cx="{L2}" cy="0" r="9" stroke="{NAVY}" stroke-width="2.5" fill="#33334e">'
                 f'<animate attributeName="fill" dur="{T:g}s" repeatCount="indefinite" calcMode="discrete" values="{tool}"/></circle>')
        s.append(f'</g><circle r="7" fill="{PETROL}" stroke="{NAVY}" stroke-width="2"/></g>')
        s.append("</g>")
        s.append(f'<circle r="22" fill="#1d1d4b" stroke="{PETROL}" stroke-width="2.5"/><circle r="8" fill="{PETROL}"/>')
        s.append(f'<text y="{40 if arm != 3 else -30}" text-anchor="middle" font-family="{MONO}" font-size="11" fill="#d1d1de" letter-spacing="1">ARM {arm}</text>')
        s.append("</g>")

    # captions
    for i, (start, text) in enumerate(CAPTIONS):
        end = CAPTIONS[i + 1][0] if i + 1 < len(CAPTIONS) else T
        s.append(f'<text x="345" y="516" text-anchor="middle" font-size="14" fill="#ffffff" opacity="0">{text}'
                 + interval_visibility(start, end) + "</text>")

    # signal panel
    px = 690
    s.append(f'<text x="{px}" y="50" font-family="{MONO}" font-size="12.5" fill="{MINT}" letter-spacing="1.6">S7-1500 · DB1 SIGNALS</text>')
    rows = [
        ("Start", "DBX0.0", [(0.0, T)]),
        ("Prox 1", "DBX10.3", SENSORS[1]["on"]),
        ("Prox 2", "DBX10.4", SENSORS[2]["on"]),
        ("Prox 3", "DBX10.5", SENSORS[3]["on"]),
        ("Prox 4", "DBX10.6", SENSORS[4]["on"]),
        ("Conveyor 1", "DBX4.0", [CONVEYOR_RUNS[1]]),
        ("Conveyor 2", "DBX4.1", [CONVEYOR_RUNS[2]]),
    ]
    for r, (label, addr, intervals) in enumerate(rows):
        y = 84 + r * 27
        fills = ";".join(MINT if is_on(intervals, i * T / N) else "#33334e" for i in range(N + 1))
        s.append(f'<text x="{px}" y="{y}" font-size="13.5" fill="#ffffff">{label}</text>')
        s.append(f'<text x="{px + 104}" y="{y}" font-family="{MONO}" font-size="11.5" fill="#9999b3">{addr}</text>')
        s.append(f'<circle cx="{px + 266}" cy="{y - 4.5}" r="5.5" fill="#33334e">'
                 f'<animate attributeName="fill" dur="{T:g}s" repeatCount="indefinite" calcMode="discrete" values="{fills}"/></circle>')
    y = 84 + len(rows) * 27
    s.append(f'<text x="{px}" y="{y}" font-size="13.5" fill="#ffffff">Total_Prod</text>')
    s.append(f'<text x="{px + 104}" y="{y}" font-family="{MONO}" font-size="11.5" fill="#9999b3">DBW2</text>')
    s.append(f'<text x="{px + 271}" y="{y}" text-anchor="end" font-family="{MONO}" font-size="14" font-weight="700" fill="#ffffff">41{interval_visibility(0, 15.4)}</text>')
    s.append(f'<text x="{px + 271}" y="{y}" text-anchor="end" font-family="{MONO}" font-size="14" font-weight="700" fill="{MINT}" opacity="0">42{interval_visibility(15.4, T)}</text>')

    s.append(f'<line x1="{px}" y1="{y + 18}" x2="{px + 272}" y2="{y + 18}" stroke="#26265a"/>')
    s.append(f'<text x="{px}" y="{y + 46}" font-family="{MONO}" font-size="12.5" fill="{MINT}" letter-spacing="1.6">LIVE STEP TRACKING</text>')
    top = y + 76
    for arm in (1, 2, 3):
        ay = top + (arm - 1) * 64
        steps = [(0.0, "IDLE", 0)] + STEPS[arm]
        s.append(f'<text x="{px}" y="{ay}" font-size="13.5" font-weight="700" fill="#ffffff">Arm {arm}</text>')
        for k, (start, name, pct) in enumerate(steps):
            end = steps[k + 1][0] if k + 1 < len(steps) else T
            color = "#9999b3" if name == "IDLE" else MINT
            s.append(f'<text x="{px}" y="{ay + 18}" font-family="{MONO}" font-size="11" fill="{color}" opacity="0">{name}'
                     + interval_visibility(start, end) + "</text>")
            s.append(f'<text x="{px + 272}" y="{ay}" text-anchor="end" font-family="{MONO}" font-size="12" fill="#d1d1de" opacity="0">{pct}%'
                     + interval_visibility(start, end) + "</text>")

        def width(t, steps=steps):
            current = [p for st, _, p in steps if st <= t][-1]
            return fmt(round(272 * current / 100, 1))

        s.append(f'<rect x="{px}" y="{ay + 28}" width="272" height="6" rx="3" fill="#1d1d4b"/>')
        s.append(f'<rect x="{px}" y="{ay + 28}" width="0" height="6" rx="3" fill="url(#bold)">' + anim("width", sampled(width)) + "</rect>")

    s.append("</svg>")
    return "\n".join(s)


def part_svg(kind, x, y, ghost=False):
    op = ' opacity=".45"' if ghost else ""
    if kind == "holder":
        return (f'<g transform="translate({x},{y})"{op}><rect x="-17" y="-12" width="34" height="24" rx="5" fill="#e6e6e0" stroke="#9999a9" stroke-width="1.5"/>'
                f'<rect x="-11" y="-7" width="22" height="14" rx="2" fill="none" stroke="#b3b3be" stroke-dasharray="2 2"/></g>')
    return (f'<g transform="translate({x},{y})"{op}><rect x="-11" y="-7.5" width="22" height="15" rx="2" fill="#0b8f5a" stroke="{MINT}" stroke-width="1"/>'
            f'<circle cx="-6" cy="-3" r="1.6" fill="#ffd732"/><circle cx="0" cy="3" r="1.6" fill="#ffd732"/><circle cx="6" cy="-3" r="1.6" fill="#ffd732"/></g>')


# ---------------------------------------------------------------------------
# Hero banner
# ---------------------------------------------------------------------------

LOGO = """<g transform="translate({x},{y}) scale({k})">
  <rect width="64" height="64" rx="16" fill="url(#logoGrad)"/>
  <path d="M14 47.5h36" stroke="#000028" stroke-opacity=".28" stroke-width="3" stroke-linecap="round" stroke-dasharray="1 6"/>
  <g fill="none" stroke="#000028" stroke-width="5" stroke-linecap="round" stroke-linejoin="round">
    <path d="M17 52h16"/><path d="M25 52v-9"/><path d="M25 43l10-17"/><path d="M35 26l11 6"/><path d="M46 32v6"/>
  </g>
  <circle cx="25" cy="43" r="4.6" fill="#000028"/><circle cx="35" cy="26" r="4.6" fill="#000028"/>
  <path d="M41.5 40.5h9" stroke="#000028" stroke-width="4" stroke-linecap="round"/>
  <path d="M49 11.5l1.4 3.6 3.6 1.4-3.6 1.4-1.4 3.6-1.4-3.6-3.6-1.4 3.6-1.4z" fill="#ffffff"/>
</g>"""


def build_hero(dark):
    W, H = 1280, 380
    bg = NAVY if dark else SAND
    ink = "#ffffff" if dark else NAVY
    sub = "#b8b8cc" if dark else "#4d4d66"
    chip_bg = "#13133f" if dark else "#ffffff"
    chip_line = "#2e2e62" if dark else "#dcdcd4"
    dot = "#ffffff" if dark else NAVY
    dot_op = ".05" if dark else ".07"
    card = "#0e0e38" if dark else NAVY

    css = f"""
    .fade {{ opacity: 0; animation: 9s linear infinite; }}
    .q {{ animation-name: q; }} .t {{ animation-name: t; }} .r {{ animation-name: r; }}
    .r1 {{ animation-name: r1; }} .r2 {{ animation-name: r2; }} .r3 {{ animation-name: r3; }} .ok {{ animation-name: ok; }}
    @keyframes q  {{ 0%, 4% {{ opacity: 0; transform: translateY(8px); }} 9%, 92% {{ opacity: 1; transform: none; }} 97%, 100% {{ opacity: 0; }} }}
    @keyframes t  {{ 0%, 11% {{ opacity: 0; }} 13%, 24% {{ opacity: 1; }} 26%, 100% {{ opacity: 0; }} }}
    @keyframes r  {{ 0%, 25% {{ opacity: 0; transform: translateY(8px); }} 30%, 92% {{ opacity: 1; transform: none; }} 97%, 100% {{ opacity: 0; }} }}
    @keyframes r1 {{ 0%, 33% {{ opacity: 0; }} 37%, 92% {{ opacity: 1; }} 97%, 100% {{ opacity: 0; }} }}
    @keyframes r2 {{ 0%, 41% {{ opacity: 0; }} 45%, 92% {{ opacity: 1; }} 97%, 100% {{ opacity: 0; }} }}
    @keyframes r3 {{ 0%, 49% {{ opacity: 0; }} 53%, 92% {{ opacity: 1; }} 97%, 100% {{ opacity: 0; }} }}
    @keyframes ok {{ 0%, 57% {{ opacity: 0; }} 61%, 92% {{ opacity: 1; }} 97%, 100% {{ opacity: 0; }} }}
    .dot {{ animation: blink 1s ease-in-out infinite; }} .dot2 {{ animation-delay: .15s; }} .dot3 {{ animation-delay: .3s; }}
    @keyframes blink {{ 0%, 100% {{ opacity: .3; }} 50% {{ opacity: 1; }} }}
    .belt {{ animation: belt 1.2s linear infinite; }}
    @keyframes belt {{ to {{ stroke-dashoffset: -24; }} }}
    @media (prefers-reduced-motion: reduce) {{ .fade {{ animation: none; opacity: 1; }} .t {{ opacity: 0; }} .belt, .dot {{ animation: none; }} }}
    """

    chips = ["S7-1500 · Snap7", "TIA Portal Openness", "3× uArm Swift Pro", "LLM intent routing", "FastAPI"]
    chip_svg, cx = [], 64
    for label in chips:
        w = 18 + 7.4 * len(label)
        chip_svg.append(f'<g transform="translate({cx:.0f},282)"><rect width="{w:.0f}" height="30" rx="15" fill="{chip_bg}" stroke="{chip_line}"/>'
                        f'<text x="{w / 2:.0f}" y="19.5" text-anchor="middle" font-family="{MONO}" font-size="12" fill="{ink}">{label}</text></g>')
        cx += w + 10

    return f"""<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" role="img" aria-label="TIA S7 Copilot - talk to your PLC-controlled assembly cell">
<title>TIA S7 Copilot</title>
<defs>
  <pattern id="dots" width="20" height="20" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1.1" fill="{dot}" fill-opacity="{dot_op}"/></pattern>
  <linearGradient id="bold" x1="0" x2="1"><stop offset="0" stop-color="{MINT}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>
  <linearGradient id="logoGrad" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="{MINT}"/><stop offset="1" stop-color="#00a3a3"/></linearGradient>
  <radialGradient id="halo" cx=".5" cy=".5" r=".5"><stop offset="0" stop-color="{PETROL}" stop-opacity=".35"/><stop offset="1" stop-color="{PETROL}" stop-opacity="0"/></radialGradient>
</defs>
<style>{css}</style>
<rect width="{W}" height="{H}" rx="24" fill="{bg}"/>
<rect width="{W}" height="{H}" rx="24" fill="url(#dots)"/>
<rect x="0" y="{H - 5}" width="{W}" height="5" fill="url(#bold)"/>
<circle cx="1010" cy="190" r="260" fill="url(#halo)"/>

{LOGO.format(x=64, y=58, k=1.0)}
<text x="146" y="104" font-size="50" font-weight="700" letter-spacing="-1.2" fill="{ink}">TIA S7 Copilot</text>
<text x="64" y="178" font-size="25" font-weight="600" fill="{ink}">Talk to your PLC-controlled assembly cell.</text>
<text x="64" y="214" font-size="16.5" fill="{sub}">Natural-language control, live telemetry and energy analytics for a three-arm</text>
<text x="64" y="239" font-size="16.5" fill="{sub}">PCB assembly line driven by an S7-1500 PLC and uArm Swift Pro robots.</text>
{''.join(chip_svg)}

<g transform="translate(796,48)">
  <rect width="420" height="284" rx="20" fill="{card}"/>
  <rect width="420" height="284" rx="20" fill="none" stroke="{PETROL}" stroke-opacity=".55"/>
  <circle cx="24" cy="24" r="4.5" fill="#ff2640"/><circle cx="40" cy="24" r="4.5" fill="#ffd732"/><circle cx="56" cy="24" r="4.5" fill="{MINT}"/>
  <text x="396" y="28" text-anchor="end" font-family="{MONO}" font-size="11" fill="#9999b3">operator chat</text>

  <g class="fade q"><rect x="166" y="46" width="232" height="38" rx="14" fill="url(#bold)"/>
    <text x="282" y="70" text-anchor="middle" font-size="15" font-weight="600" fill="{NAVY}">copilot, set speed to 80%</text></g>

  <g class="fade t"><circle class="dot" cx="34" cy="112" r="4" fill="{MINT}"/><circle class="dot dot2" cx="48" cy="112" r="4" fill="{MINT}"/><circle class="dot dot3" cx="62" cy="112" r="4" fill="{MINT}"/>
    <text x="78" y="116" font-family="{MONO}" font-size="11.5" fill="#9999b3">classifying → SPEED_CHANGE</text></g>

  <g class="fade r"><rect x="20" y="100" width="380" height="164" rx="14" fill="#ffffff" fill-opacity=".06" stroke="#ffffff" stroke-opacity=".10"/>
    <rect x="20" y="100" width="4" height="164" rx="2" fill="{PETROL}"/>
    <text x="40" y="128" font-size="15.5" font-weight="700" fill="#ffffff">✓ Robot speed 50% → 80%</text></g>
  <g class="fade r1"><text x="40" y="158" font-family="{MONO}" font-size="12.5" fill="#d1d1de">DB1.DBD12  prox-1 timer  5500 → 4000 ms</text></g>
  <g class="fade r2"><text x="40" y="182" font-family="{MONO}" font-size="12.5" fill="#d1d1de">DB1.DBD16  prox-3 timer  7000 → 5800 ms</text></g>
  <g class="fade r3"><text x="40" y="206" font-family="{MONO}" font-size="12.5" fill="#d1d1de">cycle 80.0 s → 69.2 s   ·   +7 parts / h</text></g>
  <g class="fade ok"><rect x="40" y="222" width="160" height="26" rx="13" fill="{MINT}" fill-opacity=".14"/>
    <circle cx="56" cy="235" r="4.5" fill="{MINT}"/><text x="68" y="239.5" font-family="{MONO}" font-size="12" fill="{MINT}">PLC write verified</text></g>
</g>
</svg>
"""


# ---------------------------------------------------------------------------
# Hardware / wiring overview
# ---------------------------------------------------------------------------

def build_hardware():
    W, H = 1200, 650
    s = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W}" height="{H}" font-family="{FONT}" '
         f'role="img" aria-label="Hardware and wiring overview of the assembly cell">',
         "<title>Hardware: S7-1500 rack, field I/O, three uArm Swift Pro robots and the operator PC</title>",
         f"""<defs>
  <pattern id="hwdots" width="18" height="18" patternUnits="userSpaceOnUse"><circle cx="1" cy="1" r="1" fill="#ffffff" fill-opacity=".05"/></pattern>
  <linearGradient id="hwbold" x1="0" x2="1"><stop offset="0" stop-color="{MINT}"/><stop offset="1" stop-color="{CYAN}"/></linearGradient>
</defs>
<style>
  .flow {{ animation: flow 1.4s linear infinite; }}
  @keyframes flow {{ to {{ stroke-dashoffset: -20; }} }}
  @media (prefers-reduced-motion: reduce) {{ .flow {{ animation: none; }} }}
</style>""",
         f'<rect width="{W}" height="{H}" rx="22" fill="{NAVY}"/>',
         f'<rect x="1" y="1" width="{W - 2}" height="{H - 2}" rx="21" fill="url(#hwdots)" stroke="#26265a"/>',
         f'<rect x="0" y="{H - 4}" width="{W}" height="4" fill="url(#hwbold)"/>',
         f'<text x="40" y="50" font-family="{MONO}" font-size="12.5" fill="{MINT}" letter-spacing="1.6">HOW THE CELL IS WIRED</text>',
         f'<text x="{W - 40}" y="50" text-anchor="end" font-family="{MONO}" font-size="11" fill="#9999b3">S7-1500 · 3× uArm Swift Pro · 2 conveyors · 4 proximity sensors</text>']

    def card(x, y, w, h, title, stroke="#37376b"):
        s.append(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="12" fill="#0e0e38" stroke="{stroke}"/>')
        if title:
            s.append(f'<text x="{x + 16}" y="{y + 26}" font-family="{MONO}" font-size="11.5" fill="{MINT}" letter-spacing="1.2">{title}</text>')

    def text(x, y, value, size=13, fill="#ffffff", mono=False, anchor="start", weight="400"):
        family = f' font-family="{MONO}"' if mono else ""
        s.append(f'<text x="{x}" y="{y}" text-anchor="{anchor}"{family} font-size="{size}" font-weight="{weight}" fill="{fill}">{value}</text>')

    # cloud + operator PC
    card(40, 84, 260, 74, "AZURE OPENAI")
    text(56, 138, "chat completions · intent routing", 12.5, "#d1d1de")
    card(40, 196, 260, 250, "OPERATOR PC · WINDOWS", PETROL)
    rows = [("Dashboard + chat", "browser :8000"), ("Copilot API", "FastAPI :8000"), ("Robot controller", "FastAPI :8001"),
            ("Voice assistant", "wake word"), ("TIA Portal V19", "Openness, optional")]
    for i, (left, right) in enumerate(rows):
        y = 246 + i * 40
        text(56, y, left, 13.5)
        text(284, y, right, 10.5, "#9999b3", mono=True, anchor="end")
        if i < len(rows) - 1:
            s.append(f'<line x1="56" y1="{y + 15}" x2="284" y2="{y + 15}" stroke="#26265a"/>')
    s.append(f'<line x1="170" y1="158" x2="170" y2="196" stroke="#d1d1de" stroke-width="2" stroke-dasharray="4 4" class="flow"/>')
    text(178, 182, "HTTPS", 10, "#9999b3", mono=True)

    # S7-1500 rack
    text(420, 96, "S7-1500 PLC", 11.5, MINT, mono=True)
    s.append('<rect x="420" y="110" width="440" height="200" rx="10" fill="#0e0e38" stroke="#37376b"/>')
    s.append('<rect x="432" y="122" width="132" height="176" rx="6" fill="#1d1d4b" stroke="#009999" stroke-width="1.5"/>')
    s.append('<rect x="444" y="134" width="108" height="42" rx="4" fill="#000028" stroke="#37376b"/>')
    text(454, 151, "RUN", 10, MINT, mono=True)
    text(454, 167, "1511-1 PN", 10, "#ffffff", mono=True)
    text(444, 198, "CPU 1511-1 PN", 12.5, weight="700")
    text(444, 216, "6ES7 511-1AK02-0AB0", 9.5, "#9999b3", mono=True)
    text(444, 232, "firmware V2.9", 9.5, "#9999b3", mono=True)
    for i in range(2):
        s.append(f'<rect x="{444 + i * 22}" y="252" width="16" height="13" rx="2" fill="#000028" stroke="{MINT}"/>')
    text(492, 263, "X1 P1 / P2", 9.5, "#d1d1de", mono=True)

    modules = [(572, "AQ 8xU/I", "HS", "6ES7 532-", "5HF00-0AB0"),
               (668, "DI 16x", "24VDC BA", "6ES7 521-", "1BH10-0AA0"),
               (764, "DQ 8x", "24VDC/2A HF", "6ES7 522-", "1BF00-0AB0")]
    for x, l1, l2, a1, a2 in modules:
        s.append(f'<rect x="{x}" y="122" width="90" height="176" rx="6" fill="#1d1d4b" stroke="#37376b"/>')
        for k in range(8):
            s.append(f'<rect x="{x + 12 + (k % 2) * 36}" y="{134 + (k // 2) * 12}" width="30" height="6" rx="2" fill="#33334e"/>')
        text(x + 10, 206, l1, 11.5, weight="700")
        text(x + 10, 222, l2, 11.5, weight="700")
        text(x + 10, 246, a1, 9.5, "#9999b3", mono=True)
        text(x + 10, 260, a2, 9.5, "#9999b3", mono=True)
    for i, x in enumerate([498, 617, 713, 809], start=1):
        text(x, 290, f"slot {i}", 9.5, "#66667e", mono=True, anchor="middle")

    s.append(f'<rect x="420" y="322" width="440" height="36" rx="8" fill="#13133f" stroke="#ffd732" stroke-opacity=".7"/>')
    text(436, 345, "SITOP PSU6200 · 24 V DC for PLC I/O and conveyor drives", 12.5, "#ffffff")

    s.append('<rect x="420" y="372" width="440" height="74" rx="10" fill="#13133f" stroke="#37376b"/>')
    text(436, 394, 'DB1 "Daten_ai" · the copilot\'s window into the PLC', 11, MINT, mono=True)
    text(436, 414, "DBX0.0 Start · DBX0.1 Stop · DBW2 Total_prod · DBX4.0/.1 Conveyors", 10, "#d1d1de", mono=True)
    text(436, 432, "DBX10.0-.2 arm triggers · DBX10.3-.6 Prox 1-4 · DBD12/16/22", 10, "#d1d1de", mono=True)

    # Ethernet PC <-> CPU
    s.append(f'<line x1="300" y1="258" x2="444" y2="258" stroke="{PETROL}" stroke-width="3"/>')
    s.append(f'<line x1="300" y1="258" x2="444" y2="258" stroke="{MINT}" stroke-width="3" stroke-dasharray="6 14" class="flow"/>')
    text(360, 236, "Ethernet", 10, "#d1d1de", mono=True, anchor="middle")
    text(360, 284, "ISO-on-TCP :102", 10, "#9999b3", mono=True, anchor="middle")

    # field I/O
    field = [("PROXIMITY SENSORS", "S1-S4 along both conveyors", "DI I0.0-I0.3", 150),
             ("EMERGENCY STOP", "E-stop button + Em1-Em3 inputs", "DI I0.4-I0.7", 190),
             ("CONVEYOR 1 + 2", "24 V DC drives · fast / slow", "DQ Q16.4-Q16.7", 230),
             ("ROBOT STATUS OUT", "Robot_1-3 for OPC UA mirroring", "DQ Q16.1-Q16.3", 270)]
    for i, (title, desc, badge, src_y) in enumerate(field):
        y = 84 + i * 92
        card(900, y, 260, 76, title)
        text(916, y + 50, desc, 12, "#d1d1de")
        bw = 10 + 6.2 * len(badge)
        s.append(f'<rect x="{1144 - bw:.0f}" y="{y + 12}" width="{bw:.0f}" height="20" rx="10" fill="#ffd732" fill-opacity=".14"/>')
        text(1144 - bw / 2, y + 26, badge, 10, "#ffd732", mono=True, anchor="middle")
        cy = y + 38
        s.append(f'<path d="M860 {src_y} H880 V{cy} H900" fill="none" stroke="#ffd732" stroke-opacity=".75" stroke-width="1.8"/>')
        s.append(f'<circle cx="860" cy="{src_y}" r="3" fill="#ffd732"/>')

    # robot arms over USB
    arms = [("ARM 1", "COM3", "vacuum pump", "picks PCB holders"),
            ("ARM 2", "COM5", "gripper", "transfers + holds"),
            ("ARM 3", "COM4", "vacuum pump", "places the PCB")]
    s.append(f'<path d="M170 446 V478 H790" fill="none" stroke="{MINT}" stroke-width="2.2" stroke-dasharray="5 5" class="flow"/>')
    text(186, 470, "USB serial × 3 · uArm Python SDK", 10.5, "#9999b3", mono=True)
    for i, (name, port, tool, role) in enumerate(arms):
        x = 420 + i * 150
        s.append(f'<line x1="{x + 70}" y1="478" x2="{x + 70}" y2="500" stroke="{MINT}" stroke-width="2.2"/>')
        card(x, 500, 140, 120, name, PETROL)
        text(x + 16, 546, "uArm Swift Pro", 13, weight="700")
        text(x + 16, 566, f"USB · {port}", 10.5, "#9999b3", mono=True)
        text(x + 16, 586, tool, 12, MINT)
        text(x + 16, 605, role, 11, "#d1d1de")

    # legend
    card(900, 468, 260, 152, "LEGEND")
    legend = [(PETROL, "", "Ethernet · Snap7 + TIA Openness"), (MINT, "5 5", "USB serial to the robots"),
              ("#ffd732", "", "24 V DC field wiring"), ("#d1d1de", "4 4", "HTTPS to the language model")]
    for i, (color, dash, label) in enumerate(legend):
        y = 520 + i * 26
        dash_attr = f' stroke-dasharray="{dash}"' if dash else ""
        s.append(f'<line x1="916" y1="{y - 4}" x2="950" y2="{y - 4}" stroke="{color}" stroke-width="3"{dash_attr}/>')
        text(962, y, label, 11.5, "#d1d1de")

    s.append("</svg>")
    return "\n".join(s)


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "hero-light.svg").write_text(build_hero(dark=False), encoding="utf-8")
    (OUT / "hero-dark.svg").write_text(build_hero(dark=True), encoding="utf-8")
    (OUT / "cell-cycle.svg").write_text(build_cell(), encoding="utf-8")
    (OUT / "hardware.svg").write_text(build_hardware(), encoding="utf-8")
    for name in ("hero-light.svg", "hero-dark.svg", "cell-cycle.svg", "hardware.svg"):
        print(f"{name}: {(OUT / name).stat().st_size / 1024:.1f} KB")


if __name__ == "__main__":
    main()
