"""Worked example: wire the kit into a working agent, then drive it.

Run it: ``python3 selftest.py``

This is the shortest honest demonstration of the integration surface. Read
``build_agent`` first — everything after it is a scripted conversation proving
the wiring holds end to end.

For the defect-regression suite, see ``tests.py``. The split matters: this file
answers "how do I use it", that one answers "is it still correct". Conflating
them is how the previous version of this file ended up asserting only the parts
that already worked.
"""
from __future__ import annotations

import os
import sys
import tempfile
from dataclasses import dataclass, field
from typing import Any, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [HERE, os.path.join(HERE, "tools"), os.path.dirname(HERE)]

import loop as L
import registry as R
import skills_loader as SK
from control import make_ask_user_tool, make_todo_tool
from files import EditTool, ReadTool, WriteTool
from meta import make_skill_tool, make_tool_search
from permissions import PermissionContext, Rule
from sandbox_backend import SandboxPolicy, UnconfiguredSandbox
from search import GlobTool, GrepTool
from shell import make_shell_tool


@dataclass
class Ctx:
    """The minimum a tool needs to know about the world."""

    cwd: str
    session_dir: str
    read_files: dict[str, float] = field(default_factory=dict)
    permission_mode: str = "default"
    _abort: bool = False

    def aborted(self) -> bool:
        return self._abort


class ScriptedModel:
    """Replays a list of responses. Signature matches the Model protocol."""

    def __init__(self, script: Sequence[dict[str, Any]]):
        self.script = list(script)
        self.max_output_tokens_seen: list[int | None] = []

    def complete(self, messages, tools, max_output_tokens=None):
        self.max_output_tokens_seen.append(max_output_tokens)
        return self.script.pop(0) if self.script else {"text": "done", "tool_calls": []}


def build_agent(workdir: str, skills_dir: str | None = None):
    """Assemble a working agent. This is the whole integration surface."""
    ctx = Ctx(cwd=workdir, session_dir=workdir)
    todo_store: dict[str, Any] = {}
    skills: list[SK.Skill] = []
    if skills_dir:
        unconditional, _conditional = SK.load_all([(skills_dir, "project")])
        skills.extend(unconditional)

    # The pool is held by reference, not by value: ToolSearch must see the
    # CURRENT pool (it mutates `loaded`), and the pool is legitimately rebuilt
    # between turns as external servers connect.
    pool_ref: dict[str, R.Pool] = {}
    builtin = [
        ReadTool, WriteTool, EditTool, GlobTool, GrepTool,
        make_shell_tool(UnconfiguredSandbox(), SandboxPolicy()),
        make_todo_tool(todo_store),
        make_ask_user_tool(lambda q: {q["questions"][0]["header"]: "yes"}),
        make_tool_search(lambda: pool_ref["pool"]),
        make_skill_tool(lambda: skills),
    ]
    pool_ref["pool"] = R.assemble(builtin, context_window_tokens=200_000)

    # `accept_edits` must name its edit tools explicitly; there is no way to
    # derive "is this an edit?" from the generic safety predicates.
    perm = PermissionContext(
        mode="default", edit_tools=frozenset({"Edit", "Write"})
    )
    return ctx, pool_ref, perm, todo_store, skills


def main() -> int:
    work = tempfile.mkdtemp()
    os.makedirs(os.path.join(work, "src"))
    app = os.path.join(work, "src", "app.py")
    with open(app, "w") as fh:
        fh.write("def greet():\n    return 'hello'\n")

    skills_root = os.path.join(work, ".claude", "skills", "review")
    os.makedirs(skills_root)
    with open(os.path.join(skills_root, "SKILL.md"), "w") as fh:
        fh.write("---\ndescription: Review a diff for correctness\n---\n# Review\nCheck it.\n")

    ctx, pool_ref, perm, todo_store, skills = build_agent(
        work, os.path.join(work, ".claude", "skills")
    )
    pool = pool_ref["pool"]

    # ---- the agent does a real piece of work -----------------------------
    model = ScriptedModel([
        {"text": "Looking.", "tool_calls": [("c1", "Read", {"file_path": app})]},
        {"text": "Fixing.", "tool_calls": [
            ("c2", "Edit", {"file_path": app, "old_string": "'hello'",
                            "new_string": "'hello, world'"})]},
        {"text": "Done — greet() now returns 'hello, world'.", "tool_calls": []},
    ])
    res = L.run(model, pool, ctx, PermissionContext(mode="bypass"),
                [{"role": "user", "content": "make greet friendlier"}],
                budget=L.Budget(max_turns=10), results_dir=work)

    assert res.stop is L.Stop.COMPLETED, res
    assert res.transitions == [L.Continue.NEXT_TURN, L.Continue.NEXT_TURN], res.transitions
    assert "hello, world" in open(app).read(), "the edit did not reach disk"

    # ---- a denied tool comes back as data, and the run continues ---------
    model = ScriptedModel([
        {"text": "", "tool_calls": [("c1", "Write", {"file_path": app, "content": "x"})]},
        {"text": "I was not allowed to write.", "tool_calls": []},
    ])
    res = L.run(model, pool, ctx, PermissionContext(rules=[Rule("Write", "deny")]),
                [{"role": "user", "content": "overwrite it"}], results_dir=work)
    assert res.stop is L.Stop.COMPLETED
    blocks = [m for m in res.messages if isinstance(m["content"], list)][0]["content"]
    assert blocks[0]["is_error"] and "denied" in blocks[0]["content"].lower(), blocks
    assert "hello, world" in open(app).read(), "a denied write must not reach disk"

    # ---- skills are discovered and loadable ------------------------------
    assert [s.name for s in skills] == ["review"]
    assert SK.render_index(skills, 200_000) == "- review: Review a diff for correctness"

    print("selftest: agent wired, work done, denial respected, skills indexed")
    print("for defect coverage run: python3 tests.py")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
