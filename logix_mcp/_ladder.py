"""Render Logix ladder neutral text as Unicode text and SVG diagrams."""
from __future__ import annotations

import html
from dataclasses import dataclass, field

from logix_mcp._origin import BranchNode, InstrNode, _parse_series

# Studio-like glyphs on continuous box-drawing rails.
_WIRE = "─"
_PIPE = "│"
_CONTACT = {"XIC": "┤ ├", "XIO": "┤/├"}
_COIL = {"OTE": "( )", "OTL": "(L)", "OTU": "(U)", "OTF": "(F)"}
_SPECIAL = {
    "ONS": "[ONS]",
    "OSR": "[OSR]",
    "OSF": "[OSF]",
    "AFI": "[AFI]",
    "NOP": "[NOP]",
    "RES": "[RES]",
}
# Left-rail conditions vs right-rail outputs (Studio input/output split).
_INPUT_OPCODES = frozenset(
    {
        "XIC",
        "XIO",
        "ONS",
        "OSR",
        "OSF",
        "AFI",
        "NOP",
        "EQU",
        "NEQ",
        "GRT",
        "GEQ",
        "LES",
        "LEQ",
        "LIM",
        "MEQ",
        "CMP",
    }
)
_TEXT_IO_GAP = 10
_SVG_IO_GAP = 36.0

# Where a series wire meets a branch/box corner, promote to a T-junction.
_RAIL_JOINS = (
    ("─┌", "─┬"),
    ("┐─", "┬─"),
    ("─└", "─┴"),
    ("┘─", "┴─"),
)


@dataclass
class Tile:
    rows: list[str]
    rail: int

    @property
    def width(self) -> int:
        return len(self.rows[0]) if self.rows else 0

    @property
    def height(self) -> int:
        return len(self.rows)


def _pad_row(text: str, width: int) -> str:
    if len(text) >= width:
        return text[:width]
    pad = width - len(text)
    left = pad // 2
    return (" " * left) + text + (" " * (pad - left))


def _fix_rail_joins(row: str) -> str:
    prev = None
    while prev != row:
        prev = row
        for old, new in _RAIL_JOINS:
            if old != new:
                row = row.replace(old, new)
    return row


def _hstack(tiles: list[Tile]) -> Tile:
    """Join tiles on one rail with ``─`` links and T-junction fixes."""
    if not tiles:
        return Tile([_WIRE * 2], 0)
    rail = max(tile.rail for tile in tiles)
    height = max(tile.height + (rail - tile.rail) for tile in tiles)
    padded: list[Tile] = []
    for tile in tiles:
        top = rail - tile.rail
        bottom = height - tile.height - top
        rows = ([" " * tile.width] * top) + tile.rows + ([" " * tile.width] * bottom)
        padded.append(Tile(rows, rail))
    rows: list[str] = []
    for y in range(height):
        parts: list[str] = []
        for i, tile in enumerate(padded):
            if i:
                parts.append(_WIRE if y == rail else " ")
            parts.append(tile.rows[y])
        line = "".join(parts)
        if y == rail:
            line = _fix_rail_joins(line)
        rows.append(line)
    return Tile(rows, rail)


def _normalize_leg_width(leg: Tile, width: int) -> Tile:
    if leg.width >= width:
        return leg
    extra = width - leg.width
    left = extra // 2
    right = extra - left
    rows = []
    for i, row in enumerate(leg.rows):
        if i == leg.rail:
            rows.append((_WIRE * left) + row + (_WIRE * right))
        else:
            rows.append((" " * left) + row + (" " * right))
    return Tile(rows, leg.rail)


def _vstack_branch(legs: list[Tile]) -> Tile:
    """Parallel legs with continuous sides; series rail uses ┬ junctions."""
    if not legs:
        return Tile(["┬──┬", "└──┘"], 0)
    if len(legs) == 1:
        return legs[0]
    width = max(leg.width for leg in legs)
    normalized = [_normalize_leg_width(leg, width) for leg in legs]

    rail_indices: list[int] = []
    flat: list[tuple[bool, str]] = []
    for leg in normalized:
        base = len(flat)
        for y, row in enumerate(leg.rows):
            is_rail = y == leg.rail
            flat.append((is_rail, row))
            if is_rail:
                rail_indices.append(base + y)

    body_rows: list[str] = []
    rail_of_first = rail_indices[0]
    first_rail, last_rail = rail_indices[0], rail_indices[-1]
    for index, (is_rail, row) in enumerate(flat):
        if not is_rail:
            # Labels above the series rail must not carry branch walls — those
            # walls were getting concatenated into one chaotic top strip when
            # contacts and branches were joined horizontally.
            if index < first_rail:
                body_rows.append(" " + row + " ")
            else:
                body_rows.append(_PIPE + row + _PIPE)
            continue
        # First rail is the series path through the branch → T-junctions.
        if index == first_rail:
            left, right = "┬", "┬"
        elif index == last_rail:
            left, right = "└", "┘"
        else:
            left, right = "├", "┤"
        body_rows.append(left + row + right)
    return Tile(body_rows, rail_of_first)


