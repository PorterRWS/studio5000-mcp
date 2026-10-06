"""Backward origin trace over ladder neutral text in an L5X.

A destructive cross-reference is a flat list of instructions that write one
tag. This module walks one step further: from each write, to the tags that
feed it (source operands and the contacts that gate it), and repeats until
it reaches a literal or a tag the project never writes.

Structured text, FBD/SFC, JSR, and unclassified instructions are recorded as
gaps and are not followed.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from logix_mcp._xml import _local, _rung_text, _scope_from_ancestor_chain
from logix_mcp._xref import _balanced_close, _dest_indexes, _split_args

_CONDITION: frozenset[str] = frozenset(
    {"XIC", "XIO", "EQU", "NEQ", "GRT", "GEQ", "LES", "LEQ", "LIM", "MEQ", "CMP"}
)
_LENGTH_THIRD: frozenset[str] = frozenset({"COP", "CPS", "FLL"})
_IMPLICIT_VALUE: dict[str, str] = {
    "OTE": "coil energize (1)",
    "OTL": "latch (1)",
    "OTU": "unlatch (0)",
    "CLR": "clear (0)",
    "RES": "reset (0)",
}
_MAX_STEPS = 8000

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_TAG = re.compile(
    r"(?<![A-Za-z0-9_])"
    r"(?:\\(?P<prog>[A-Za-z_][A-Za-z0-9_]*)\.)?"
    r"(?P<base>[A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)*)"
    r"(?P<suf>(?:\.[A-Za-z_][A-Za-z0-9_]*|\.\d+|\[[^\]]*\])*)"
)
_LITERAL = re.compile(
    r"[-+]?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?|\d+#[0-9A-Fa-f_]+"
)


def _is_literal(text: str) -> bool:
    t = text.strip()
    if len(t) >= 2 and t[0] in "'\"" and t[-1] == t[0]:
        return True
    return _LITERAL.fullmatch(t) is not None


@dataclass(frozen=True)
class Part:
    kind: str  # member | index
    value: str
    variable: bool = False


@dataclass(frozen=True)
class RawRef:
    program: str
    text: str


@dataclass(frozen=True)
class Resolved:
    scope: str
    base: str
    parts: tuple[Part, ...]

    @property
    def key(self) -> tuple:
        norm: list[tuple[str, str]] = []
        for part in self.parts:
            if part.kind == "member":
                norm.append(("m", part.value.lower()))
            elif part.variable:
                norm.append(("i", "*"))
            else:
                norm.append(("i", part.value))
        return (self.scope.lower(), self.base.lower(), tuple(norm))

    @property
    def display(self) -> str:
        text = self.base
        for part in self.parts:
            if part.kind == "member":
                text += "." + part.value
            else:
                text += "[" + (part.value if part.value else "*") + "]"
        if self.scope and self.scope != "Controller":
            return self.scope + "\\" + text
        return text

    def operand(self) -> str:
        """L5X comment operand for this reference, such as ``[0].3``."""
        text = ""
        for part in self.parts:
            if part.kind == "index":
                if part.variable:
                    return ""
                text += "[" + part.value + "]"
            else:
                text += "." + part.value
        return text


@dataclass
class InstrNode:
    name: str
    args: list[str]
    raw: str


@dataclass
class BranchNode:
    legs: list[list]


@dataclass
class WriteSite:
    program: str
    routine: str
    rung: str
    instruction: str
    snippet: str
    dest: Resolved
    feeders: list[tuple[str, Resolved, str]] = field(default_factory=list)
    literals: list[str] = field(default_factory=list)
    implicit: str = ""


@dataclass
class RungBox:
    box_id: str
    title: str
    dest: str
    inputs: list[str]
    links: list[tuple[str, str]]
    program: str = ""
    routine: str = ""
    rung: str = ""
    instruction: str = ""
    text: str = ""


def _is_zero_literal(text: str) -> bool:
    token = text.strip().lower().replace("_", "")
    if token in {"0", "+0", "-0", "0.0", "0.00", "16#0", "2#0", "8#0"}:
        return True
    if re.fullmatch(r"[+-]?\d+(?:\.\d+)?", token):
        try:
            return float(token) == 0.0
        except ValueError:
            return False
    return False


def _is_zero_write(site: WriteSite) -> bool:
    """True when the instruction's value is a literal 0 or a clear."""
    if site.instruction == "CLR":
        return True
    has_tag_source = any(role == "source" for role, _ref, _label in site.feeders)
    return any(_is_zero_literal(literal) for literal in site.literals) and not has_tag_source


def _skip_ws(text: str, index: int) -> int:
    while index < len(text) and text[index] in " \t\r\n":
        index += 1
    return index


def _parse_instr(text: str, index: int, match: re.Match[str]) -> tuple[InstrNode, int]:
    name = match.group(0).upper()
    open_at = _skip_ws(text, match.end())
    close_at = _balanced_close(text, open_at)
    if close_at < 0:
        raw = text[match.start() :].strip()
        return InstrNode(name, [], raw), len(text)
    body = text[open_at + 1 : close_at]
    raw = text[match.start() : close_at + 1]
    return InstrNode(name, _split_args(body), raw), close_at + 1


def _parse_series(text: str, index: int) -> tuple[list, int]:
    nodes: list = []
    while True:
        index = _skip_ws(text, index)
        if index >= len(text) or text[index] in ",];":
            break
        if text[index] == "[":
            node, index = _parse_branch(text, index)
            nodes.append(node)
            continue
        match = _IDENT.match(text, index)
        if match:
            paren_at = _skip_ws(text, match.end())
            if paren_at < len(text) and text[paren_at] == "(":
                node, index = _parse_instr(text, index, match)
                nodes.append(node)
                continue
        index += 1
    return nodes, index


