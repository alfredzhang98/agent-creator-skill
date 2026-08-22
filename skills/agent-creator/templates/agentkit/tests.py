"""Regression suite. Run: ``python3 tests.py``

Written after an independent review found 33 reproducible defects in code
whose own self-test passed 14/14. The root cause was not any single bug — it
was that the tests stopped where the bugs started. The deferral test asserted
that a schema came back and never asserted that the subsequent call
*succeeded*; it could not have failed.

So this file has one rule, applied to every case:

    **Assert the outcome, not the intermediate step.**

Not "ToolSearch returned a schema" but "the tool then ran". Not "the budget
function was called" but "the message got smaller". Not "a permission was
computed" but "which one, and it was not `allow`".

Every case below is named for the defect it exists to keep fixed, so a
regression tells you what broke rather than that something did.
"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import warnings

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "tools"), os.path.dirname(HERE)]

import cost_meter as CM
import hooks as H
import loop as L
import memory as MEM
import permissions as P
import planner as PL
import registry as R
import result_store as RS
import skills_loader as SK
import state as ST
import verifier as V
from contract import DEFAULT_MAX_RESULT_CHARS, Permission, ToolResult, build_tool
from files import EditTool, ReadTool, WriteTool
from meta import make_skill_tool, make_tool_search
from sandbox_backend import SandboxPolicy, UnconfiguredSandbox
from search import GlobTool, GrepTool
from shell import make_shell_tool

FAILURES: list[str] = []
COUNT = 0


def check(name: str, condition: bool, detail: str = "") -> None:
    global COUNT
    COUNT += 1
    if not condition:
        FAILURES.append(f"{name}{' — ' + detail if detail else ''}")


def case(fn):
    """Run a case; an exception is a failure, not a crash of the suite."""
    try:
        fn()
    except Exception as exc:  # noqa: BLE001
        FAILURES.append(f"{fn.__name__} raised {type(exc).__name__}: {exc}")
    return fn


class Ctx:
    def __init__(self, cwd: str):
        self.cwd = cwd
        self.session_dir = cwd
        self.read_files: dict[str, float] = {}
        self.permission_mode = "default"

    def aborted(self) -> bool:
        return False


def tool(name: str, **kw):
    kw.setdefault("description", f"d {name}")
    kw.setdefault("input_schema", {"type": "object", "properties": {}})
    kw.setdefault("call", lambda i, c: ToolResult.success(f"ran {name}"))
    return build_tool(name=name, **kw)


# ==========================================================================
# Permissions — three confirmed security inversions
# ==========================================================================

@case
def permission_ladder():
    plain = tool("T")
    raising = tool("R", check_permissions=lambda i, c: (_ for _ in ()).throw(RuntimeError()))

    # dont_ask is the most RESTRICTIVE autonomous mode, not a soft bypass.
    check("dont_ask converts ask to deny",
          P.resolve(plain, {}, P.PermissionContext(mode="dont_ask"), None).behavior == "deny")
    check("raising predicate never becomes allow",
          P.resolve(raising, {}, P.PermissionContext(mode="dont_ask"), None).behavior == "deny")
    check("raising predicate asks in default mode",
          P.resolve(raising, {}, P.PermissionContext(), None).behavior == "ask")

    # accept_edits grants EDITS, and only to declared edit tools.
    ctx = P.PermissionContext(mode="accept_edits", edit_tools=frozenset({"Edit"}))
    check("accept_edits allows a declared edit tool",
          P.resolve(tool("Edit"), {}, ctx, None).behavior == "allow")
    check("accept_edits does not blanket-allow other tools",
          P.resolve(tool("Deploy"), {}, ctx, None).behavior == "ask")

    # Bypass immunity must survive the spelling a real tool emits.
    for spelling in ("safety", "safetyCheck"):
        t = tool(f"S{spelling}", check_permissions=lambda i, c, sp=spelling:
                 Permission("ask", reason={"type": sp}))
        check(f"bypass cannot override reason={spelling}",
              P.resolve(t, {}, P.PermissionContext(mode="bypass"), None).behavior == "ask")

    # A configured deny must never evaporate because it cannot be evaluated.
    ctx = P.PermissionContext(mode="dont_ask", rules=[P.Rule("T", "deny", content="rm:*")])
    check("unevaluable content-scoped deny does not become allow",
          P.resolve(plain, {}, ctx, None).behavior == "deny")

    # Rules are literal, not globs: widening what a human typed grants more.
    check("rule content is not a glob", not P.Rule("T", "allow", "a?b").matches("T", "axb"))
    check("rule content matches itself", P.Rule("T", "allow", "a?b").matches("T", "a?b"))

    # Provenance decides, not list order.
    ctx = P.PermissionContext(rules=[P.Rule("T", "allow", source="project"),
                                     P.Rule("T", "deny", source="policy")])
    check("policy outranks project regardless of order",
          P.resolve(plain, {}, ctx, None).behavior == "deny")

    # Loud failure beats a silently-lost guarantee.
    bad = tool("B", check_permissions=lambda i, c: Permission("ask", reason={"type": "typo"}))
    try:
        P.decide(bad, {}, P.PermissionContext(), None)
        check("unknown reason type raises", False)
    except ValueError:
        check("unknown reason type raises", True)
    try:
        P.PermissionContext(mode="bypassPermissions")
        check("unknown mode raises", False)
    except ValueError:
        check("unknown mode raises", True)

    ctx = P.PermissionContext(rules=[P.Rule("T", "deny")])
    outcomes = [P.resolve(plain, {}, ctx, None).behavior for _ in range(3)]
    check("repeated denial escalates to ask", outcomes == ["deny", "deny", "ask"], str(outcomes))
    check("rule suggestion keeps two-token prefix",
          any(s["content"] == "git push:*" for s in P.suggest_rules("Bash", "git push --force")))


# ==========================================================================
# Deferral — the flagship feature that could never work
# ==========================================================================

@case
def deferral_round_trip():
    ref: dict = {}
    ts = make_tool_search(lambda: ref["pool"])
    ref["pool"] = R.assemble(
        [tool("TodoWrite", should_defer=True),
         tool("SlackSend", should_defer=True, search_hint="send slack message"),
         tool("Read"), ts],
        defer_mode="always",
    )
    work = tempfile.mkdtemp()

    class M:
        def __init__(self): self.n = 0
        def complete(self, m, t, mo=None):
            self.n += 1
            if self.n == 1:
                return {"text": "", "tool_calls": [("c1", "ToolSearch", {"query": "select:TodoWrite"})]}
            if self.n == 2:
                return {"text": "", "tool_calls": [("c2", "TodoWrite", {})]}
            return {"text": "done", "tool_calls": []}

    res = L.run(M(), ref["pool"], Ctx(work), P.PermissionContext(mode="bypass"),
                [{"role": "user", "content": "x"}], results_dir=work)
    # THE assertion the old suite was missing: not "a schema came back" but
    # "the tool then actually ran". Searched by content, not by index — an
    # index-keyed assertion breaks on any message-shape change and proves
    # nothing about behaviour.
    text = "".join(
        b.get("content", "") if isinstance(b, dict) else str(b)
        for m in res.messages
        for b in (m["content"] if isinstance(m["content"], list) else [m["content"]])
        if isinstance(b, (dict, str))
    )
    check("a fetched tool can actually be called", "ran TodoWrite" in text)
    check("the schema really was delivered first", "<functions>" in text)
    check("model is told which tools are withheld", "SlackSend" in text)
    check("the run completed rather than looping", res.stop is L.Stop.COMPLETED,
          res.stop.value)

    matches, already = R.search(ref["pool"], "+slack")
    check("required-only query matches", [t.name for t in matches] == ["SlackSend"])
    matches, already = R.search(ref["pool"], "select:Read")
    check("already-loaded tool reported, not 'no match'", already == ["Read"])
    check("loaded tool leaves the withheld list", "TodoWrite" not in ref["pool"].withheld_names)
    check("deferred schema still carries input_schema",
          all("input_schema" in s for s in ref["pool"].schemas()))


# ==========================================================================
# Loop — recovery ladders, budgets, hook discipline
# ==========================================================================

@case
def loop_behaviour():
    pool = R.assemble([tool("Read")])
    work = tempfile.mkdtemp()
    ctx = Ctx(work)

    calls: list = []
    class Hk:
        def run(self, spec, payload): calls.append(spec.event); return H.HookRun(exit_code=0)
    L.run(type("M", (), {"complete": lambda s, m, t, mo=None: {"text": "d", "tool_calls": []}})(),
          pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
          hook_specs=[H.HookSpec("Stop", "n")], hook_executor=Hk(), results_dir=work)
    check("Stop hooks fire exactly once", calls == ["Stop"], str(calls))

    res = L.run(type("M", (), {"complete": lambda s, m, t, mo=None:
                               {"text": "d", "tool_calls": [], "usage": {}}})(),
                pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
                budget=L.Budget(max_usd=0.01), cost_of=lambda u: 999.0, results_dir=work)
    check("a single overspending turn is caught", res.stop is L.Stop.COST_LIMIT, res.stop.value)

    seen: list = []
    class M3:
        def __init__(self): self.n = 0
        def complete(self, m, t, mo=None):
            self.n += 1; seen.append(mo)
            return ({"error": "output_truncated", "text": "p"} if self.n == 1
                    else {"text": "done", "tool_calls": []})
    L.run(M3(), pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
          results_dir=work)
    check("the escalate rung raises the output cap",
          seen == [None, L.ESCALATED_MAX_OUTPUT_TOKENS], str(seen))

    class M4:
        def __init__(self): self.n = 0
        def complete(self, m, t, mo=None):
            self.n += 1
            return ({"error": "context_too_long"} if self.n < 3
                    else {"text": "done", "tool_calls": []})
    res = L.run(M4(), pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
                shed=lambda m: m[-1:], compact=lambda m: m[-1:], results_dir=work)
    check("context ladder tries cheap relief before summarising",
          res.transitions == [L.Continue.SHED, L.Continue.COMPACTED],
          str([t.value for t in res.transitions]))

    res = L.run(type("M", (), {"complete": lambda s, m, t, mo=None: {"text": "", "tool_calls": []}})(),
                pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
                results_dir=work)
    roles = [m["role"] for m in res.messages]
    check("silent turns do not stack user messages",
          not any(a == b == "user" for a, b in zip(roles, roles[1:])), str(roles))
    check("silent turns still terminate", res.stop is L.Stop.NO_ACTION)

    ev: list = []
    class Hk2:
        def run(self, spec, payload): ev.append(spec.event); return H.HookRun(exit_code=0)
    L.run(type("M", (), {"complete": lambda s, m, t, mo=None: {"error": "context_too_long"}})(),
          pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
          hook_specs=[H.HookSpec("Stop", "a"), H.HookSpec("StopFailure", "b")],
          hook_executor=Hk2(), results_dir=work)
    check("failed exits run StopFailure, not Stop", ev == ["StopFailure"], str(ev))

    class Boom:
        def complete(self, m, t, mo=None): raise RuntimeError("socket died")
    res = L.run(Boom(), pool, ctx, P.PermissionContext(), [{"role": "user", "content": "x"}],
                results_dir=work)
    check("a provider crash is an exit, not a traceback",
          res.stop is L.Stop.MODEL_ERROR and "socket died" in res.detail)

    class M9:
        def __init__(self): self.n = 0
        def complete(self, m, t, mo=None):
            self.n += 1
            return ({"text": "", "tool_calls": [("c", "Read", {})]} if self.n == 1
                    else {"text": "d", "tool_calls": []})
    res = L.run(M9(), pool, ctx, P.PermissionContext(mode="bypass"),
                [{"role": "user", "content": "x"}], results_dir=work)
    block = [m for m in res.messages if isinstance(m.get("content"), list)][0]["content"][0]
    check("tool_result blocks carry only API-legal keys",
          set(block) <= {"type", "tool_use_id", "content", "is_error"}, str(sorted(block)))


# ==========================================================================
# Pipeline — errors as data, never an exception
# ==========================================================================

@case
def pipeline_never_raises():
    work = tempfile.mkdtemp()
    ctx = Ctx(work)
    import pipeline as PI

    hostile = tool(
        "Hostile",
        is_read_only=lambda i: (_ for _ in ()).throw(RuntimeError("predicate")),
        rule_key=lambda i: (_ for _ in ()).throw(RuntimeError("rule_key")),
        call=lambda i, c: (_ for _ in ()).throw(RuntimeError("call")),
    )
    pool = R.assemble([hostile])
    out = PI.execute(pool, "t1", "Hostile", {}, ctx, P.PermissionContext(mode="bypass"),
                     results_dir=work)
    check("a tool that raises everywhere still yields a result",
          not out.result.ok and "call" in out.result.error)

    out = PI.execute(pool, "t2", "NoSuchTool", {}, ctx, P.PermissionContext(), results_dir=work)
    check("unknown tool is a correctable error", "No such tool" in out.result.error)

    strict = tool("Strict", input_schema={"type": "object",
                                          "properties": {"a": {"type": "string"}},
                                          "required": ["a"], "additionalProperties": False})
    pool2 = R.assemble([strict])
    out = PI.execute(pool2, "t3", "Strict", {}, ctx, P.PermissionContext(mode="bypass"),
                     results_dir=work)
    check("a missing required parameter is named", "a" in out.result.error, out.result.error)
    out = PI.execute(pool2, "t3b", "Strict", {"a": "ok", "b": 1}, ctx,
                     P.PermissionContext(mode="bypass"), results_dir=work)
    check("an unexpected parameter is named as unexpected",
          "unexpected" in out.result.error and "b" in out.result.error, out.result.error)

    # A disabled tool is filtered out at assembly — the model never sees it.
    disabled = tool("Off", is_enabled=lambda: False)
    check("a disabled tool never reaches the model",
          R.assemble([disabled]).tools == ())
    # If it is disabled AFTER assembly, the dispatcher is the backstop.
    flag = {"on": True}
    late = tool("Late", is_enabled=lambda: flag["on"])
    pool3 = R.assemble([late])
    flag["on"] = False
    out = PI.execute(pool3, "t4", "Late", {}, ctx, P.PermissionContext(mode="bypass"),
                     results_dir=work)
    check("a tool disabled after assembly is refused at dispatch",
          "not enabled" in out.result.error, out.result.error)

    huge = tool("Huge", call=lambda i, c: ToolResult.failure("E" * 200_000))
    out = PI.execute(R.assemble([huge]), "t5", "Huge", {}, ctx,
                     P.PermissionContext(mode="bypass"), results_dir=work)
    block = out.to_block(work, DEFAULT_MAX_RESULT_CHARS)
    check("an enormous ERROR is capped too, not just a success",
          len(block["content"]) < 100_000, str(len(block["content"])))


# ==========================================================================
# Verifier — attribution-scoped, report-once
# ==========================================================================

@case
def verifier_baseline():
    A = os.path.abspath("a.py")
    v = V.BaselineVerifier()
    v.before_edit(A, [V.Signal(A, "undefined name x", "error", line=3)])
    fresh = v.new_findings(lambda p: V.CheckResult.of(p, [
        V.Signal(A, "undefined name x", "error", line=3),
        V.Signal(A, "undefined name x", "error", line=9)]))
    check("a second occurrence of the same message is new", len(fresh) == 1, str(len(fresh)))

    v = V.BaselineVerifier()
    v.before_edit(A, [V.Signal(A, "pre-existing", "warning", line=1)])
    v.new_findings(lambda p: V.CheckResult.unavailable())
    after = v.new_findings(lambda p: V.CheckResult.of(p, [V.Signal(A, "pre-existing", "warning")]))
    check("a checker that could not run does not wipe the baseline", after == [])

    v = V.BaselineVerifier(); v.before_edit(A, [])
    check("relative and absolute paths are the same file",
          len(v.new_findings(lambda p: V.CheckResult.of(p, [V.Signal("a.py", "boom", "error")]))) == 1)

    v = V.BaselineVerifier(); v.before_edit(A, [])
    check("LSP-style severity does not crash the diff",
          [s.severity for s in v.new_findings(
              lambda p: V.CheckResult.of(p, [V.Signal(A, "x", "Error")]))] == ["error"])
    check("bool is not read as LSP severity 1", V.normalize_severity(True) == "error")
    check("'Information' maps to info, not error", V.normalize_severity("Information") == "info")

    try:
        V.BaselineVerifier().before_edit(A)
        check("before_edit requires an explicit baseline", False)
    except TypeError:
        check("before_edit requires an explicit baseline", True)

    v = V.BaselineVerifier(); v.before_edit(A, [])
    try:
        v.new_findings(lambda p: [])
        check("a bare list is rejected as ambiguous", False)
    except TypeError:
        check("a bare list is rejected as ambiguous", True)

    report = V.Report([V.Signal(A, "y" * 200, "error", line=i) for i in range(50)])
    rendered = report.render(1000)
    check("a large report respects its cap and says what it dropped",
          len(rendered) <= 1000 and "more not shown" in rendered, str(len(rendered)))


# ==========================================================================
# Result store — the budget must actually shrink the message
# ==========================================================================

@case
def result_budget():
    d = tempfile.mkdtemp()
    blocks = [{"tool_use_id": f"t{i}", "content": "x" * 2100} for i in range(3)]
    before = sum(len(b["content"]) for b in blocks)
    out, ok = RS.apply_message_budget(blocks, d, budget=6000)
    after = sum(len(b["content"]) for b in out)
    check("the budget never makes the message bigger", after <= before, f"{before}->{after}")
    check("a futile relocation leaves no orphan file", len(os.listdir(d)) == 0)
    check("an unachievable budget is reported, not assumed", ok is False)

    big = [{"tool_use_id": f"b{i}", "content": "y" * 80_000} for i in range(4)]
    out, ok = RS.apply_message_budget(big, d, budget=100_000)
    check("an achievable budget is actually achieved",
          ok and sum(len(b["content"]) for b in out) <= 100_000)

    sub = os.path.join(tempfile.mkdtemp(), "a", "r"); os.makedirs(sub)
    p = RS.persist("SECRET", sub, "../../pwned")
    check("a hostile tool_use_id cannot escape the results directory",
          os.path.abspath(p.path).startswith(os.path.abspath(sub)), p.path)
    check("ids sharing a long prefix do not collide",
          RS.persist("A", sub, "x" * 70 + "AAA").path != RS.persist("B", sub, "x" * 70 + "BBB").path)


# ==========================================================================
# State — resume must produce a conversation the API accepts
# ==========================================================================

@case
def state_resume():
    partial = [{"role": "user", "content": "go"},
               {"role": "assistant", "content": "", "tool_calls": [{"id": "a"}, {"id": "b"}]},
               {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "a"}]}]
    out = ST.drop_orphan_tool_calls(partial)
    leftovers = [b for m in out if isinstance(m.get("content"), list)
                 for b in m["content"] if b.get("type") == "tool_result"]
    check("a half-written batch leaves no orphan tool_result", leftovers == [])
    check("repair does not create consecutive user messages",
          [m["role"] for m in out] == ["user"])

    anthropic = [{"role": "assistant", "content": [{"type": "tool_use", "id": "z"}]}]
    check("Anthropic-shaped orphans are recognised",
          ST.drop_orphan_tool_calls(anthropic) == [])

    out_of_order = [{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "q"}]},
                    {"role": "assistant", "content": "", "tool_calls": [{"id": "q"}]}]
    check("a result before its call does not count as satisfied",
          ST.drop_orphan_tool_calls(out_of_order) == [])

    good = [{"role": "assistant", "content": "", "tool_calls": [{"id": "g"}]},
            {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "g"}]}]
    check("a well-formed pair survives untouched", ST.drop_orphan_tool_calls(good) == good)

    d = tempfile.mkdtemp()
    paths = ST.Paths(d, "s").ensure()
    with ST.Transcript(paths.transcript) as t:
        t.append("message", {"message": {"role": "user", "content": "hi"}})
    with open(paths.transcript, "a") as fh:
        fh.write('{"kind": "mes\n')
    bad: list = []
    entries = list(ST.read_transcript(paths.transcript, on_bad_line=lambda n, l, e: bad.append(n)))
    check("a torn tail costs one line, not the session", len(entries) == 1 and len(bad) == 1)

    st = ST.Staging(paths)
    with open(st.path("o.txt"), "w") as fh:
        fh.write("artifact")
    dest = os.path.join(d, "lib", "r1")
    st.promote(dest)
    check("promotion moves the artifact", os.path.exists(os.path.join(dest, "o.txt")))
    try:
        ST.Staging(ST.Paths(d, "s2").ensure()).promote(dest)
        check("promotion refuses to overwrite", False)
    except FileExistsError:
        check("promotion refuses to overwrite", True)


# ==========================================================================
# Memory
# ==========================================================================

@case
def memory_recall():
    headers = [MEM.MemoryHeader(f"/a/{f}", n, "d", "project", 1)
               for f, n in [("one.md", "dup"), ("two.md", "dup"), ("three.md", "x")]]
    got = MEM.select("q", headers, lambda a, b: ["one.md", "two.md"])
    check("files sharing a frontmatter name stay reachable",
          [h.path for h in got] == ["/a/one.md", "/a/two.md"], str([h.path for h in got]))

    script = (
        "import sys;sys.path.insert(0,'.');import memory as M;"
        "hs=[M.MemoryHeader(f'/m{i}.md',f'm{i}','d',None,1) for i in range(50)];"
        "print([h.name for h in M.select('q',hs,lambda a,b:[f'm{i}.md' for i in range(50)])])"
    )
    runs = {subprocess.run([sys.executable, "-c", script], capture_output=True,
                           text=True, cwd=HERE).stdout.strip() for _ in range(3)}
    check("recall is deterministic across processes", len(runs) == 1, str(runs))
    check("recall preserves the model's ranking", "['m0', 'm1', 'm2', 'm3', 'm4']" in runs.pop())

    for body, expected in [("ship by Thursday", True), ("next sprint", True),
                           ("in 2 weeks", True), ("end of month", True),
                           ("released on 2026-03-05", False),
                           ("the codebase today uses X", False),
                           ("`today` is a column name", False)]:
        check(f"relative-date check on {body!r}",
              (MEM._relative_date(body) is not None) == expected)

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "m.md"), "w") as fh:
        fh.write(MEM.render_memory("a-b", "has: colon\nand newline", "user", "body"))
    check("a written memory reads back with its description intact",
          MEM.scan(d)[0].description == "has: colon and newline")
    text, _ = MEM.truncate_entrypoint("x" * 30_000)
    check("a truncated entrypoint respects its own cap",
          len(text.encode()) <= MEM.MAX_ENTRYPOINT_BYTES)


# ==========================================================================
# Planner
# ==========================================================================

@case
def planner_phases():
    d = tempfile.mkdtemp()
    plan_path = os.path.join(d, "p.md")

    s = PL.PlanSession(PL.Plan(plan_path))
    s.advance(PL.Phase.ABANDONED)
    check("abandoning a plan does not unlock writes",
          s.read_only and not s.tool_allowed("Write"))

    s = PL.PlanSession(PL.Plan(plan_path))
    s.ask("redis or in-memory?")
    check("open questions block the move to design", not s.advance(PL.Phase.DESIGN)[0])
    s.answered("redis or in-memory?")
    check("answering unblocks it", s.advance(PL.Phase.DESIGN)[0])
    ok, why = s.advance(PL.Phase.AWAITING_APPROVAL)
    check("approval cannot be requested without a plan on disk", not ok and "no plan file" in why)
    with open(plan_path, "w") as fh:
        fh.write("# Plan\n1. do it\n")
    check("with a plan on disk it can", s.advance(PL.Phase.AWAITING_APPROVAL)[0])
    check("execution requires approval", not s.advance(PL.Phase.EXECUTE)[0])
    check("approval reaches execute", s.approve()[0] and s.tool_allowed("Write"))

    s2 = PL.PlanSession(PL.Plan(plan_path))
    s2.advance(PL.Phase.DESIGN); s2.advance(PL.Phase.AWAITING_APPROVAL); s2.approve()
    s2.phase = PL.Phase.AWAITING_APPROVAL
    with open(plan_path, "w") as fh:
        fh.write("# swapped\nrm -rf /\n")
    ok, why = s2.advance(PL.Phase.EXECUTE)
    check("rewriting the plan after approval is caught", not ok and "changed after" in why)

    s3 = PL.PlanSession(PL.Plan(plan_path))
    s3.advance(PL.Phase.DESIGN); s3.advance(PL.Phase.AWAITING_APPROVAL)
    check("a plan can be rejected back to design", s3.reject("too broad")[0])
    s3.approve()  # no-op; not awaiting
    s4 = PL.PlanSession(PL.Plan(plan_path))
    s4.advance(PL.Phase.DESIGN); s4.advance(PL.Phase.AWAITING_APPROVAL); s4.approve()
    s4.set_todos([PL.Todo("a", "completed")])
    check("a finished run has a success terminal",
          s4.advance(PL.Phase.DONE)[0] and s4.phase is PL.Phase.DONE)
    check("statuses are validated before counting",
          not s4.set_todos([PL.Todo("a", "in-progress")])[0])
    try:
        PL.explore_prompts("q", 9)
        check("over-fanning exploration is refused", False)
    except ValueError:
        check("over-fanning exploration is refused", True)


# ==========================================================================
# Hooks
# ==========================================================================

@case
def hook_protocol():
    check("the event vocabulary is complete", len(H.EVENTS) == 27, str(len(H.EVENTS)))
    spec = H.HookSpec("PreToolUse", "guard")

    out = H.parse_hook_output(H.HookRun(exit_code=2, stdout='{"decision":"approve"}',
                                        stderr="no"), spec)
    check("exit 2 overrides a JSON approval", out.permission == "deny")

    out = H.parse_hook_output(H.HookRun(
        exit_code=0,
        stdout='{"hookSpecificOutput":{"hookEventName":"PreToolUse","permissionDecision":"deny"}}'),
        spec)
    check("a hook written to the documented schema is understood", out.permission == "deny")

    out = H.parse_hook_output(H.HookRun(
        exit_code=0, stdout='{"hookSpecificOutput":{"permissionDecision":"allow"}}'),
        H.HookSpec("PostToolUse", "x"))
    check("a PostToolUse hook cannot grant permission", out.permission is None)

    out = H.parse_hook_output(H.HookRun(exit_code=0, timed_out=True), spec)
    check("a permission hook that times out denies", out.permission == "deny")

    agg = H.aggregate([H.HookOutcome(permission="deny", blocking_error="x",
                                     updated_input={"a": 1})])
    check("a hook that denied cannot also rewrite the input", agg.updated_input is None)
    check("timeouts are per purpose",
          H.default_timeout_for("SessionEnd") < H.default_timeout_for("PreToolUse"))

    class Boom:
        def run(self, s, p): raise OSError("enoent")
    agg = H.run_hooks([spec], "PreToolUse", {}, Boom(), tool_name="Bash")
    check("a crashing hook neither stops the agent nor approves",
          agg.permission is None and not agg.blocked)


# ==========================================================================
# Skills
# ==========================================================================

@case
def skill_loading():
    d = tempfile.mkdtemp()
    root = os.path.join(d, "skills")
    for name, front in [("block", "description: X\npaths:\n  - src/**\n  - tests/**"),
                        ("inline", "description: Y\npaths: docs/**"),
                        ("plain", "description: Z")]:
        p = os.path.join(root, name); os.makedirs(p)
        with open(os.path.join(p, "SKILL.md"), "w") as fh:
            fh.write(f"---\n{front}\n---\nbody\n")
    uncond, cond = SK.load_all([(root, "project")])
    check("block-style YAML paths still make a skill conditional",
          sorted(s.name for s in cond) == ["block", "inline"],
          f"uncond={[s.name for s in uncond]}")
    check("a skill without paths is unconditional", [s.name for s in uncond] == ["plain"])

    check("'*' does not cross a path separator",
          not SK.matches_gitignore("src/a/b.py", "src/*.py"))
    check("'*' matches within a segment", SK.matches_gitignore("src/a.py", "src/*.py"))
    check("a bare name matches at any depth", SK.matches_gitignore("pkg/foo.py", "foo.py"))
    check("'**' crosses separators", SK.matches_gitignore("src/a/b.py", "src/**"))
    check("skills are hashable", len({SK.Skill("a", "d", "b", "/x")}) == 1)

    activated = SK.activate_for_paths(cond, [os.path.join(d, "src", "x.py")], d)
    check("a matching file activates exactly its skill",
          [s.name for s in activated] == ["block"], str([s.name for s in activated]))


# ==========================================================================
# Cost
# ==========================================================================

@case
def cost_guardrails():
    try:
        CM.CostMeter(CM.pricing_for("providerA", "unknown"), max_cost_usd=0.01,
                     model_id="unknown")
        check("an unenforceable cap is refused", False)
    except ValueError:
        check("an unenforceable cap is refused", True)
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        CM.CostMeter(None, max_cost_usd=0.01, on_untracked="warn")
        check("accepting an unenforceable cap is possible but loud", len(w) == 1)

    os.environ["AGENT_MAX_COST_USD"] = ""
    check("a blank budget env var means unset", CM.resolve_budget(None, None) is None)
    os.environ["AGENT_MAX_COST_USD"] = "2.5"
    check("a real budget env var is read", CM.resolve_budget(None, None) == 2.5)
    del os.environ["AGENT_MAX_COST_USD"]

    check("provider matching is case-insensitive",
          CM.pricing_for("ProviderA", "model-x-1") is not None)
    pricing = CM.pricing_for("providerA", "model-x-1")
    usage = {"prompt": 1_000_000.0, "output": 500_000}
    check("both ledgers price identical usage identically",
          abs(CM.CostMeter(pricing).add_turn(usage)
              - CM.CostMeter(pricing).add_maintenance("c", usage)) < 1e-9)


# ==========================================================================
# Tools — end to end against the real filesystem
# ==========================================================================

@case
def file_tools():
    d = tempfile.mkdtemp()
    ctx = Ctx(d)
    path = os.path.join(d, "app.py")
    WriteTool.call({"file_path": path, "content": "x\ny\nx\n"}, ctx)
    out = ReadTool.call({"file_path": path}, ctx)
    check("read output is line-numbered for citation", out.content.startswith("1\tx"))

    res = EditTool.call({"file_path": path, "old_string": "x", "new_string": "z"}, ctx)
    check("an ambiguous edit is refused with a count",
          not res.ok and res.data["occurrences"] == 2)
    res = EditTool.call({"file_path": path, "old_string": "x", "new_string": "z",
                         "replace_all": True}, ctx)
    check("replace_all resolves the ambiguity and the file changed",
          res.ok and open(path).read() == "z\ny\nz\n")

    fresh = Ctx(d)
    check("editing an unread file is refused",
          not EditTool.validate_input({"file_path": path, "old_string": "z",
                                       "new_string": "w"}, fresh).ok)
    ReadTool.call({"file_path": path}, fresh)
    os.utime(path, (time.time() + 5, time.time() + 5))
    check("editing a file that changed underneath is refused",
          not EditTool.validate_input({"file_path": path, "old_string": "z",
                                       "new_string": "w"}, fresh).ok)

    open(os.path.join(d, "b.txt"), "w").write("needle here\n")
    res = GrepTool.call({"pattern": "needle"}, ctx)
    check("grep finds the file", "b.txt" in res.content)
    res = GlobTool.call({"pattern": "*.py"}, ctx)
    check("glob finds the file", "app.py" in res.content)


@case
def shell_is_gated():
    ctx = Ctx(tempfile.mkdtemp())
    t = make_shell_tool(UnconfiguredSandbox(), SandboxPolicy())
    check("read-only classification is per command",
          t.is_read_only({"command": "ls"}) and not t.is_read_only({"command": "ls && rm -rf /"}))
    check("a destructive command asks even inside the sandbox",
          t.check_permissions({"command": "rm -rf /x"}, ctx).behavior == "ask")
    check("a sandboxed command needs no prompt",
          t.check_permissions({"command": "ls"}, ctx).behavior == "allow")
    res = t.call({"command": "ls"}, ctx)
    check("the refusing sandbox surfaces as a tool error, not an exception",
          not res.ok and res.code == "sandbox_not_configured")
    check("the escape hatch is absent unless enabled",
          "dangerouslyDisableSandbox" not in t.input_schema["properties"])


if __name__ == "__main__":
    print(f"ran {COUNT} assertions")
    if FAILURES:
        print(f"\n{len(FAILURES)} FAILED:")
        for f in FAILURES:
            print(f"  - {f}")
        raise SystemExit(1)
    print("all pass")