def _wrap_words(text: str, width: int) -> list[str]:
    """Soft-wrap on spaces; hard-break overlong words with a trailing hyphen."""
    text = " ".join((text or "").split())
    if not text:
        return []
    width = max(1, int(width))
    words = text.split(" ")
    lines: list[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            lines.append(current)
            current = ""

    for word in words:
        while len(word) > width:
            flush()
            if width == 1:
                lines.append(word[0])
                word = word[1:]
                continue
            lines.append(word[: width - 1] + "-")
            word = word[width - 1 :]
        if not word:
            continue
        if not current:
            current = word
        elif len(current) + 1 + len(word) <= width:
            current = f"{current} {word}"
        else:
            flush()
            current = word
    flush()
    return lines


def _caption_rows(description: str, width: int) -> list[str]:
    return [_pad_row(line, width) for line in _wrap_words(description, width)]


def _desc_for_args(args: list[str], descriptions: dict[str, str] | None) -> str:
    if not descriptions:
        return ""
    for arg in args:
        key = (arg or "").strip().lower()
        if key and key in descriptions:
            return descriptions[key]
    return ""


def _label_tile(symbol: str, label: str, description: str = "") -> Tile:
    """Tag above a contact/coil; optional wrapped description above the tag."""
    label = label.strip() or " "
    core = _WIRE + symbol + _WIRE
    width = max(len(label), len(core))
    lab = _pad_row(label, width)
    extra = width - len(core)
    left = extra // 2
    right = extra - left
    rail = (_WIRE * left) + core + (_WIRE * right)
    rows = _caption_rows(description, width) + [lab, rail]
    return Tile(rows, len(rows) - 1)


def _block_tile(name: str, args: list[str], description: str = "") -> Tile:
    """Instruction box with the rail piercing the sides (┤…├)."""
    lines = [name] + [arg.strip() or " " for arg in args]
    inner = max(max(len(line) for line in lines), 3)
    rows = ["┌" + (_WIRE * inner) + "┐"]
    for line in lines:
        rows.append(_PIPE + _pad_row(line, inner) + _PIPE)
    rows.append("└" + (_WIRE * inner) + "┘")
    rail = len(rows) // 2
    content = rows[rail][1:-1]
    rows[rail] = "┤" + content + "├"
    out = [
        (_WIRE + row + _WIRE) if i == rail else (" " + row + " ")
        for i, row in enumerate(rows)
    ]
    width = len(out[0]) if out else inner + 4
    caption = _caption_rows(description, width)
    return Tile(caption + out, rail + len(caption))


def _instr_tile(node: InstrNode, descriptions: dict[str, str] | None = None) -> Tile:
    name = node.name.upper()
    args = node.args
    label = args[0] if args else ""
    desc = _desc_for_args(args, descriptions)
    if name in _CONTACT:
        return _label_tile(_CONTACT[name], label, desc)
    if name in _COIL:
        return _label_tile(_COIL[name], label, desc)
    if name == "RES":
        return _label_tile(_SPECIAL["RES"], label, desc)
    if name in _SPECIAL:
        return _label_tile(_SPECIAL[name], label or name, desc)
    return _block_tile(name, args, desc)


def _instr_is_output(name: str) -> bool:
    return name.upper() not in _INPUT_OPCODES


def _leg_ends_with_output(leg: list) -> bool:
    for child in reversed(leg):
        if isinstance(child, InstrNode):
            return _instr_is_output(child.name)
        if isinstance(child, BranchNode):
            return _node_is_output(child)
    return False


def _node_is_output(node) -> bool:
    """True when this series element belongs on the output (right) side."""
    if isinstance(node, InstrNode):
        return _instr_is_output(node.name)
    if not isinstance(node, BranchNode):
        return False
    nonempty = [leg for leg in node.legs if leg]
    if not nonempty:
        return False
    if all(_leg_ends_with_output(leg) for leg in nonempty):
        return True
    return sum(_leg_ends_with_output(leg) for leg in nonempty) > len(nonempty) / 2


def _io_gap_tile(width: int = _TEXT_IO_GAP) -> Tile:
    """Longer wire between the input zone and the output zone."""
    width = max(4, int(width))
    return Tile([" " * width, _WIRE * width], 1)


def _series_tile(series: list, descriptions: dict[str, str] | None = None) -> Tile:
    if not series:
        return Tile([_WIRE * 4], 0)
    split = next((i for i, node in enumerate(series) if _node_is_output(node)), None)
    tiles: list[Tile] = []
    for i, node in enumerate(series):
        if split is not None and i == split and i > 0:
            tiles.append(_io_gap_tile())
        if isinstance(node, BranchNode):
            tiles.append(_branch_tile(node, descriptions))
        elif isinstance(node, InstrNode):
            tiles.append(_instr_tile(node, descriptions))
    return _hstack(tiles)


def _branch_tile(node: BranchNode, descriptions: dict[str, str] | None = None) -> Tile:
    legs = [
        _series_tile(leg, descriptions) if leg else Tile([_WIRE * 4], 0)
        for leg in node.legs
    ]
    return _vstack_branch(legs)


def _power_rails(tile: Tile) -> Tile:
    rows = []
    for y, row in enumerate(tile.rows):
        if y == tile.rail:
            rows.append("┃" + _fix_rail_joins(_WIRE + row + _WIRE) + "┃")
        else:
            rows.append("┃ " + row + " ┃")
    return Tile(rows, tile.rail)


def parse_rung_text(text: str) -> list:
    body = (text or "").strip()
    if body.endswith(";"):
        body = body[:-1]
    series, _ = _parse_series(body, 0)
    return series


def render_rung_text(
    text: str,
    *,
    descriptions: dict[str, str] | None = None,
) -> str:
    """Studio-like ladder with continuous Unicode box-drawing rails.

    When ``descriptions`` maps operand text → L5X comment, each instruction
    shows that text soft-wrapped and centered above its tag (column width
    unchanged; overlong words hard-break with a hyphen).
    """
    series = parse_rung_text(text)
    if not series:
        return "(empty rung)"
    return "\n".join(_power_rails(_series_tile(series, descriptions)).rows)


def render_rung_ascii(
    text: str,
    *,
    descriptions: dict[str, str] | None = None,
) -> str:
    return render_rung_text(text, descriptions=descriptions)


render_rung_unicode = render_rung_ascii


# --- Geometric SVG layout ---

WIRE = "#9cdcfe"
INK = "#d4d4d4"
ACCENT = "#ce9178"
BG = "#1e1e1e"
BOX = "#569cd6"


@dataclass
class SvgBox:
    width: float
    height: float
    rail_y: float
    parts: list[str] = field(default_factory=list)

    def shifted(self, dx: float, dy: float) -> "SvgBox":
        if not dx and not dy:
            return self
        transformed = [f'<g transform="translate({dx:.1f},{dy:.1f})">{p}</g>' for p in self.parts]
        return SvgBox(self.width, self.height, self.rail_y + dy, transformed)


def _esc(text: str) -> str:
    return html.escape(text, quote=True)


def _text(x: float, y: float, content: str, *, size: int = 11, fill: str = INK, anchor: str = "middle") -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
        f'text-anchor="{anchor}" font-family="Segoe UI, Consolas, sans-serif">'
        f"{_esc(content)}</text>"
    )