def _parse_branch(text: str, index: int) -> tuple[BranchNode, int]:
    index += 1
    legs: list[list] = []
    while index < len(text):
        index = _skip_ws(text, index)
        if index >= len(text):
            break
        if text[index] == "]":
            index += 1
            break
        leg, index = _parse_series(text, index)
        legs.append(leg)
        index = _skip_ws(text, index)
        if index < len(text) and text[index] == ",":
            index += 1
            continue
        if index < len(text) and text[index] == "]":
            index += 1
            break
    return BranchNode(legs), index


def _contains(series: list, target: InstrNode) -> bool:
    for node in series:
        if node is target:
            return True
        if isinstance(node, BranchNode) and any(_contains(leg, target) for leg in node.legs):
            return True
    return False


def _all_conditions(node: BranchNode) -> list[InstrNode]:
    found: list[InstrNode] = []
    for leg in node.legs:
        for child in leg:
            if isinstance(child, InstrNode) and child.name in _CONDITION:
                found.append(child)
            elif isinstance(child, BranchNode):
                found.extend(_all_conditions(child))
    return found


def _conditions_for(series: list, target: InstrNode, inherited: list[InstrNode]) -> list[InstrNode]:
    acc = list(inherited)
    for node in series:
        if node is target:
            return acc
        if isinstance(node, InstrNode):
            if node.name in _CONDITION:
                acc.append(node)
            continue
        if isinstance(node, BranchNode):
            for leg in node.legs:
                if _contains(leg, target):
                    return _conditions_for(leg, target, acc)
            acc.extend(_all_conditions(node))
    return acc


def _extract_refs(text: str) -> list[RawRef]:
    refs: list[RawRef] = []

    def _scan(blob: str) -> None:
        for match in _TAG.finditer(blob):
            prog = match.group("prog") or ""
            base = match.group("base")
            suf = match.group("suf") or ""
            refs.append(RawRef(prog, base + suf))
            # A member name is part of this reference. Only an index can hold another tag.
            if suf:
                for inner in re.findall(r"\[([^\[\]]*)\]", suf):
                    _scan(inner)

    _scan(text)
    return refs


def _parse_suffix(suffix: str) -> tuple[Part, ...]:
    parts: list[Part] = []
    index = 0
    while index < len(suffix):
        if suffix[index] == ".":
            match = re.match(r"\.([A-Za-z_][A-Za-z0-9_]*|\d+)", suffix[index:])
            if not match:
                break
            parts.append(Part("member", match.group(1)))
            index += match.end()
            continue
        if suffix[index] == "[":
            close_at = suffix.find("]", index)
            if close_at < 0:
                break
            inner = suffix[index + 1 : close_at].strip()
            if _is_literal(inner):
                parts.append(Part("index", inner, False))
            else:
                parts.append(Part("index", inner, True))
            index = close_at + 1
            continue
        break
    return tuple(parts)


def _split_body(text: str) -> tuple[str, tuple[Part, ...]]:
    match = re.match(
        r"([A-Za-z_][A-Za-z0-9_]*(?::[A-Za-z0-9_]+)*)(.*)\Z",
        text.strip(),
    )
    if not match:
        return text.strip(), ()
    return match.group(1), _parse_suffix(match.group(2))


def _covers(write: Resolved, query: Resolved) -> bool:
    if write.scope.lower() != query.scope.lower():
        return False
    if write.base.lower() != query.base.lower():
        return False
    if len(write.parts) > len(query.parts):
        return False
    for left, right in zip(write.parts, query.parts):
        if left.kind != right.kind:
            return False
        if left.kind == "member":
            if left.value.lower() != right.value.lower():
                return False
            continue
        if left.variable or right.variable:
            continue
        if left.value != right.value:
            return False
    return True


def _flat_text(el) -> str:
    text = " ".join("".join(el.itertext()).split())
    if len(text) > 120:
        text = text[:119] + "…"
    return text


def _tag_texts(el) -> tuple[str, dict[str, str]]:
    desc = ""
    comments: dict[str, str] = {}
    for child in el:
        kind = _local(child.tag)
        if kind == "Description":
            desc = _flat_text(child)
        elif kind == "Comments":
            for comment in child:
                if _local(comment.tag) != "Comment":
                    continue
                operand = (comment.get("Operand") or "").strip().lower()
                text = _flat_text(comment)
                if operand and text:
                    comments[operand] = text
    return desc, comments


def _tag_scope(el) -> str:
    parent = el.getparent()
    while parent is not None:
        kind = _local(parent.tag)
        if kind in ("Program", "SafetyProgram"):
            return parent.get("Name") or "?"
        if kind in ("AddOnInstructionDefinition", "AddOnInstruction"):
            return "AOI:" + (parent.get("Name") or "?")
        parent = parent.getparent()
    return "Controller"


