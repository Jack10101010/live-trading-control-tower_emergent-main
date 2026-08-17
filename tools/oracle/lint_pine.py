"""Static validation of the generated Pine artefact.

    python -m tools.oracle.lint_pine [--file PATH]

TradingView cannot be driven headlessly from this VPS, so the generated script
cannot be COMPILED here. This linter is the honest substitute: it catches the
structural and semantic mistakes that are mechanically detectable, and it makes
no claim beyond that. A clean run means "no detectable defect", NOT "compiles".

The checks are not generic Pine style rules — each one encodes a mistake that
would silently corrupt parity in THIS oracle:

  * a global `array.push` (re-executes every bar; the array grows without bound)
  * `request.security(..., "D", ...)` / `timeframe.change("D")` (exchange-session
    day instead of the production UTC calendar day — limitation L-06)
  * a calendar call without an explicit "UTC" argument (silently uses the chart's
    timezone)
  * a `<=`/`<` slip in the session window test (production is [start, end))
  * a suppressible parity warning
  * an absolute path or wall-clock stamp embedded in a generated file
"""

from __future__ import annotations

#: TradingView's hard ceiling on plot-family calls (RE10140).
PLOT_LIMIT = 64
#: Slots that must stay free after any wave. Hitting the ceiling is not a
#: compile error — it is a RUNTIME error on a live chart, discovered by an
#: operator, so the budget has to fail in CI while there is still room to pack.
#: Two is the minimum a stage needs to add one packed code plot and one float.
PLOT_RESERVE = 2
#: Warn once fewer than this many slots remain — earlier than the hard reserve,
#: so a wave that is heading for the ceiling is visible before it arrives.
PLOT_HEADROOM = 6

import argparse
import re
import sys
from pathlib import Path

from tools.oracle.engine_access import CT_ROOT

DEFAULT_FILE = CT_ROOT / "pine" / "generated" / "tradingview_visual_oracle.pine"

# Calendar functions that MUST carry an explicit timezone argument.
_TZ_FUNCS = ("year", "month", "dayofmonth", "hour", "minute", "second",
             "dayofweek", "weekofyear")


def _strip_comments(src: str) -> list[tuple[int, str]]:
    """(lineno, code) with only // comments removed — string literals INTACT.

    Rules that must inspect a literal (e.g. the forbidden `"D"` timeframe) have
    to run against this form. Blanking strings first would silently defeat them,
    which is exactly the bug the negative tests caught.
    """
    out = []
    for i, raw in enumerate(src.splitlines(), 1):
        # Find a // that is not inside a string literal.
        in_str, cut = False, None
        j = 0
        while j < len(raw):
            ch = raw[j]
            if ch == "\\" and in_str:
                j += 2
                continue
            if ch == '"':
                in_str = not in_str
            elif not in_str and raw.startswith("//", j):
                cut = j
                break
            j += 1
        out.append((i, raw if cut is None else raw[:cut]))
    return out


def _strip_comments_and_strings(src: str) -> list[tuple[int, str]]:
    """(lineno, code) with // comments removed AND "..." literals blanked.

    Needed so a rule that legitimately MENTIONS a forbidden pattern inside a
    string (banner text, tooltips) is not flagged as using it.
    """
    return [(n, re.sub(r'"(?:[^"\\]|\\.)*"', '""', l)) for n, l in _strip_comments(src)]


#: Build targets that are `strategy()` scripts rather than `indicator()`
#: ones. There is exactly one, and it is the whole reason this distinction
#: exists: the Strategy Tester will not run an indicator, and the Visual Oracle
#: must never place an order. Rules that encode "the oracle is an indicator"
#: are therefore scoped to the builds that ARE oracles, rather than deleted —
#: deleting them would let a future edit put `strategy.entry` into the oracle
#: with nothing to object.
STRATEGY_TARGETS = {"strategy_companion"}


def build_target(src: str) -> str:
    """Which build this file claims to be, read from its generated header.

    The header is generated and covered by the source hash, so a file cannot
    quietly claim to be a different target than it is: `check_freshness`
    compares the hash and reports INCOMPATIBLE.
    """
    m = re.search(r"^// Build target\s*:\s*(\S+)", src, re.M)
    return m.group(1) if m else "detection_15m"


