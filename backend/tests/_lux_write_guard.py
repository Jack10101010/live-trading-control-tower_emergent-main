"""Static analysis: does this source write to the LIVE governed engine tree?

ONE analysis, two callers. `test_oracle_s1.py` scans the whole repository;
`test_engine_identity.py` scans the test suite. They used to be separate
implementations and the second was line-based — it looked for a write verb and
the token `LUX_ROOT` ON THE SAME LINE, which is exactly what the original defect
did not do:

    target = LUX_ROOT / "strategy_core" / "execution.py"   # taint here…
    original = target.read_bytes()
    try:
        target.write_bytes(original + b"probe")            # …write five lines later
    finally:
        target.write_bytes(original)

Neither write line contains `LUX_ROOT`, so a line-local scan scores zero. That is
measured, not asserted — see `test_a_line_local_scan_would_miss_the_defect`.

TWO GRADES OF TAINT, because one grade cannot express the rule that matters:

  ROOT-ANCHORED  the path is anchored at the live tree (`LUX_ROOT / …`, or a
                 name bound to such an expression). NEVER excusable. A comment
                 that could excuse these would be a licence to write to the live
                 engine, and "it restores the bytes afterwards" is precisely the
                 reasoning that would be written there.
  tree-derived   the expression merely MENTIONS something derived from the tree —
                 typically a RELATIVE path from `.relative_to(LUX_ROOT)` joined
                 onto a tmp_path. That is how a legitimate mirror is built, so it
                 may be excused by a marker on the ENCLOSING FUNCTION.

The marker is function-scoped. It used to be file-scoped, which meant one audited
helper silenced an entire module.
"""

from __future__ import annotations

import ast
import re

#: A file that both names the production engine tree AND writes must say why it
#: is safe. Deliberately ugly so it cannot be added absent-mindedly.
LUX_WRITE_MARKER = "# lux-write-audited:"

#: Path METHODS that mutate the filesystem.
WRITE_METHODS = frozenset({
    "write_text", "write_bytes", "mkdir", "makedirs", "touch", "unlink",
    "rmdir", "rename", "replace", "chmod", "lchmod", "symlink_to",
    "hardlink_to", "open",          # Path.open("w") — mode checked below
})

#: Functions that mutate a path handed to them FIRST.
WRITE_FUNCS = frozenset({
    "remove", "unlink", "rename", "replace", "rmtree", "makedirs", "mkdir",
    "removedirs", "rmdir", "utime", "chmod", "truncate", "link", "symlink",
})

#: Functions whose DESTINATION is the SECOND positional argument (or `dst=`).
#: Flagging the first would condemn every legitimate READ of the engine tree,
#: which is how a mirror is built.
WRITE_FUNCS_DST = frozenset({
    "copy", "copy2", "copyfile", "copytree", "copystat", "copymode", "move",
})

OPEN_WRITE_MODES = ("w", "a", "x", "+")

#: The ONLY unambiguous marker of the live tree. `lux_root` is deliberately NOT
#: here: tests routinely point `cfg.lux_root` at a synthetic tree under tmp_path
#: and write into it, which is correct and must not be flagged.
LUX_LITERAL = "Lux-OB-Backtester"

#: What a reader would grep for; used only to decide whether a file is in scope.
LUX_REFS = (LUX_LITERAL, "lux_root", "LUX_ROOT")


def audited_functions(src: str) -> set:
    """Functions carrying an audited marker in the comment block immediately
    above their `def`."""
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return set()
    lines = src.splitlines()
    out = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        i = node.lineno - 2
        while i >= 0 and lines[i].lstrip().startswith("#"):
            if LUX_WRITE_MARKER in lines[i]:
                out.add(node.name)
                break
            i -= 1
    return out