class OriginIndex:
    def __init__(self, root, *, logic: bool = True) -> None:
        self.defined: dict[tuple[str, str], str] = {}
        self.alias: dict[tuple[str, str], str] = {}
        self.descriptions: dict[tuple[str, str], str] = {}
        self.comments: dict[tuple[str, str], dict[str, str]] = {}
        self.scope_names: dict[str, str] = {"controller": "Controller"}
        self.writes: dict[tuple[str, str], list[WriteSite]] = {}
        self.gap_refs: dict[tuple[str, str], list[tuple[Resolved, str]]] = {}
        self.rung_text: dict[tuple[str, str, str], str] = {}
        self._load_tags(root)
        if logic:
            self._load_logic(root)

    def canon(self, name: str) -> str | None:
        return self.scope_names.get(name.strip().lower())

    def resolve(self, raw: RawRef, context: str, seen: frozenset[tuple[str, str]] | None = None) -> Resolved:
        seen = seen or frozenset()
        base, parts = _split_body(raw.text)
        if raw.program:
            scope = self.canon(raw.program) or raw.program
        elif context and context.lower() != "controller" and (context.lower(), base.lower()) in self.defined:
            scope = self.defined[(context.lower(), base.lower())]
        elif ("controller", base.lower()) in self.defined:
            scope = "Controller"
        else:
            scope = self.canon(context) or context or "Controller"
        key = (scope.lower(), base.lower())
        target = self.alias.get(key)
        if target and key not in seen:
            refs = _extract_refs(target)
            raw_target = refs[0] if refs else RawRef("", target)
            collapsed = self.resolve(raw_target, scope, seen | {key})
            return Resolved(collapsed.scope, collapsed.base, collapsed.parts + parts)
        return Resolved(scope, base, parts)

    def resolve_start(self, name: str, program: str) -> Resolved:
        needle = (name or "").strip()
        if not needle:
            raise ValueError("name is empty; pass a tag / symbol to trace")
        refs = _extract_refs(needle)
        if not refs:
            raise ValueError(f"name {needle!r} is not a tag reference")
        raw = refs[0]
        if raw.program:
            return self.resolve(raw, raw.program)
        chosen = (program or "").strip()
        if chosen:
            scope = self.canon(chosen)
            if scope is None:
                raise ValueError(f"program {chosen!r} was not found in the project")
            return self.resolve(raw, scope)
        base, _ = _split_body(raw.text)
        scopes = sorted({scope for (scope_l, tag_l), scope in self.defined.items() if tag_l == base.lower()})
        if len(scopes) > 1:
            raise ValueError(
                f"{base!r} is defined in multiple scopes: {', '.join(scopes)}. Pass program= to choose one."
            )
        return self.resolve(raw, scopes[0] if scopes else "Controller")

    def description(self, ref: Resolved) -> str:
        """Most specific description: bit/member comment, otherwise the tag description."""
        key = (ref.scope.lower(), ref.base.lower())
        operand = ref.operand().lower()
        if operand:
            comment = (self.comments.get(key) or {}).get(operand, "")
            if comment:
                return comment
        return self.descriptions.get(key, "")

    def caption(self, ref: Resolved) -> str:
        desc = self.description(ref)
        if not desc:
            return ref.display
        return f"{ref.display} — {desc}"

    def trace(
        self,
        start: Resolved,
        max_depth: int,
        max_origins: int,
        project_path: str,
        *,
        format: str = "both",
        diagram: bool = True,
        focus: str = "",
        from_box: str = "",
    ) -> str:
        walker = _Walker(self, max(0, int(max_depth)), max(1, int(max_origins)))
        walker.walk(start, 0, (), [(0, self.caption(start))], 1, into_zero=True)
        return _format_report(
            project_path,
            start,
            walker,
            format=format,
            diagram=diagram,
            focus=focus,
            from_box=from_box,
        )

    def _remember_scope(self, scope: str) -> None:
        self.scope_names[scope.lower()] = scope

    def _load_tags(self, root) -> None:
        for el in root.iter():
            if _local(el.tag) not in ("Tag", "LocalTag", "Parameter", "ConfigTag"):
                continue
            name = (el.get("Name") or "").strip()
            if not name:
                continue
            scope = _tag_scope(el)
            self._remember_scope(scope)
            self.defined[(scope.lower(), name.lower())] = scope
            alias_for = (el.get("AliasFor") or "").strip()
            if alias_for:
                self.alias[(scope.lower(), name.lower())] = alias_for
            desc, comments = _tag_texts(el)
            tag_key = (scope.lower(), name.lower())
            if desc:
                self.descriptions[tag_key] = desc
            if comments:
                self.comments[tag_key] = comments

    def _add_write(self, site: WriteSite) -> None:
        key = (site.dest.scope.lower(), site.dest.base.lower())
        self.writes.setdefault(key, []).append(site)

    def _add_gap(self, ref: Resolved, message: str) -> None:
        key = (ref.scope.lower(), ref.base.lower())
        self.gap_refs.setdefault(key, []).append((ref, message))

    def _load_logic(self, root) -> None:
        for routine in root.iter():
            if _local(routine.tag) != "Routine":
                continue
            program, routine_name = _scope_from_ancestor_chain(routine)
            if program == "—":
                program = "Controller"
            self._remember_scope(program)
            kind = (routine.get("Type") or "").upper()
            if kind in ("RLL", "LADDER", ""):
                self._load_rungs(routine, program, routine_name)
                if kind in ("RLL", "LADDER"):
                    continue
            if kind not in ("RLL", "LADDER"):
                self._load_other(routine, program, routine_name, kind or "logic")

    def _load_rungs(self, routine, program: str, routine_name: str) -> None:
        for el in routine.iter():
            if _local(el.tag) != "Rung":
                continue
            owner = el
            while owner is not None and _local(owner.tag) != "Routine":
                owner = owner.getparent()
            if owner is not routine:
                continue
            text = ""
            for child in el:
                if _local(child.tag) == "Text" and child.text:
                    text = child.text.strip()
                    break
            if not text:
                continue
            number = el.get("Number", "?")
            full = (_rung_text(el) or text).strip()
            self.rung_text[(program.lower(), routine_name.lower(), str(number))] = full
            series, _ = _parse_series(text, 0)
            self._index_series(series, [], program, routine_name, number)

    def _index_series(self, series: list, inherited: list[InstrNode], program: str, routine: str, rung: str) -> None:
        prefix = list(inherited)
        for node in series:
            if isinstance(node, BranchNode):
                for leg in node.legs:
                    self._index_series(leg, prefix, program, routine, rung)
                continue
            if not isinstance(node, InstrNode):
                continue
            self._index_instr(node, _conditions_for(series, node, inherited), program, routine, rung)
            if node.name in _CONDITION:
                prefix.append(node)

    def _index_instr(self, node: InstrNode, conditions: list[InstrNode], program: str, routine: str, rung: str) -> None:
        context_refs: list[tuple[str, RawRef]] = []
        for cond in conditions:
            blob = " ".join(cond.args) or cond.raw
            context_refs.extend((cond.name, raw) for raw in _extract_refs(blob))
        indexes = _dest_indexes(node.name, len(node.args))
        if node.name == "JSR" or indexes is None:
            blob = node.raw or " ".join(node.args)
            for raw in _extract_refs(blob):
                ref = self.resolve(raw, program)
                self._add_gap(
                    ref,
                    f"{node.name} {program}/{routine} rung {rung} references {raw.text}",
                )
            return
        dest_at = set(indexes)
        dests: list[tuple[int, Resolved, str]] = []
        sources: list[tuple[Resolved, str]] = []
        literals: list[str] = []
        for arg_i, arg in enumerate(node.args):
            if arg_i in dest_at:
                for raw in _extract_refs(arg):
                    dests.append((arg_i, self.resolve(raw, program), raw.text))
                continue
            if node.name in _LENGTH_THIRD and arg_i == 2 and _is_literal(arg):
                continue
            refs = _extract_refs(arg)
            if refs:
                for raw in refs:
                    sources.append((self.resolve(raw, program), raw.text))
                continue
            if _is_literal(arg):
                literals.append(arg.strip())
        feeders: list[tuple[str, Resolved, str]] = []
        seen: set[tuple] = set()
        for opcode, raw in context_refs:
            ref = self.resolve(raw, program)
            item = ("condition", ref, f"{opcode} {raw.text}")
            mark = ("condition", ref.key, opcode)
            if mark not in seen:
                seen.add(mark)
                feeders.append(item)
        for ref, label in sources:
            if ("source", ref.key) in seen:
                continue
            seen.add(("source", ref.key))
            feeders.append(("source", ref, label))
        implicit = ""
        if not sources and not literals:
            implicit = _IMPLICIT_VALUE.get(node.name, "")
        snippet = node.raw.replace("\n", " ")
        if len(snippet) > 160:
            snippet = snippet[:159] + "…"
        for _, dest, _label in dests:
            self._add_write(
                WriteSite(
                    program=program,
                    routine=routine,
                    rung=rung,
                    instruction=node.name,
                    snippet=snippet,
                    dest=dest,
                    feeders=list(feeders),
                    literals=list(literals),
                    implicit=implicit,
                )
            )

    def _load_other(self, routine, program: str, routine_name: str, kind: str) -> None:
        label = kind or "logic"
        for node in routine.iter():
            if _local(node.tag) in ("Comment", "Rung"):
                continue
            if _local(node.tag) not in ("Text", "LineText", "Operand"):
                continue
            body = (node.text or "").strip()
            if not body:
                continue
            parent = node.getparent()
            number = node.get("Number") or (parent.get("Number") if parent is not None else "") or ""
            where = f"line {number}" if number else "text"
            for raw in _extract_refs(body):
                ref = self.resolve(raw, program)
                self._add_gap(ref, f"{label} {program}/{routine_name} {where} references {raw.text}")