def _line(x1: float, y1: float, x2: float, y2: float, *, stroke: str = WIRE, width: float = 2) -> str:
    return (
        f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
        f'stroke="{stroke}" stroke-width="{width}" stroke-linecap="square"/>'
    )


def _wire_stub(width: float, rail_y: float) -> SvgBox:
    return SvgBox(width, rail_y * 2, rail_y, [_line(0, rail_y, width, rail_y)])


def _svg_contact(name: str, tag: str) -> SvgBox:
    tag = tag or name
    bar_gap = 8.0
    bar_h = 10.0
    # Cell is only as wide as the glyph needs; long tags may extend visually via text-anchor.
    tag_w = len(tag) * 7.0 + 4.0
    sym_w = bar_gap + 16.0
    w = max(sym_w + 12.0, min(tag_w, sym_w + 48.0))
    rail = 28.0
    h = 44.0
    cx = w / 2
    left = cx - bar_gap / 2
    right = cx + bar_gap / 2
    parts = [
        _text(cx, 14, tag, size=11),
        _line(0, rail, left, rail),
        _line(left, rail - bar_h, left, rail + bar_h, width=2.5),
        _line(right, rail - bar_h, right, rail + bar_h, width=2.5),
        _line(right, rail, w, rail),
    ]
    if name == "XIO":
        parts.append(_line(left - 2, rail + bar_h + 1, right + 2, rail - bar_h - 1, stroke=ACCENT, width=2))
    return SvgBox(w, h, rail, parts)