def write_offenders(src: str, with_meta: bool = False):
    """Write calls whose TARGET provably derives from the live engine tree."""
    try:
        tree = ast.parse(src)
    except SyntaxError:                       # not our problem to police
        return []

    def seg(node):
        return ast.get_source_segment(src, node) or ""

    def is_tainted(text, tainted):
        if LUX_LITERAL in text:
            return True
        return any(re.search(rf"\b{re.escape(n)}\b", text) for n in tainted)

    def leftmost(node):
        """The ANCHOR of a path expression: `a / b / c` -> `a`.

        Path joining is left-associative, so the leftmost operand decides where
        the result actually lives. This is what separates `LUX_ROOT / rel` from
        `dest / rel` even though both mention tree-derived material.
        """
        while isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            node = node.left
        return node

    def is_root(node, roots):
        anchor = leftmost(node)
        text = seg(anchor)
        if LUX_LITERAL in text:
            return True
        # A `.relative_to(...)` result is RELATIVE — it carries the tree's name
        # but is anchored nowhere.
        if isinstance(anchor, ast.Call) and \
                isinstance(anchor.func, ast.Attribute) and \
                anchor.func.attr == "relative_to":
            return False
        if isinstance(anchor, ast.Attribute) and anchor.attr == "parent":
            return is_root(anchor.value, roots)
        if isinstance(anchor, ast.Call) and \
                isinstance(anchor.func, ast.Attribute) and \
                anchor.func.attr in ("resolve", "absolute", "expanduser"):
            return is_root(anchor.func.value, roots)
        return any(re.fullmatch(rf"\s*{re.escape(n)}\s*", text) for n in roots)

    def propagate(body, tainted):
        tainted = set(tainted)
        for _ in range(4):                    # cheap fixpoint; depth is tiny
            before = len(tainted)
            for stmt in body:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Assign) and is_tainted(seg(n.value), tainted):
                        for t in n.targets:
                            if isinstance(t, ast.Name):
                                tainted.add(t.id)
                    elif isinstance(n, (ast.For, ast.AsyncFor)) and \
                            is_tainted(seg(n.iter), tainted):
                        if isinstance(n.target, ast.Name):
                            tainted.add(n.target.id)
            if len(tainted) == before:
                break
        return tainted

    def propagate_roots(body, roots):
        roots = set(roots)
        for _ in range(4):
            before = len(roots)
            for stmt in body:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Assign) and is_root(n.value, roots):
                        for t in n.targets:
                            if isinstance(t, ast.Name):
                                roots.add(t.id)
            if len(roots) == before:
                break
        return roots

    def mode_writes(node):
        """`open(p, "w")` / `p.open("a")` — is the mode a writing one?"""
        modes = [a.value for a in node.args
                 if isinstance(a, ast.Constant) and isinstance(a.value, str)]
        modes += [k.value.value for k in node.keywords
                  if k.arg == "mode" and isinstance(k.value, ast.Constant)
                  and isinstance(k.value.value, str)]
        if not modes:
            return False                      # no mode given -> "r"
        return any(ch in " ".join(modes) for ch in OPEN_WRITE_MODES)

    module_level = [s for s in tree.body
                    if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                          ast.ClassDef))]
    base = propagate(module_level, set())
    base_roots = propagate_roots(module_level, set())

    def scan(body, tainted, roots, func):
        found = []

        def hit(node, target_node, label):
            found.append({"line": node.lineno, "func": func, "text": label,
                          "root": is_root(target_node, roots)})

        for stmt in body:
            for n in ast.walk(stmt):
                if not isinstance(n, ast.Call):
                    continue
                if isinstance(n.func, ast.Name) and n.func.id == "open" \
                        and n.args and is_tainted(seg(n.args[0]), tainted) \
                        and mode_writes(n):
                    hit(n, n.args[0], f"open({seg(n.args[0])[:40]}, <write mode>)")
                    continue
                if not isinstance(n.func, ast.Attribute):
                    continue
                attr = n.func.attr
                # NOT elif: several names are BOTH a Path method and an `os`
                # function — `unlink`, `rename`, `replace`, `mkdir`, `makedirs`.
                # Chaining them meant `os.unlink(LUX_ROOT / "a")` was tested
                # against its RECEIVER (`os`, never tainted) and the argument was
                # never looked at.
                if attr in WRITE_METHODS:
                    recv = seg(n.func.value)
                    if is_tainted(recv, tainted) and not (
                            attr == "open" and not mode_writes(n)):
                        hit(n, n.func.value, f"{recv[:50]}.{attr}(...)")
                if attr in WRITE_FUNCS and n.args:
                    if is_tainted(seg(n.args[0]), tainted):
                        hit(n, n.args[0],
                            f"{seg(n.func)}({seg(n.args[0])[:40]}...)")
                if attr in WRITE_FUNCS_DST:
                    dst = n.args[1] if len(n.args) > 1 else next(
                        (k.value for k in n.keywords if k.arg in ("dst", "dest")),
                        None)
                    if dst is not None and is_tainted(seg(dst), tainted):
                        hit(n, dst, f"{seg(n.func)}(..., {seg(dst)[:40]})")
        return found

    out = scan(module_level, base, base_roots, "<module>")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out += scan(node.body, propagate(node.body, base),
                        propagate_roots(node.body, base_roots), node.name)

    seen, uniq = set(), []
    for h in sorted(out, key=lambda x: (x["line"], x["text"])):
        key = (h["line"], h["text"])
        if key not in seen:
            seen.add(key)
            uniq.append(h)
    if with_meta:
        return uniq
    return [f"line {h['line']}: {h['text']}" for h in uniq]


def unexcused(src: str):
    """Offenders a marker cannot excuse: everything ROOT-ANCHORED, plus anything
    tree-derived in a function nobody audited."""
    audited = audited_functions(src)
    return [h for h in write_offenders(src, with_meta=True)
            if h["root"] or h["func"] not in audited]