class _Walker:
    def __init__(self, index: OriginIndex, max_depth: int, max_origins: int) -> None:
        self.index = index
        self.max_depth = max_depth
        self.max_origins = max_origins
        self.origins: list[list[tuple[int, str]]] = []
        self.cycles: list[list[tuple[int, str]]] = []
        self.gap_lines: list[str] = []
        self.zero_notes: list[str] = []
        self._zero_seen: set[tuple] = set()
        self.boxes: list[RungBox] = []
        self._box_index: dict[tuple, str] = {}
        self._gap_seen: set[str] = set()
        self._paths: set[tuple] = set()
        self.cut_depth = 0
        self.cut_cap = 0
        self.steps = 0
        self.stopped = False

    def walk(
        self,
        resolved: Resolved,
        depth: int,
        stack: tuple,
        lines: list[tuple[int, str]],
        indent: int,
        *,
        into_zero: bool = False,
    ) -> list[str]:
        if self.stopped:
            return []
        key = resolved.key
        if key in stack:
            self._add_cycle(lines + [(indent, f"CYCLE {self.index.caption(resolved)}")])
            return []
        if depth > self.max_depth:
            self.cut_depth += 1
            return []
        self.steps += 1
        if self.steps > _MAX_STEPS:
            self.cut_cap += 1
            self.stopped = True
            return []
        self._note_gaps(resolved)
        writers = [
            site
            for site in self.index.writes.get((resolved.scope.lower(), resolved.base.lower()), [])
            if _covers(site.dest, resolved)
        ]
        if not writers:
            if self._gap_messages(resolved):
                return []
            self._add_origin(lines + [(indent, "ORIGIN no logic writer")])
            return []
        zero_sites = [site for site in writers if _is_zero_write(site)]
        live_sites = [site for site in writers if not _is_zero_write(site)]
        for site in zero_sites:
            self._note_zero(resolved, site)
        if not live_sites:
            # The clear itself is the cutoff. The rungs that feed it stay.
            if not into_zero:
                return []
            return self._walk_until_zero(resolved, zero_sites, depth, stack, lines, indent)
        stack2 = stack + (key,)
        box_ids: list[str] = []
        for site in live_sites:
            if self.stopped:
                return box_ids
            box_id, fresh = self._ensure_box(site, resolved)
            if box_id not in box_ids:
                box_ids.append(box_id)
            header = (
                indent,
                f"<- {site.instruction} {site.program}/{site.routine} rung {site.rung} {site.snippet}",
            )
            base = lines + [header]
            emitted = False
            value_sources = [item for item in site.feeders if item[0] == "source"]
            if site.implicit and not value_sources and not site.literals:
                self._add_origin(base + [(indent + 1, f"ORIGIN value {site.implicit}")])
                emitted = True
            for literal in site.literals:
                self._add_origin(
                    base + [(indent + 1, f"<- source literal {literal}"), (indent + 2, "ORIGIN literal")]
                )
                emitted = True
            if not fresh:
                continue
            box = self._box_by_id(box_id)
            for role, feeder, label in site.feeders:
                if self.stopped:
                    return box_ids
                feeder_lines = base + [
                    (indent + 1, f"<- {role} {label}"),
                    (indent + 2, self.index.caption(feeder)),
                ]
                if feeder.key in stack2:
                    self._add_cycle(feeder_lines + [(indent + 3, f"CYCLE {self.index.caption(feeder)}")])
                    child_ids: list[str] = []
                else:
                    child_ids = self.walk(feeder, depth + 1, stack2, feeder_lines, indent + 3)
                caption = self.index.caption(feeder)
                for child_id in child_ids:
                    link = (caption, child_id)
                    if link not in box.links:
                        box.links.append(link)
                emitted = True
            if not emitted:
                self._add_origin(base + [(indent + 1, f"ORIGIN value {site.instruction}")])
        return box_ids

    def _walk_until_zero(
        self,
        resolved: Resolved,
        sites: list[WriteSite],
        depth: int,
        stack: tuple,
        lines: list[tuple[int, str]],
        indent: int,
    ) -> list[str]:
        """Follow the contacts of a clear, and stop before the next clear."""
        stack2 = stack + (resolved.key,)
        box_ids: list[str] = []
        seen: set[str] = set()
        for site in sites:
            if self.stopped:
                return box_ids
            header = (
                indent,
                "truncated before "
                f"{site.instruction} {site.program}/{site.routine} rung {site.rung}",
            )
            for role, feeder, label in site.feeders:
                if self.stopped:
                    return box_ids
                if role != "condition":
                    continue
                feeder_lines = lines + [
                    header,
                    (indent + 1, f"<- {role} {label}"),
                    (indent + 2, self.index.caption(feeder)),
                ]
                if feeder.key in stack2:
                    self._add_cycle(
                        feeder_lines + [(indent + 3, f"CYCLE {self.index.caption(feeder)}")]
                    )
                    child_ids: list[str] = []
                else:
                    child_ids = self.walk(
                        feeder, depth + 1, stack2, feeder_lines, indent + 3, into_zero=False
                    )
                for child_id in child_ids:
                    if child_id in seen:
                        continue
                    seen.add(child_id)
                    box_ids.append(child_id)
        return box_ids

    def _note_zero(self, resolved: Resolved, site: WriteSite) -> None:
        mark = (resolved.key, site.program, site.routine, site.rung, site.instruction, site.snippet)
        if mark in self._zero_seen:
            return
        self._zero_seen.add(mark)
        verb = "clears to 0" if site.instruction == "CLR" else "writes literal 0 into"
        target = self.index.caption(resolved)
        note = f"{site.instruction} {site.program}/{site.routine} rung {site.rung} {verb} {target}"
        conditions = [
            self.index.caption(feeder)
            for role, feeder, _label in site.feeders
            if role == "condition"
        ]
        if conditions:
            note += " when " + "; ".join(conditions)
        self.zero_notes.append(note)

    def _ensure_box(self, site: WriteSite, resolved: Resolved) -> tuple[str, bool]:
        key = (site.program, site.routine, site.rung, site.instruction, resolved.key, site.snippet)
        existing = self._box_index.get(key)
        if existing is not None:
            return existing, False
        box_id = f"b{len(self.boxes)}"
        inputs: list[str] = []
        seen: set[str] = set()
        for _role, feeder, _label in site.feeders:
            caption = self.index.caption(feeder)
            if caption in seen:
                continue
            seen.add(caption)
            inputs.append(caption)
        full = self.index.rung_text.get(
            (site.program.lower(), site.routine.lower(), str(site.rung)),
            site.snippet,
        )
        box = RungBox(
            box_id=box_id,
            title=f"{site.program} / {site.routine} rung {site.rung}",
            dest=self.index.caption(resolved),
            inputs=inputs,
            links=[],
            program=site.program,
            routine=site.routine,
            rung=str(site.rung),
            instruction=site.instruction,
            text=full,
        )
        self.boxes.append(box)
        self._box_index[key] = box_id
        return box_id, True

    def _box_by_id(self, box_id: str) -> RungBox:
        for box in self.boxes:
            if box.box_id == box_id:
                return box
        raise KeyError(box_id)

    def _gap_messages(self, resolved: Resolved) -> list[str]:
        rows = self.index.gap_refs.get((resolved.scope.lower(), resolved.base.lower()), [])
        return [msg for ref, msg in rows if _covers(ref, resolved) or _covers(resolved, ref)]

    def _note_gaps(self, resolved: Resolved) -> None:
        for msg in self._gap_messages(resolved):
            if msg in self._gap_seen:
                continue
            self._gap_seen.add(msg)
            self.gap_lines.append(msg)

    def _add_origin(self, lines: list[tuple[int, str]]) -> bool:
        key = tuple(lines)
        if key in self._paths:
            return True
        self._paths.add(key)
        if len(self.origins) >= self.max_origins:
            self.cut_cap += 1
            self.stopped = True
            return False
        self.origins.append(lines)
        return True

    def _add_cycle(self, lines: list[tuple[int, str]]) -> None:
        key = tuple(lines)
        if key in self._paths:
            return
        self._paths.add(key)
        self.cycles.append(lines)