def _svg_coil(name: str, tag: str) -> SvgBox:
    tag = tag or name
    label = {"OTE": "", "OTL": "L", "OTU": "U", "OTF": "F"}.get(name, "")
    r = 11.0
    stub = 6.0
    sym_w = stub * 2 + r * 2
    tag_w = len(tag) * 7.0 + 8.0
    w = max(sym_w, tag_w)
    rail = 28.0
    h = 48.0
    cx = w / 2
    parts = [
        _text(cx, 14, tag, size=11),
        _line(0, rail, cx - r - 1, rail),
        f'<circle cx="{cx:.1f}" cy="{rail:.1f}" r="{r:.1f}" fill="none" stroke="{WIRE}" stroke-width="2"/>',
        _line(cx + r + 1, rail, w, rail),
    ]
    if label:
        parts.append(_text(cx, rail + 4, label, size=12, fill=ACCENT))
    return SvgBox(w, h, rail, parts)


def _svg_special(name: str, tag: str) -> SvgBox:
    shown = tag or name
    box_w, box_h = 40.0, 22.0
    stub = 6.0
    sym_w = stub * 2 + box_w
    tag_w = len(shown) * 7.0 + 8.0
    w = max(sym_w, tag_w)
    rail = 28.0
    h = 48.0
    cx = w / 2
    parts = [
        _text(cx, 14, shown, size=11),
        _line(0, rail, cx - box_w / 2 - 1, rail),
        f'<rect x="{cx - box_w / 2:.1f}" y="{rail - box_h / 2:.1f}" width="{box_w:.1f}" height="{box_h:.1f}" '
        f'rx="3" fill="none" stroke="{WIRE}" stroke-width="2"/>',
        _text(cx, rail + 4, name, size=10, fill=ACCENT),
        _line(cx + box_w / 2 + 1, rail, w, rail),
    ]
    return SvgBox(w, h, rail, parts)


def _svg_block(name: str, args: list[str]) -> SvgBox:
    lines = [name] + list(args)
    inner_w = max(max((len(line) for line in lines), default=3) * 7.2, 48)
    box_w = inner_w + 16
    line_h = 14
    box_h = 12 + line_h * len(lines)
    stub = 6.0
    w = box_w + stub * 2
    rail = box_h / 2 + 8
    h = box_h + 16
    x0 = stub
    y0 = 8
    parts = [
        _line(0, rail, x0, rail),
        f'<rect x="{x0:.1f}" y="{y0:.1f}" width="{box_w:.1f}" height="{box_h:.1f}" rx="4" '
        f'fill="#252526" stroke="{BOX}" stroke-width="2"/>',
        _line(x0 + box_w, rail, w, rail),
    ]
    for i, line in enumerate(lines):
        fill = BOX if i == 0 else INK
        parts.append(_text(x0 + box_w / 2, y0 + 16 + i * line_h, line, size=11, fill=fill))
    return SvgBox(w, h, rail, parts)


def _svg_instr(node: InstrNode) -> SvgBox:
    name = node.name.upper()
    args = node.args
    tag = args[0] if args else ""
    if name in {"XIC", "XIO"}:
        return _svg_contact(name, tag)
    if name in {"OTE", "OTL", "OTU", "OTF"}:
        return _svg_coil(name, tag)
    if name in {"ONS", "OSR", "OSF", "AFI", "NOP", "RES"}:
        return _svg_special(name, tag)
    return _svg_block(name, args)


def _svg_hstack(boxes: list[SvgBox]) -> SvgBox:
    if not boxes:
        return _wire_stub(12, 20)
    rail = max(box.rail_y for box in boxes)
    height = max(box.height + (rail - box.rail_y) for box in boxes)
    link = 8.0
    x = 0.0
    parts: list[str] = []
    for i, box in enumerate(boxes):
        if i:
            parts.append(_line(x, rail, x + link, rail))
            x += link
        dy = rail - box.rail_y
        parts.extend(box.shifted(x, dy).parts)
        x += box.width
    return SvgBox(x, height, rail, parts)