def lint(src: str) -> list[dict]:
    findings: list[dict] = []
    target = build_target(src)
    is_strategy = target in STRATEGY_TARGETS
    code = _strip_comments_and_strings(src)   # literals blanked
    lit = _strip_comments(src)                # literals intact

    def add(sev, rule, lineno, detail):
        findings.append({"severity": sev, "rule": rule, "line": lineno, "detail": detail})

    # ── CE10156: end of line without line continuation ───────────────────────
    #
    # Pine continues a statement onto the next line only while that line is
    # indented MORE THAN THE LINE THAT STARTED THE STATEMENT. So a line ending
    # in a binary operator, followed by a line at the SAME indent as the
    # statement's first line, is two statements — the first trailing an
    # operator. The editor says "end of line without line continuation"; from
    # the source it looks entirely reasonable, which is why it needs a rule.
    #
    # Relative to the STATEMENT START, not to the preceding line. A first
    # attempt compared against the preceding line and flagged sixteen healthy
    # continuations — an expression may indent 9 then 5 and both continue a
    # statement that began at 4.
    #
    # Skipped inside brackets, where Pine allows newlines freely: the identical
    # shape is fatal in a function body and fine in a `tooltip = …` argument.
    CONT_OPS = ("+", "-", "*", "/", "?", ":", "%", ":=", "==", "!=", ">=",
                "<=", ">", "<", "and", "or")
    BLOCK_END = ("=>",)
    depth = 0
    stmt_start = None
    prev = None               # (lineno, indent) of the last line, if it trails an op
    for n, l in code:
        body = l.strip()
        if not body:
            continue
        indent = len(l) - len(l.lstrip())
        if depth == 0:
            if stmt_start is None or indent <= stmt_start:
                if prev is not None:
                    add("error", "line_continuation", prev[0],
                        f"line ends with an operator but line {n} is indented "
                        f"{indent}, not deeper than the statement that began at "
                        f"column {stmt_start} — Pine reads them as two "
                        "statements (CE10156). Indent the continuation, or move "
                        "the right-hand side onto the same line.")
                stmt_start = indent
        opened = depth
        depth += l.count("(") + l.count("[") - l.count(")") - l.count("]")
        prev = None
        if opened == 0 and depth == 0:
            # A header (`… =>`, `if …`) opens a BLOCK, not a continuation, so
            # its body gets its own statement start.
            if body.endswith(BLOCK_END) or re.match(
                    r"(if|else|for|while|switch)", body):
                # A block header does not START a statement at its own indent —
                # its BODY does, one level in. Leaving it at the header's indent
                # made every body line look like a continuation and the rule
                # caught nothing at all.
                stmt_start = None
            else:
                for op in CONT_OPS:
                    if body.endswith(op):
                        prev = (n, indent)
                        break

    # ── structure ────────────────────────────────────────────────────────────
    if not re.search(r"^//@version=6\s*$", src, re.M):
        add("error", "version", 0, "missing `//@version=6`")
    ind = [n for n, l in code if re.match(r"\s*indicator\s*\(", l)]
    strat = [n for n, l in code if re.match(r"\s*strategy\s*\(", l)]
    want, got, other = (("strategy", strat, ind) if is_strategy
                        else ("indicator", ind, strat))
    if len(got) != 1:
        add("error", "declaration", got[0] if got else 0,
            f"expected exactly one {want}() declaration, found {len(got)}")
    if other:
        add("error", "declaration_kind", other[0],
            f"build target {target!r} must be a {want}(), but the file declares "
            f"{'an indicator' if is_strategy else 'a strategy'}()")
    if not is_strategy and re.search(r"^\s*strategy\.", src, re.M):
        add("error", "not_a_strategy", 0,
            "strategy.* found — an oracle build is an INDICATOR; it must never "
            "place orders")

    # ── CE10123: input.time needs a CONST int ────────────────────────────────
    #
    # `timestamp("UTC", 2026, 6, 23, 0, 0)` is a SIMPLE int; only
    # `timestamp("<iso>")` folds to a const. Everything else about the two forms
    # is identical, which is what makes the wrong one so easy to write — and the
    # compiler is the only other thing that will tell you.
    GOOD_DEFVAL = re.compile(r'input\.time\s*\(\s*timestamp\s*\(\s*"[^"]*"\s*\)')
    for n, l in lit:
        if "input.time" in l and not GOOD_DEFVAL.search(l):
            add("error", "input_time_not_const", n,
                "`input.time` requires a CONST int default, and only the "
                "single-string form gives one: "
                'timestamp("2026-06-23T00:00:00+0000"). The multi-argument '
                "timestamp(\"UTC\", y, m, d, …) is a SIMPLE int and "
                "TradingView rejects it with CE10123.")

    # ── CE10197: a bare literal is not a statement ───────────────────────────
    #
    # Runs against `lit` (literals INTACT) for the same reason the day-semantics
    # rule does: the defect IS the literal, so the blanked view cannot see it.
    prev_code = ""
    for n, l in ((n, l) for n, l in lit if l.strip()):
        body = l.split("//")[0].rstrip()
        if not body.strip():
            continue
        s = body.strip()
        pure = (len(s) >= 2 and s[0] in "'\"" and s[-1] == s[0]
                and s[1:-1].count(s[0]) == 0)
        if pure and not prev_code.endswith(CONT_OPS):
            add("error", "bare_literal", n,
                f"`{s}` is a statement that is only a literal — TradingView "
                f"rejects this with CE10197 \"is not a valid statement\". It is "
                f"usually the residue of a deleted line.")
        prev_code = s

    # ── the array.push trap ──────────────────────────────────────────────────
    for n, l in code:
        if re.match(r"\s*array\.push\s*\(", l) and not l.startswith((" ", "\t")):
            add("error", "global_array_push", n,
                "top-level array.push re-executes every bar and grows the array "
                "without bound — build arrays with array.from() inside a var")

    # ── forbidden day semantics (limitation L-06) ────────────────────────────
    # Runs against `lit` (literals INTACT): the defect IS the "D" literal, so a
    # string-blanked view can never see it.
    for n, l in lit:
        if re.search(r'request\.security\s*\([^)]*["\']D["\']', l):
            add("error", "exchange_day", n,
                'request.security(..., "D", ...) uses the EXCHANGE session day; '
                "production uses the UTC calendar day")
        if re.search(r'timeframe\.change\s*\(\s*["\']D["\']', l):
            add("error", "exchange_day", n,
                'timeframe.change("D") uses the exchange session day')
        if "dayofweek" in l and "UTC" not in l:
            add("warn", "weekday_use", n,
                "production session assignment has NO weekday logic; a weekday "
                "read here needs justification")

    # ── forbidden built-in substitutions (S6) ────────────────────────────────
    # Each of these LOOKS like the production indicator and is not. Measured:
    #   ta.ema   seeds with an SMA of the first `length` values; production's
    #            ewm(adjust=False) seeds with the FIRST value.
    #   ta.stdev is a POPULATION deviation; production's rolling().std() is
    #            ddof=1, a SAMPLE deviation — sqrt(n/(n-1)) apart, ~2.6% at 20,
    #            which is thousands of times the BBW threshold's own margin.
    #   ta.adx   smooths with an RMA seeded from a sum; production reuses the
    #            same first-value-seeded ewm at alpha = 1/length.
    _FORBIDDEN_BUILTINS = {
        "ta.ema": "seeds with an SMA; production's ewm(adjust=False) seeds with "
                  "the first value",
        "ta.stdev": "is a POPULATION deviation; production uses ddof=1 (SAMPLE)",
        "ta.variance": "is a POPULATION variance; production uses ddof=1",
        "ta.adx": "uses RMA seeded from a sum; production uses a first-value "
                  "seeded ewm at alpha=1/length",
        "ta.dmi": "returns ta.adx's smoothing, which production does not use",
    }
    for n, l in code:
        for fn, why in _FORBIDDEN_BUILTINS.items():
            if re.search(rf"(?<![\w.]){re.escape(fn)}\s*\(", l):
                add("error", "forbidden_builtin", n,
                    f"{fn}() {why} — hand-roll it")

    # ── TradingView's plot ceiling ───────────────────────────────────────────
    # A script may create at most 64 plots. Exceeding it raises RE10140 at
    # RUNTIME on a live chart — the compiler does not catch it, and this linter
    # did not either until the build shipped 82 and the chart went blank.
    # `plot`, `plotshape`, `plotchar`, `plotarrow`, `plotcandle` and `plotbar`
    # all count against the same budget.
    _PLOT_FAMILY = ("plot", "plotshape", "plotchar", "plotarrow", "plotcandle",
                    "plotbar")
    plot_calls = 0
    for _n, l in code:
        for fn in _PLOT_FAMILY:
            plot_calls += len(re.findall(rf"(?<![\w.]){fn}\s*\(", l))
    if plot_calls > PLOT_LIMIT:
        add("error", "plot_limit", 0,
            f"{plot_calls} plot-family calls exceed TradingView's limit of "
            f"{PLOT_LIMIT} — RE10140 at RUNTIME, the whole indicator stops "
            "rendering. Pack small-integer fields into a composite plot rather "
            "than dropping exported evidence.")
    elif plot_calls > PLOT_LIMIT - PLOT_RESERVE:
        add("error", "plot_limit", 0,
            f"{plot_calls} of {PLOT_LIMIT} plot slots used — fewer than "
            f"{PLOT_RESERVE} free. The reserve is not spare capacity: it is what "
            "the next stage needs to add one packed code plot and one float, and "
            "spending it means the ceiling is discovered on a live chart instead "
            "of here.")
    elif plot_calls > PLOT_LIMIT - PLOT_HEADROOM:
        add("warn", "plot_limit", 0,
            f"{plot_calls} of {PLOT_LIMIT} plot slots used — fewer than "
            f"{PLOT_HEADROOM} remain for the next stage")

    # ── global assignment inside a function (CE10088) ────────────────────────
    #
    # Pine forbids `x := y` on a GLOBAL scalar from inside a function body:
    #     "Cannot modify global variable 'x' in function"
    # It is a COMPILE error, so it never reaches a chart — but it also never
    # reaches this linter's other rules, and it cost a full paste-and-fail cycle.
    # Mutating a global COLLECTION is fine (`array.push`), which is why the
    # retention helpers are legal and this rule only looks at `:=`.
    #
    # Function bodies are found by indentation: a `f_name(...) =>` header, then
    # every more-indented line until the indentation returns to column 0.
    globals_declared = set()
    for _n, l in code:
        m = re.match(r"\s*(?:var\s+)?(?:float|int|bool|string|color|line|label|"
                     r"box|table)?\s*([A-Za-z_]\w*)\s*(?::=|=)\s*\S", l)
        if m and not l.startswith((" ", "\t")):
            globals_declared.add(m.group(1))

    in_func, func_name, func_line = False, "", 0
    for n, l in code:
        if not l.strip():
            continue
        header = re.match(r"^([A-Za-z_]\w*)\s*\([^)]*\)\s*=>", l)
        if header and not l.startswith((" ", "\t")):
            in_func, func_name, func_line = True, header.group(1), n
            continue
        if in_func and not l.startswith((" ", "\t")):
            in_func = False
        if in_func:
            m = re.match(r"\s*([A-Za-z_]\w*)\s*:=", l)
            if m and m.group(1) in globals_declared:
                add("error", "global_assign_in_function", n,
                    f"`{m.group(1)} := …` inside function `{func_name}` "
                    f"(declared line {func_line}) — Pine raises CE10088 "
                    '"Cannot modify global variable in function" and the script '
                    "will not compile. Return the value and assign it at the "
                    "call site; mutating a global COLLECTION is fine, "
                    "reassigning a global SCALAR is not.")

    # ── tuple arity (CE10172) ────────────────────────────────────────────────
    #
    # `[a, b, c] = f(...)` requires `f` to END with a 3-element tuple at function
    # -body indentation. Get it wrong — omit the line, or nest it one level
    # deeper inside an `if` — and Pine raises
    #     "Cannot assign a variable to a tuple. The right side must be a
    #      function call or structure returning a tuple with the same number of
    #      elements."
    # Another COMPILE error, so again it never reaches a chart and never reached
    # this linter. Both halves are checkable statically, so they are.
    func_bodies: dict[str, list] = {}
    current = None
    for n, l in code:
        if not l.strip():
            continue
        header = re.match(r"^([A-Za-z_]\w*)\s*\([^)]*\)\s*=>", l)
        if header and not l.startswith((" ", "\t")):
            current = header.group(1)
            func_bodies[current] = []
            continue
        if current and not l.startswith((" ", "\t")):
            current = None
        if current:
            func_bodies[current].append((n, l))

    def _returned_arity(name):
        body = func_bodies.get(name)
        if not body:
            return None
        # The tuple must sit at the function's BASE indentation. One nested
        # inside a trailing `if` is still the last line of the body but is not
        # the function's return — that is the harder half of CE10172 and the
        # shape a reader is most likely to write.
        base_indent = len(body[0][1]) - len(body[0][1].lstrip())
        _n, last = body[-1]
        stripped = last.strip()
        if len(last) - len(last.lstrip()) != base_indent:
            return 0
        if not (stripped.startswith("[") and stripped.endswith("]")):
            return 0
        # Only count top-level commas, so a nested call's arguments do not
        # inflate the arity.
        depth, parts = 0, 1
        for ch in stripped[1:-1]:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                depth -= 1
            elif ch == "," and depth == 0:
                parts += 1
        return parts

    for n, l in code:
        m = re.match(r"\s*\[([^\]]+)\]\s*=\s*([A-Za-z_]\w*)\s*\(", l)
        if not m:
            continue
        want = len([x for x in m.group(1).split(",") if x.strip()])
        name = m.group(2)
        got = _returned_arity(name)
        if got is None:
            continue                      # a built-in, not one of ours
        if got != want:
            add("error", "tuple_arity", n,
                f"`{name}` is destructured into {want} values but its body "
                f"{'ends with no tuple at all' if got == 0 else f'returns {got}'}"
                " — Pine raises CE10172. The tuple must be the LAST expression "
                "at function-body indentation; nesting it inside an `if` hides "
                "it from the return.")

    # ── assignment before declaration (CE10272) ──────────────────────────────
    #
    # Pine resolves TOP TO BOTTOM. `x := v` where `x` was never declared — or is
    # declared LATER in the file — is "Undeclared identifier" at compile time.
    #
    # This is the failure mode of assembling a script from fragments: a rewrite
    # replaced the block that held six `var` declarations while the code that
    # assigned them survived, and the result linted clean and would not compile.
    # The whole point of generating the file is that this class of mistake is
    # mechanically detectable, so it is detected here.
    declared: dict[str, int] = {}
    for n, l in code:
        m = re.match(r"\s*(?:var(?:ip)?\s+)?"
                     r"(?:float|int|bool|string|color|line|label|box|table|"
                     r"array<[^>]+>|matrix<[^>]+>|[A-Z]\w*)?\s*"
                     r"([A-Za-z_]\w*)\s*=(?!=)", l)
        if m and m.group(1) not in declared:
            declared[m.group(1)] = n
        # tuple declarations: `[a, b] = f(...)`
        t = re.match(r"\s*\[([^\]]+)\]\s*=(?!=)", l)
        if t:
            for part in t.group(1).split(","):
                name = part.strip()
                if name.isidentifier() and name not in declared:
                    declared[name] = n
    # function parameters are declared by their header
    for n, l in code:
        h = re.match(r"^[A-Za-z_]\w*\s*\(([^)]*)\)\s*=>", l)
        if h:
            for part in h.group(1).split(","):
                name = part.strip().split(" ")[-1]
                if name.isidentifier() and name not in declared:
                    declared[name] = n
    # `for i = 0 to n` declares `i`
    for n, l in code:
        f = re.match(r"\s*for\s+([A-Za-z_]\w*)\s*=", l)
        if f and f.group(1) not in declared:
            declared[f.group(1)] = n

    for n, l in code:
        m = re.match(r"\s*([A-Za-z_]\w*)\s*:=", l)
        if not m:
            continue
        name = m.group(1)
        if name not in declared:
            add("error", "assign_before_declare", n,
                f"`{name} := …` but `{name}` is never declared — Pine raises "
                "CE10272 \"Undeclared identifier\". A fragment rewrite that "
                "removed the declaration while leaving the assignment is the "
                "usual cause.")
        elif declared[name] > n:
            add("error", "assign_before_declare", n,
                f"`{name} := …` on line {n}, but `{name}` is not declared until "
                f"line {declared[name]} — Pine resolves top to bottom, so this "
                "is CE10272. Check the fragment assembly order.")

    # ── unbounded collections ────────────────────────────────────────────────
    # A `var` array pushed to on every bar grows without limit until Pine kills
    # the script. Every push site must have a matching bound somewhere in the
    # file (a size check that shifts/pops, or a retention loop).
    pushed = set()
    for n, l in code:
        for m in re.finditer(r"array\.push\s*\(\s*([A-Za-z_]\w*)", l):
            pushed.add(m.group(1))
    whole = "\n".join(l for _n, l in code)
    for name in sorted(pushed):
        bounded = (
            re.search(rf"array\.size\s*\(\s*{re.escape(name)}\s*\)\s*[><]", whole)
            or re.search(rf"array\.(shift|pop|remove)\s*\(\s*{re.escape(name)}",
                         whole))
        if not bounded:
            add("error", "unbounded_collection", 0,
                f"`{name}` is pushed to but never bounded — a var array that "
                "grows every bar eventually kills the script")

    # ── silent state fallback ────────────────────────────────────────────────
    # Production returns NO state when an input is non-finite. A fallback that
    # picks a plausible-looking state instead is the single most dangerous shape
    # this oracle can take: the chart would look right and be wrong.
    for n, l in lit:
        if (re.search(r"(marketState|stateCode|s6_stateCode)\s*:?=.*"
                      r"(Chop|Range|Neutral|Unknown)", l)
                and "==" not in l and "CODE_" not in l):
            add("error", "silent_state_fallback", n,
                "a market state must never fall back to a plausible default; "
                "production returns None and the oracle must show invalid")

    # ── timezone-explicit calendar calls ─────────────────────────────────────
    for n, l in code:
        for fn in _TZ_FUNCS:
            for m in re.finditer(rf"(?<![\w.]){fn}\s*\(", l):
                seg = l[m.end():]
                depth, arg = 1, ""
                for ch in seg:
                    depth += (ch == "(") - (ch == ")")
                    if depth == 0:
                        break
                    arg += ch
                if "," not in arg:
                    add("error", "implicit_timezone", n,
                        f"{fn}() without an explicit timezone argument — it would "
                        "use the chart timezone, not UTC")

    # ── session window inclusivity ───────────────────────────────────────────
    # Only meaningful in a build that HAS session logic. The execution oracle
    # does not — it owns the 1-minute pending walk and never classifies a
    # session — and demanding the idiom there would push a reader toward adding
    # session code purely to satisfy a linter.
    # Gate on USE, not on the constant's presence: both builds carry the shared
    # session schedule from the contract, but only the detection build tests
    # against it.
    joined = " ".join(l for _, l in code)
    if re.search(r"array\.get\s*\(\s*SESSION_START_HOUR", joined) and \
            "s <= h and h < e" not in joined.replace("  ", " "):
        add("error", "window_inclusivity", 0,
            "the session window test must be exactly `s <= h and h < e` "
            "([start, end) — inclusive start, exclusive end)")

    # ── non-suppressible warning ─────────────────────────────────────────────
    warn_lines = [n for n, l in enumerate(src.splitlines(), 1)
                  if "SHADOW MODE" in l and "EXECUTION STATE UNKNOWN" in l]
    # The oracle's banner claims "this chart is not production's execution
    # state". The strategy companion makes a DIFFERENT unsuppressible claim, on
    # its own panel, and is checked for that below instead.
    if not is_strategy and len(warn_lines) < 2:
        add("error", "warning_suppressible", warn_lines[0] if warn_lines else 0,
            "the SHADOW MODE / EXECUTION STATE UNKNOWN / NEWS UNAVAILABLE banner must "
            "render on BOTH the HUD-on and HUD-off paths so no input can hide it")

    # ── generated-file hygiene ───────────────────────────────────────────────
    for pat, rule in ((r"[A-Za-z]:\\\\", "absolute_path"),
                      (r"/Users/", "absolute_path"),
                      (r"/home/", "absolute_path")):
        for n, l in enumerate(src.splitlines(), 1):
            if re.search(pat, l):
                add("error", rule, n, "generated file embeds a machine-specific path")
    if "DO NOT EDIT" not in src:
        add("error", "no_edit_banner", 0, "generated file lacks a DO-NOT-EDIT banner")
    # A DATE INSIDE `timestamp(...)` IS A FIXED DEFAULT, NOT A STAMP. The rule
    # guards against the GENERATOR writing the current time into the file, which
    # would make every regeneration a different file and destroy the tamper
    # check. `input.time(timestamp("2026-06-23T00:00:00+0000"), …)` is the same
    # bytes on every run — and it is the ONLY form `input.time` accepts, because
    # the multi-argument `timestamp()` returns a simple int where a const is
    # required (CE10123). Scoped rather than deleted: a bare ISO stamp anywhere
    # else still errors, and a test holds both halves.
    unstamped = re.sub(r'timestamp\s*\(\s*"[^"]*"\s*\)', "timestamp()", src)
    if re.search(r"\b20\d\d-\d\d-\d\dT\d\d:\d\d", unstamped):
        add("error", "wallclock_stamp", 0,
            "generated file embeds a wall-clock timestamp — regeneration would not "
            "be byte-identical and the tamper check would break")

    # ── stage scope: S1 must contain no trading logic ────────────────────────
    banned = ("ta.pivothigh", "ta.pivotlow", "ta.atr", "ta.rma", "ta.ema", "ta.sma",
              "strategy.entry", "strategy.close", "order_block", "swingHigh",
              "swingLow", "bosLevel", "chochLevel")
    if not is_strategy:
        for n, l in code:
            for b in banned:
                if b in l:
                    add("error", "stage_scope", n,
                        f"`{b}` is beyond Stage S1 (data/time/session only)")

    # ── the strategy companion's own non-negotiables ─────────────────────
    #
    # Each of these is a defect this project has already made once, on the
    # oracle, and would otherwise be free to make again on a second script.
    if is_strategy:
        # The declaration STATEMENT, not a fixed window of leading lines.
        # A window is a guess about how long the header comment happens to be,
        # and this one was wrong by four lines: `slippage = 0` fell inside it
        # and `commission_type` fell outside, so two of the three cost rules
        # were passing on absence rather than on evidence.
        first = strat[0] if strat else 0
        decl = " ".join(l for n, l in code
                        if first <= n < first + 40).split(")")[0]
        # SLIPPAGE. The cost is carried entirely by a cash-per-contract
        # commission against risk-derived quantity, which is a fixed fraction
        # of R at every stop distance. A tick-denominated slippage on top is
        # both a second charge and a stop-distance-dependent one.
        if not re.search(r"slippage\s*=\s*0\b", decl):
            add("error", "cost_double_counted", 0,
                "the declaration must set `slippage = 0`: production's cost is "
                "represented in full by the cash-per-contract commission, and a "
                "tick slippage would charge it twice at a rate that varies with "
                "the stop distance")
        if "strategy.commission.cash_per_contract" not in decl:
            add("error", "cost_model", 0,
                "commission must be cash_per_contract — percent-of-value and "
                "per-order forms do not reduce to a constant fraction of R")
        # PROCESS ORDERS ON CLOSE. True fills an order at the close of the very
        # bar the arm was detected on, which is arm == fill: the exact defect
        # removed from the oracle's live layer.
        if not re.search(r"process_orders_on_close\s*=\s*false", decl):
            add("error", "arm_equals_fill", 0,
                "`process_orders_on_close = false` is required: true fills at "
                "the close of the bar the arm was detected on, reproducing the "
                "arm==fill defect and violating production's 3-minute delay")
        # THE DISCLOSURE. A backtest headline travels without its caveats
        # unless the caveat is on the same surface as the number.
        if "APPROXIMATE" not in src:
            add("error", "undisclosed_approximation", 0,
                "PRACTICAL mode's headline metrics must be labelled APPROXIMATE "
                "on the script's own panel — a number read off the Strategy "
                "Tester is quoted without whatever a comment said")
        if not re.search(r"production executes on 1m", src):
            add("error", "undisclosed_timeframe", 0,
                "the panel must state that production executes on 1-minute "
                "candles while this build is 15-minute, on every path")

    # ── undeclared generated constants ───────────────────────────────────────
    # The rule that would have caught CE10272 "Undeclared identifier
    # ORACLE_SOURCE_HASH_SHORT" — a fragment referenced a constant the generator
    # never emitted, and every structural check still passed. Pine only reports
    # this at COMPILE time, which is the one thing this environment cannot do, so
    # it has to be caught here.
    #
    # Scope: SCREAMING_SNAKE identifiers only. Those are exactly the generated
    # constants (ORACLE_*, SESSION_*, CODE_*); lower-case names are Pine builtins
    # and locals, which this linter has no symbol table for and must not guess at.
    defined: set[str] = set()
    for _, l in code:
        # NAME = ...          (plain constant)
        m = re.match(r"\s*([A-Z][A-Z0-9_]{2,})\s*=(?!=)", l)
        if m:
            defined.add(m.group(1))
        # var type NAME = ... (array/series constant)
        m = re.match(r"\s*var\s+[\w<>]+\s+([A-Z][A-Z0-9_]{2,})\s*=(?!=)", l)
        if m:
            defined.add(m.group(1))

    PINE_BUILTIN_CAPS = {"OPEN", "HIGH", "LOW", "CLOSE"}
    used: dict[str, int] = {}
    for n, l in code:
        # `#` is excluded from the lookbehind: `#EF476F` is a hex COLOUR literal,
        # not an identifier. Without it the rule flags every colour whose hex
        # happens to start with a letter — a false positive that would train
        # people to ignore this check, which is worse than not having it.
        for tok in re.findall(r"(?<![\w.#])([A-Z][A-Z0-9_]{2,})(?![\w])", l):
            used.setdefault(tok, n)
    for tok, first_line in sorted(used.items(), key=lambda kv: kv[1]):
        if tok in defined or tok in PINE_BUILTIN_CAPS:
            continue
        add("error", "undeclared_constant", first_line,
            f"`{tok}` is used but never defined — Pine will refuse this with "
            "\"Undeclared identifier\" (CE10272). Either the generator must emit "
            "it, or the fragment must stop referencing it.")

    # ── forward references ───────────────────────────────────────────────────
    # Pine resolves strictly top-to-bottom: a fragment may only reference what an
    # EARLIER fragment declared. With the assembly now at 13 fragments this is the
    # likeliest remaining compile break, and — like CE10272 and CE10088 — Pine
    # only reports it at compile time.
    #
    # Scoped to the ORACLE's own identifiers. Pine builtins are declared nowhere
    # in the file, so a general rule would flag every one of them.
    _OWN = re.compile(r"^(?:s2_|s3_|s4_|sess|utc|day|bar|oracle|ctx|BG_|CTX_|"
                      r"i_|f_|ORACLE_|SESSION_|CODE_|TAG_|BIAS_|LEG_|SWING_|"
                      r"ATR_|VOL_|OB_FILTER)")
    decl_line: dict[str, int] = {}
    for n, l in code:
        if not l or l[:1].isspace():
            continue
        for pat in (r"^([A-Za-z_]\w*)\s*=(?!=)",
                    r"^var\s+(?:[\w<>]+\s+)?([A-Za-z_]\w*)\s*=(?!=)",
                    r"^([A-Za-z_]\w*)\s*\(.*\)\s*=>"):
            m = re.match(pat, l)
            if m and _OWN.match(m.group(1)):
                decl_line.setdefault(m.group(1), n)
                break

    for n, l in code:
        # No `(?![\w(])` here: excluding tokens followed by `(` would skip every
        # function CALL site, which is exactly the forward reference most likely
        # to occur across fragments. Self-flagging on the declaration line is
        # prevented by the `n < d` test instead.
        for tok in re.findall(r"(?<![\w.])([A-Za-z_]\w*)", l):
            d = decl_line.get(tok)
            if d is not None and n < d:
                add("error", "forward_reference", n,
                    f"`{tok}` is used here but not declared until line {d}. Pine "
                    "resolves top-to-bottom — reorder the fragments, or move the "
                    "declaration into an earlier one.")

    # ── global assignment inside a function ──────────────────────────────────
    # Pine refuses this outright: "Cannot modify global variable X in function"
    # (CE10088). A user-defined function may read globals but never assign to
    # them. This shipped once — retention counters were kept inside
    # `f_pushLabel` — and, like CE10272, it is only reported at COMPILE time,
    # which this environment cannot reach.
    #
    # A function body is specifically a `name(...) =>` block. An `if` block at
    # global scope is ALSO indented but assigning to a global there is perfectly
    # legal, so the two must not be conflated.
    globals_declared: set[str] = set()
    for _, l in code:
        if not l or l[:1].isspace():
            continue
        m = (re.match(r"(?:var\s+[\w<>]+\s+|var\s+)?([A-Za-z_]\w*)\s*=(?!=)", l)
             or re.match(r"([A-Za-z_]\w*)\s*:=", l))
        if m:
            globals_declared.add(m.group(1))

    in_func = False
    func_name = ""
    for n, l in code:
        if not l.strip():
            continue
        indented = l[:1].isspace()
        if not indented:
            fm = re.match(r"([A-Za-z_]\w*)\s*\(.*\)\s*=>", l)
            in_func = bool(fm)
            func_name = fm.group(1) if fm else ""
            continue
        if not in_func:
            continue
        am = re.match(r"\s*([A-Za-z_]\w*)\s*:=", l)
        if am and am.group(1) in globals_declared:
            add("error", "global_assign_in_function", n,
                f"`{am.group(1)}` is a global and `{func_name}()` assigns to it — "
                "Pine refuses this with \"Cannot modify global variable\" "
                "(CE10088). Derive the value at the call site, or compute it "
                "on demand instead of keeping a counter.")

    def _split_args(tail: str) -> list:
        """Top-level comma-separated arguments of an already-opened call.

        Depth-aware, and it stops at the call's own closing bracket. The regex
        this replaced could not split `a, 2, 3)` at all — every comma looked
        nested to it — so any table declared on a single line went unchecked.
        """
        out, cur, depth = [], [], 0
        for ch in tail:
            if ch in "([":
                depth += 1
            elif ch in ")]":
                if depth == 0:
                    break
                depth -= 1
            elif ch == "," and depth == 0:
                out.append("".join(cur).strip())
                cur = []
                continue
            cur.append(ch)
        if "".join(cur).strip():
            out.append("".join(cur).strip())
        return [a for a in out if a]

    # ── table row capacity ───────────────────────────────────────────────────
    # RE10040 "Row N is out of table bounds". A RUNTIME error, so it survives
    # compilation and static syntax checking and only appears once a chart is
    # loaded — at which point it kills the whole indicator and the chart goes
    # blank. It shipped once (HUD declared 24 rows, 25 row-writes) and was
    # simultaneously latent in the debug table (18 declared, 21 writes).
    #
    # Counting is deliberately an UPPER BOUND: conditional rows are counted as if
    # they all render, because the worst case is what must fit.
    tables: dict[str, tuple[int, int]] = {}          # name -> (rows, lineno)
    for n, l in code:
        m = re.search(r"\b(?:var\s+)?table\s+(\w+)\s*=\s*table\.new\s*\((.*)", l)
        if not m:
            continue
        name, rest = m.group(1), m.group(2)
        # Row count is the 3rd positional arg, possibly on the next line.
        tail = rest
        idx = code.index((n, l))
        for _, nxt in code[idx + 1: idx + 3]:
            if tail.count("(") > tail.count(")"):
                tail += " " + nxt.strip()
        args = _split_args(tail)
        if len(args) >= 3:
            # A declaration that closes on its own line leaves the third
            # argument as `20)`. Left unstripped that parses as neither a digit
            # nor a name, `rows` stays None, and the table is skipped entirely
            # — the check passing because it never looked.
            rows_expr = args[2].rstrip(")").strip()
            rm = re.fullmatch(r"(\w+)", rows_expr)
            rows = None
            if rows_expr.isdigit():
                rows = int(rows_expr)
            elif rm:
                # a named constant declared elsewhere in the file
                for _, dl in code:
                    dm = re.match(rf"^{re.escape(rm.group(1))}\s*=\s*(\d+)\s*$", dl.strip())
                    if dm:
                        rows = int(dm.group(1))
                        break
            if rows is not None:
                tables[name] = (rows, n)

    # helper -> table it writes into
    helper_table: dict[str, str] = {}
    cur_helper = None
    for n, l in code:
        if not l.strip():
            continue
        if not l[:1].isspace():
            hm = re.match(r"([A-Za-z_]\w*)\s*\(.*\)\s*=>", l)
            cur_helper = hm.group(1) if hm else None
            continue
        if cur_helper:
            cm = re.search(r"table\.cell\s*\(\s*(\w+)\s*,", l)
            if cm and cm.group(1) in tables:
                helper_table[cur_helper] = cm.group(1)

    # ROWS, not cell writes. A four-column panel writes two cells per row; a
    # rule that counts writes reports twice the height and demands twice the
    # capacity, which is the opposite of what it is for.
    writes: dict[str, int] = {t: 0 for t in tables}
    rowset: dict[str, set] = {t: set() for t in tables}
    offsets: dict[str, int] = {t: 0 for t in tables}

    def _note_row(tbl, expr):
        """Record a row expression. Returns False if it could not be read."""
        expr = expr.strip()
        if re.fullmatch(r"\d+", expr):
            rowset[tbl].add(int(expr))
            return True
        m = re.fullmatch(r"(\w+)\s*\+\s*(\d+)", expr)
        if m:
            # `base + N`: the base is itself some number of rows in, so the
            # reachable index is at least N. Tracked separately and added to
            # the count of unresolved rows below.
            offsets[tbl] = max(offsets[tbl], int(m.group(2)) + 1)
            return True
        return False

    for n, l in code:
        stripped = l.strip()
        if re.match(r"[A-Za-z_]\w*\s*\(.*\)\s*=>", stripped):
            continue                       # the helper's own definition
        for helper, tbl in helper_table.items():
            hm = re.search(
                rf"(?<![\w.]){re.escape(helper)}\s*\(\s*([^,]+),", stripped)
            if hm and not _note_row(tbl, hm.group(1)):
                writes[tbl] += 1
        dm = re.search(r"table\.cell\s*\(\s*(\w+)\s*,\s*[^,]+,\s*([^,]+),",
                       stripped)
        if dm and dm.group(1) in writes and not any(
                re.search(rf"(?<![\w.]){re.escape(h)}\s*\(", stripped)
                for h in helper_table):
            if cur_helper is None and not _note_row(dm.group(1), dm.group(2)):
                writes[dm.group(1)] += 1

    for tbl, (rows, lineno) in tables.items():
        literal = max(rowset.get(tbl, {0}) or {0}) + 1
        used = max(literal, writes.get(tbl, 0) + offsets.get(tbl, 0))
        if used > rows:
            add("error", "table_row_overflow", lineno,
                f"table `{tbl}` is declared with {rows} rows but up to {used} "
                "row-writes are reachable — Pine raises RE10040 \"Row N is out of "
                "table bounds\" at RUNTIME and the whole indicator stops "
                "rendering. Raise the declared capacity.")

    # ── bracket balance ──────────────────────────────────────────────────────
    # Tracked CUMULATIVELY, not per line: Pine allows a statement to wrap across
    # lines, so a per-line count flags every legal continuation. The real defect
    # is a new TOP-LEVEL statement beginning while brackets are still open, or a
    # file that ends unbalanced.
    depth = 0
    for n, l in code:
        stripped = l.strip()
        if stripped and not l[:1].isspace() and depth != 0:
            add("error", "unclosed_bracket", n,
                f"a new top-level statement begins while {depth} bracket(s) are "
                "still open — the previous statement never closed")
            depth = 0
        depth += l.count("(") - l.count(")")
        depth += l.count("[") - l.count("]")
        if depth < 0:
            add("error", "extra_closing_bracket", n, "closing bracket with no opener")
            depth = 0
    if depth != 0:
        add("error", "unclosed_bracket", 0, f"file ends with {depth} unclosed bracket(s)")

    return findings


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--file", type=Path, default=DEFAULT_FILE)
    args = ap.parse_args(argv)

    if not args.file.is_file():
        print(f"FAIL: no Pine artefact at {args.file}", file=sys.stderr)
        return 2

    src = args.file.read_text(encoding="utf-8")
    findings = lint(src)
    errors = [f for f in findings if f["severity"] == "error"]
    warns = [f for f in findings if f["severity"] == "warn"]

    print(f"lint: {args.file.name}  ({src.count(chr(10)) + 1} lines)")
    print(f"  errors: {len(errors)}   warnings: {len(warns)}")
    for f in findings:
        loc = f"line {f['line']}" if f["line"] else "file"
        print(f"  [{f['severity']}] {f['rule']} ({loc}): {f['detail']}")
    if not findings:
        print("\n  no detectable defect. NOTE: this is static analysis only — it "
              "does NOT prove the script compiles on TradingView.")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