def _origin_label(path: list[tuple[int, str]]) -> str:
    texts = [text for _, text in path]
    last = texts[-1] if texts else ""
    if last == "ORIGIN no logic writer" and len(texts) >= 2:
        return f"{texts[-2]} (no logic writer)"
    if last == "ORIGIN literal" and len(texts) >= 2:
        return texts[-2].replace("<- source literal ", "literal ", 1)
    if last.startswith("ORIGIN value "):
        return last.replace("ORIGIN value ", "value ", 1)
    return last


def _render_path(path: list[tuple[int, str]]) -> list[str]:
    return ["  " + ("  " * indent) + text for indent, text in path]


_MERMAID_NODE_MAX = 160
# Subgraph titles size the Mermaid box; long lists overflow the border.
_MERMAID_SUBGRAPH_MAX = 48


def _mermaid_label(text: str, *, max_len: int = _MERMAID_NODE_MAX) -> str:
    cleaned = (
        text.replace("\\", "/")
        .replace('"', "'")
        .replace("|", "/")
        .replace("[", "(")
        .replace("]", ")")
        .replace("{", "(")
        .replace("}", ")")
        .replace("<", "")
        .replace(">", "")
        .replace("#", "")
        .replace(" — ", "<br/>")
    )
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


def _rung_span(rungs: list[str]) -> str:
    """Compact rung list: ``27-42`` when contiguous, else ``27,28,39…``."""
    nums: list[int] = []
    for item in rungs:
        try:
            nums.append(int(item))
        except ValueError:
            return ", ".join(rungs[:4]) + ("…" if len(rungs) > 4 else "")
    nums = sorted(set(nums))
    if not nums:
        return ""
    if len(nums) == 1:
        return str(nums[0])
    if nums[-1] - nums[0] + 1 == len(nums):
        return f"{nums[0]}-{nums[-1]}"
    shown = ",".join(str(n) for n in nums[:4])
    return shown + ("…" if len(nums) > 4 else "")