def _svg_branch(node: BranchNode) -> SvgBox:
    legs = [_svg_series(leg) if leg else _wire_stub(40, 20) for leg in node.legs]
    if len(legs) == 1:
        return legs[0]
    width = max(leg.width for leg in legs)
    # Pad each leg's content width with wires to a common width.
    padded: list[SvgBox] = []
    for leg in legs:
        if leg.width >= width:
            padded.append(leg)
            continue
        left = (width - leg.width) / 2
        right = width - leg.width - left
        parts = []
        if left:
            parts.append(_line(0, leg.rail_y, left, leg.rail_y))
        parts.extend(leg.shifted(left, 0).parts)
        if right:
            parts.append(_line(left + leg.width, leg.rail_y, width, leg.rail_y))
        padded.append(SvgBox(width, leg.height, leg.rail_y, parts))
    gap = 10.0
    total_h = sum(leg.height for leg in padded) + gap * (len(padded) - 1)
    side = 14.0
    parts: list[str] = []
    y = 0.0
    rails: list[float] = []
    for leg in padded:
        parts.extend(leg.shifted(side, y).parts)
        rail = y + leg.rail_y
        rails.append(rail)
        parts.append(_line(0, rail, side, rail))
        parts.append(_line(side + width, rail, side + width + side, rail))
        parts.append(f'<circle cx="0" cy="{rail:.1f}" r="2.5" fill="{WIRE}"/>')
        parts.append(f'<circle cx="{side + width + side:.1f}" cy="{rail:.1f}" r="2.5" fill="{WIRE}"/>')
        y += leg.height + gap
    for left_rail, right_rail in zip(rails, rails[1:]):
        parts.append(_line(0, left_rail, 0, right_rail))
        parts.append(_line(side + width + side, left_rail, side + width + side, right_rail))
    return SvgBox(width + 2 * side, total_h, rails[0], parts)


def _svg_series(series: list) -> SvgBox:
    if not series:
        return _wire_stub(24, 20)
    split = next((i for i, node in enumerate(series) if _node_is_output(node)), None)
    boxes: list[SvgBox] = []
    for i, node in enumerate(series):
        if split is not None and i == split and i > 0:
            # Distinct span between conditions and coils/blocks.
            boxes.append(_wire_stub(_SVG_IO_GAP, 20))
        if isinstance(node, BranchNode):
            boxes.append(_svg_branch(node))
        elif isinstance(node, InstrNode):
            boxes.append(_svg_instr(node))
    return _svg_hstack(boxes)


def render_rung_svg(text: str, *, title: str = "") -> str:
    series = parse_rung_text(text)
    body = _svg_series(series) if series else _wire_stub(40, 20)
    pad = 16.0
    title_h = 22.0 if title else 0.0
    rail_left = 10.0
    width = body.width + pad * 2 + rail_left * 2
    height = body.height + pad * 2 + title_h
    # Shift body so rail connects to power rails.
    dx = pad + rail_left
    dy = pad + title_h
    shifted = body.shifted(dx, dy)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width:.0f}" height="{height:.0f}" '
        f'viewBox="0 0 {width:.1f} {height:.1f}">',
        f'<rect width="100%" height="100%" fill="{BG}"/>',
    ]
    if title:
        parts.append(_text(pad, pad + 12, title, size=12, fill="#cccccc", anchor="start"))
    # Power rails.
    x_left = pad
    x_right = width - pad
    y1 = pad + title_h
    y2 = height - pad
    parts.append(_line(x_left, y1, x_left, y2, width=3))
    parts.append(_line(x_right, y1, x_right, y2, width=3))
    # Connect body rail to rails.
    rail = shifted.rail_y
    parts.append(_line(x_left, rail, dx, rail))
    parts.append(_line(dx + body.width, rail, x_right, rail))
    parts.extend(shifted.parts)
    parts.append("</svg>")
    return "\n".join(parts)


def render_rung_diagram(
    text: str,
    *,
    title: str = "",
    format: str = "both",
    descriptions: dict[str, str] | None = None,
) -> str:
    """Return Unicode ladder text, SVG, or both for one rung's neutral text."""
    fmt = (format or "both").strip().lower()
    # ``ascii`` / ``unicode`` / ``text`` all mean the Unicode text ladder.
    if fmt in {"ascii", "unicode", "text"}:
        return render_rung_text(text, descriptions=descriptions)
    if fmt == "svg":
        return render_rung_svg(text, title=title)
    if fmt == "both":
        return "\n".join(
            [
                "```ladder",
                render_rung_text(text, descriptions=descriptions),
                "```",
                "",
                "```svg",
                render_rung_svg(text, title=title),
                "```",
            ]
        )
    raise ValueError("format must be text, ascii, unicode, svg, or both")