def _terminal_title(group: list[RungBox]) -> str:
    """One short title for several dead-end writes of the same tag."""
    if len(group) == 1:
        return group[0].title
    buckets: dict[str, list[str]] = {}
    order: list[str] = []
    for box in group:
        head = f"{box.program} / {box.routine}".strip(" /")
        if not head or head == "/":
            head, _, _rung = box.title.rpartition(" rung ")
        if head not in buckets:
            buckets[head] = []
            order.append(head)
        if box.rung and box.rung not in buckets[head]:
            buckets[head].append(box.rung)
    parts = []
    for head in order:
        rungs = buckets[head]
        if len(rungs) == 1:
            parts.append(f"{head} rung {rungs[0]}")
            continue
        # Prefer a short count so Mermaid subgraph borders are not blown out.
        routine = head.rsplit(" / ", 1)[-1] if " / " in head else head
        parts.append(f"{routine} · {len(rungs)} writers ({_rung_span(rungs)})")
    return "; ".join(parts)


def _mermaid_rungs(
    boxes: list[RungBox],
    root_label: str = "",
    *,
    show_start_root: bool = True,
) -> list[str]:
    """One subgraph per rung. Leaf inputs are omitted; only followed inputs are drawn.

    When ``show_start_root`` is false (``from_box`` views), do not attach the
    original start tag as ``nRoot`` — the selected box is already the root.
    """
    if not boxes:
        return []
    node_ids: dict[tuple[str, str, str], str] = {}
    edges: list[tuple[str, str]] = []
    continuing: dict[str, list[str]] = {}
    for box in boxes:
        linked = {caption for caption, _child in box.links}
        continuing[box.box_id] = [caption for caption in box.inputs if caption in linked]

    def node(box_id: str, role: str, label: str) -> str:
        key = (box_id, role, label)
        if key not in node_ids:
            node_ids[key] = f"n{len(node_ids)}"
        return node_ids[key]

    # Dead-end writes of one tag become one box, so a row of unlatches does not overlap.
    shown: list[tuple[str, str, RungBox, list[str]]] = []
    display_of: dict[str, str] = {}
    pending: dict[str, list[RungBox]] = {}
    for box in boxes:
        if continuing[box.box_id]:
            shown.append((box.box_id, box.title, box, continuing[box.box_id]))
            display_of[box.box_id] = box.box_id
        else:
            pending.setdefault(box.dest, []).append(box)
    for group in pending.values():
        display_id = group[0].box_id
        shown.append((display_id, _terminal_title(group), group[0], []))
        for box in group:
            display_of[box.box_id] = display_id

    lines = ["```mermaid", "flowchart TD"]
    targeted = {display_of[child] for box in boxes for _caption, child in box.links if child in display_of}
    root_ids = [display_of[box.box_id] for box in boxes if display_of.get(box.box_id) not in targeted]
    root_ids = list(dict.fromkeys(root_ids))
    draw_root = (
        show_start_root
        and bool(root_label)
        and bool(root_ids)
        and not any(box.dest == root_label for box in boxes)
    )
    if draw_root:
        lines.append(f'  nRoot["{_mermaid_label(root_label)}"]')
    dest_of: dict[str, str] = {}
    for display_id, title, box, inputs in shown:
        lines.append(
            f'  subgraph {display_id} ["{_mermaid_label(title, max_len=_MERMAID_SUBGRAPH_MAX)}"]'
        )
        dest_id = node(display_id, "dest", box.dest)
        dest_of[display_id] = dest_id
        lines.append(f'    {dest_id}["{_mermaid_label(box.dest)}"]')
        for caption in inputs:
            lines.append(f'    {node(display_id, "in", caption)}["{_mermaid_label(caption)}"]')
        lines.append("  end")
    for box in boxes:
        display_id = display_of[box.box_id]
        dest_id = dest_of[display_id]
        for caption in continuing[box.box_id]:
            edges.append((dest_id, node(display_id, "in", caption)))
        for caption, child_id in box.links:
            child_display = display_of.get(child_id)
            if child_display not in dest_of:
                continue
            if caption not in continuing[box.box_id]:
                continue
            edges.append((node(display_id, "in", caption), dest_of[child_display]))
    seen: set[tuple[str, str]] = set()
    for src, dst in edges:
        if src == dst or (src, dst) in seen:
            continue
        seen.add((src, dst))
        lines.append(f"  {src} --> {dst}")
    if draw_root:
        for display_id in root_ids:
            lines.append(f"  nRoot --> {dest_of[display_id]}")
    lines.append("```")
    return lines


_DRILL_DOWN = (
    "Drill-down: call get_rung or get_rung_diagram(path, program, routine, number) "
    "for a box title, or search_rungs / cross_reference on a node. focus= keeps the "
    "path(s) from the start tag to boxes that write that tag (multiple writers "
    "are multiple branches); it does not expand deeper origins below the focus. "
    "from_box= starts the diagram at that box (no original start-tag root) and "
    "keeps the origin boxes it links to. Set diagram=false on large trees."
)


def _filter_boxes(
    boxes: list[RungBox],
    *,
    focus: str = "",
    from_box: str = "",
) -> list[RungBox]:
    """Keep a branch: from_box descendants, and/or boxes matching focus plus neighbors."""
    if not boxes:
        return []
    by_id = {box.box_id: box for box in boxes}
    children: dict[str, list[str]] = {box.box_id: [] for box in boxes}
    parents: dict[str, list[str]] = {box.box_id: [] for box in boxes}
    for box in boxes:
        for _caption, child in box.links:
            if child not in by_id:
                continue
            children[box.box_id].append(child)
            parents[child].append(box.box_id)

    keep: set[str] = set()

    def add_descendants(start_id: str) -> None:
        stack = [start_id]
        while stack:
            current = stack.pop()
            if current in keep or current not in by_id:
                continue
            keep.add(current)
            stack.extend(children.get(current, []))

    def add_ancestors(start_id: str) -> None:
        stack = [start_id]
        seen: set[str] = set()
        while stack:
            current = stack.pop()
            if current in seen or current not in by_id:
                continue
            seen.add(current)
            keep.add(current)
            stack.extend(parents.get(current, []))

    needle = (from_box or "").strip().lower()
    if needle:
        if needle not in by_id:
            raise ValueError(f"from_box {from_box!r} was not found in this trace")
        add_descendants(needle)

    focus_text = (focus or "").strip().lower()
    if focus_text:
        # Match boxes that write the focused tag (dest only). Each match is
        # one writer branch. Keep the path from the start/root to that writer;
        # do not expand deeper origins below it, and do not match rung text or
        # leaf inputs (those falsely pull in every examine of the bit).
        matched = [box.box_id for box in boxes if focus_text in box.dest.lower()]
        if not matched and not keep:
            raise ValueError(f"focus {focus!r} matched no writer boxes in this trace")
        for box_id in matched:
            add_ancestors(box_id)
            keep.add(box_id)

    if not focus_text and not needle:
        return boxes

    selected = [box for box in boxes if box.box_id in keep]
    remapped: list[RungBox] = []
    for box in selected:
        remapped.append(
            RungBox(
                box_id=box.box_id,
                title=box.title,
                dest=box.dest,
                inputs=list(box.inputs),
                links=[(caption, child) for caption, child in box.links if child in keep],
                program=box.program,
                routine=box.routine,
                rung=box.rung,
                instruction=box.instruction,
                text=box.text,
            )
        )
    return remapped


def _structure_payload(
    project_path: str,
    start: Resolved,
    walker: _Walker,
    boxes: list[RungBox],
    unique: list[str],
    *,
    focus: str = "",
    from_box: str = "",
) -> dict:
    box_rows = []
    edges = []
    for box in boxes:
        box_rows.append(
            {
                "id": box.box_id,
                "program": box.program,
                "routine": box.routine,
                "rung": box.rung,
                "instruction": box.instruction,
                "dest": box.dest,
                "inputs": list(box.inputs),
                "links": [{"input": caption, "to": child} for caption, child in box.links],
                "text": box.text,
            }
        )
        for caption, child in box.links:
            edges.append({"from": box.box_id, "input": caption, "to": child})
    return {
        "start": walker.index.caption(start),
        "scope": start.scope,
        "project": project_path or "(L5X)",
        "focus": focus or None,
        "from_box": from_box or None,
        "origins": unique,
        "origin_paths": len(walker.origins),
        "cycles": len(walker.cycles),
        "cut_depth": walker.cut_depth,
        "cut_cap": walker.cut_cap,
        "zero_writes": list(walker.zero_notes),
        "gaps": list(walker.gap_lines),
        "boxes": box_rows,
        "edges": edges,
        "drill_down": _DRILL_DOWN,
    }


def _format_report(
    project_path: str,
    start: Resolved,
    walker: _Walker,
    *,
    format: str = "both",
    diagram: bool = True,
    focus: str = "",
    from_box: str = "",
) -> str:
    fmt = (format or "both").strip().lower()
    if fmt not in {"text", "json", "both"}:
        raise ValueError("format must be text, json, or both")
    boxes = _filter_boxes(walker.boxes, focus=focus, from_box=from_box)
    unique: list[str] = []
    seen: set[str] = set()
    for path in walker.origins:
        label = _origin_label(path)
        if label not in seen:
            seen.add(label)
            unique.append(label)
    payload = _structure_payload(
        project_path,
        start,
        walker,
        boxes,
        unique,
        focus=focus,
        from_box=from_box,
    )
    if fmt == "json":
        return json.dumps(payload, indent=2) + "\n"

    where = project_path or "(L5X)"
    lines = [
        f"[OK] trace_origin for '{walker.index.caption(start)}' ({start.scope}) in {where}",
        f"Origins: {len(walker.origins)}",
        f"Cycles: {len(walker.cycles)}",
        f"Cut off: depth={walker.cut_depth} cap={walker.cut_cap}",
        "Engine: offline L5X ladder (not Studio xref.dll). Copy a locked ACD before tracing it.",
        "ST, FBD/SFC, JSR, and unknown instructions are gaps and are not followed.",
        _DRILL_DOWN,
    ]
    if focus or from_box:
        parts = []
        if focus:
            parts.append(f"focus={focus!r}")
        if from_box:
            parts.append(f"from_box={from_box!r}")
        lines.append(f"Filtered to {len(boxes)} box(es) ({', '.join(parts)}).")
    if not walker.origins and walker.gap_lines and not walker.zero_notes:
        lines.append("No proved ladder writer. Unresolved references may hide a write.")
    lines.append("")
    if walker.zero_notes:
        lines.append("Literal 0 writes (not in the diagram):")
        lines.extend(f"  {note}" for note in walker.zero_notes)
        lines.append("")
    lines.append("Unique origins:")
    if unique:
        lines.extend(f"  {item}" for item in unique)
    else:
        lines.append("  (none)")
    lines.append("")
    if diagram:
        graph = _mermaid_rungs(
            boxes,
            walker.index.caption(start),
            show_start_root=not bool((from_box or "").strip()),
        )
        if graph:
            lines.append(
                "Each box is one rung. The first node is the tag that rung writes. "
                "Other nodes are inputs that continue to another rung. Inputs that "
                "stop at this rung are left out. The branch is cut off before a CLR "
                "or a literal 0, which is listed above and is not drawn."
            )
            if (from_box or "").strip():
                lines.append(
                    f"from_box={from_box!r}: diagram starts at that box "
                    "(original start tag omitted)."
                )
            lines.append("")
            lines.extend(graph)
            lines.append("")
    if focus or from_box:
        lines.append("Rungs in this branch:")
        for box in boxes:
            loc = f"{box.program}/{box.routine} rung {box.rung}"
            lines.append(f"  [{box.box_id}] {box.instruction} {loc}")
            lines.append(f"    dest: {box.dest}")
            if box.text:
                lines.append(f"    {box.text}")
        lines.append("")
    for number, path in enumerate(walker.origins, 1):
        lines.append(f"--- Path {number}")
        lines.extend(_render_path(path))
        lines.append("")
    if walker.cycles:
        lines.append("Cycles:")
        shown = walker.cycles[:20]
        for number, path in enumerate(shown, 1):
            lines.append(f"--- Cycle {number}")
            lines.extend(_render_path(path))
            lines.append("")
        extra = len(walker.cycles) - len(shown)
        if extra > 0:
            lines.append(f"... {extra} more cycles")
            lines.append("")
    if walker.gap_lines:
        lines.append("Gaps:")
        shown_gaps = walker.gap_lines[:40]
        lines.extend(f"  {gap}" for gap in shown_gaps)
        extra_gaps = len(walker.gap_lines) - len(shown_gaps)
        if extra_gaps > 0:
            lines.append(f"  ... {extra_gaps} more gaps")
    text = "\n".join(lines).rstrip() + "\n"
    if fmt == "text":
        return text
    return text + "\n```json\n" + json.dumps(payload, indent=2) + "\n```\n"


def trace_origin_l5x(
    root,
    name: str,
    *,
    program: str = "",
    max_depth: int = 8,
    max_origins: int = 50,
    project_path: str = "",
    format: str = "both",
    diagram: bool = True,
    focus: str = "",
    from_box: str = "",
) -> str:
    """Trace write feeders of ``name`` back to literals and unwritten tags."""
    index = OriginIndex(root)
    start = index.resolve_start(name, program)
    return index.trace(
        start,
        max_depth,
        max_origins,
        project_path,
        format=format,
        diagram=diagram,
        focus=focus,
        from_box=from_box,
    )


def _refs_from_rung_operands(text: str) -> list[RawRef]:
    """Tag references from instruction operands only (not opcode names like XIC)."""
    body = (text or "").strip()
    if body.endswith(";"):
        body = body[:-1]
    series, _ = _parse_series(body, 0)
    refs: list[RawRef] = []

    def walk(nodes: list) -> None:
        for node in nodes:
            if isinstance(node, InstrNode):
                for arg in node.args:
                    refs.extend(_extract_refs(arg))
            elif isinstance(node, BranchNode):
                for leg in node.legs:
                    walk(leg)

    walk(series)
    return refs


def _unscoped_display(ref: Resolved) -> str:
    text = ref.base
    for part in ref.parts:
        if part.kind == "member":
            text += "." + part.value
        else:
            text += "[" + (part.value if part.value else "*") + "]"
    return text


def rung_description_lookup(root, program: str, text: str) -> dict[str, str]:
    """Map operand / display forms → description for ladder caption lookup."""
    index = OriginIndex(root, logic=False)
    context = (program or "").strip() or "Controller"
    out: dict[str, str] = {}
    for raw in _refs_from_rung_operands(text or ""):
        ref = index.resolve(raw, context)
        desc = index.description(ref)
        if not desc:
            continue
        for key in (raw.text, _unscoped_display(ref), ref.display):
            k = key.strip().lower()
            if k and k not in out:
                out[k] = desc
    return out


def rung_tag_descriptions(root, program: str, text: str) -> list[tuple[str, str]]:
    """Unique tag references in rung text with L5X bit/member or tag descriptions.

    Order follows first appearance in the neutral text. Uses tag catalog only
    (no write-site walk).
    """
    index = OriginIndex(root, logic=False)
    context = (program or "").strip() or "Controller"
    rows: list[tuple[str, str]] = []
    seen: set[tuple] = set()
    for raw in _refs_from_rung_operands(text or ""):
        ref = index.resolve(raw, context)
        key = ref.key
        if key in seen:
            continue
        seen.add(key)
        rows.append((ref.display, index.description(ref)))
    return rows


def format_rung_tag_table(rows: list[tuple[str, str]]) -> str:
    """Markdown table of ``(tag, description)`` rows for ``get_rung_diagram``."""
    if not rows:
        return "_No tag references found in this rung._"
    lines = [
        "| Tag | Description |",
        "| --- | --- |",
    ]
    for tag, desc in rows:
        tag_cell = (tag or "").replace("|", "\\|")
        desc_cell = (desc or "(none)").replace("|", "\\|").replace("\n", " ")
        lines.append(f"| `{tag_cell}` | {desc_cell} |")
    return "\n".join(lines)
