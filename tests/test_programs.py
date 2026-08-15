"""The Studio's Kimiya programs: kept, checked, and run on the rails.

The built-ins are read-only worked examples; the photographer's own are
files under the Studio, name-validated, shadow-proof, resolved against
the application's audited kernels, and queued with the same provider
bundle and credential handling as every shipped program.
"""

from __future__ import annotations

import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from opencull_gui.jobs import JobError, JobManager, kimiya_arguments  # noqa: E402
from opencull_gui.programs import (  # noqa: E402
    BUILT_INS,
    ProgramError,
    ProgramStore,
)

ROOT = Path(__file__).resolve().parent.parent


class StoreTests(unittest.TestCase):
    def setUp(self):
        self._temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self._temporary.name) / "Programs"
        self.addCleanup(self._temporary.cleanup)
        self.store = ProgramStore(self.directory, ROOT,
                                  python=str(ROOT / ".venv/bin/python"))

    def test_the_builtins_are_all_listed_with_their_purposes(self):
        names = [item["name"] for item in self.store.catalogue()
                 if item["kind"] == "built-in"]
        self.assertEqual(names, [name for name, _purpose in BUILT_INS])

    def test_a_kept_program_appears_after_the_builtins(self):
        self.store.save("my-experiment", "-- mine\nparam output: text\n")
        listed = self.store.catalogue()
        self.assertEqual(listed[-1]["name"], "my-experiment.kim")
        self.assertEqual(listed[-1]["kind"], "yours")
        self.assertEqual(listed[-1]["purpose"], "mine")

    def test_a_builtin_cannot_be_shadowed(self):
        with self.assertRaises(ProgramError):
            self.store.save("opencull.kim", "-- impostor\n")

    def test_a_name_with_a_directory_in_it_is_refused(self):
        with self.assertRaises(ProgramError):
            self.store.read("../secrets.kim")
        with self.assertRaises(ProgramError):
            self.store.save("../escape", "-- no\n")

    def test_an_ugly_name_is_refused_with_the_rule(self):
        for name in ("MY PROGRAM", "über.kim", "a", ""):
            with self.assertRaises(ProgramError):
                self.store.save(name, "-- x\n")

    def test_duplicating_a_builtin_notes_its_origin(self):
        kept = self.store.duplicate("control_zones.kim", "my-zones")
        text = kept.read_text()
        self.assertIn("Duplicated from control_zones.kim", text)
        self.assertIn("param photos", text)

    def test_only_your_own_programs_can_be_deleted(self):
        with self.assertRaises(ProgramError):
            self.store.delete("opencull.kim")
        self.store.save("my-experiment", "-- mine\n")
        self.store.delete("my-experiment.kim")
        self.assertEqual(
            [item for item in self.store.catalogue()
             if item["kind"] == "yours"], [])

    def test_parameters_are_read_off_the_head(self):
        told = self.store.parameters("protect_then_reveal.kim")
        names = {item["name"] for item in told}
        self.assertIn("photos", names)
        self.assertIn("rounds", names)
        rounds = next(item for item in told if item["name"] == "rounds")
        self.assertEqual(rounds["type"], "num")
        self.assertEqual(rounds["default"], "3")

    def test_kernels_resolve_beside_the_program_then_to_the_apps(self):
        self.store.directory.mkdir(parents=True, exist_ok=True)
        (self.store.directory / "my_kernel.py").write_text("def f():\n    pass\n")
        resolved = self.store.resolve_uses(
            'use "agents.kim"\nuse python "my_kernel.py"\n'
            'use python "treatment_kernel.py"\n',
            agents=ROOT / "agents.kim")
        self.assertIn(str(self.store.directory / "my_kernel.py"), resolved)
        self.assertIn(str(ROOT / "treatment_kernel.py"), resolved)

    def test_a_kernel_nobody_has_is_named_in_the_refusal(self):
        with self.assertRaises(ProgramError) as caught:
            self.store.resolve_uses('use python "nowhere_kernel.py"\n',
                                    agents=ROOT / "agents.kim")
        self.assertIn("nowhere_kernel.py", str(caught.exception))

    def test_the_compiler_accepts_a_builtin_and_a_duplicate(self):
        ok, said = self.store.check("protect_then_reveal.kim")
        self.assertTrue(ok, said)
        self.store.duplicate("control_zones.kim", "my-zones")
        ok, said = self.store.check("my-zones.kim")
        self.assertTrue(ok, said)

    def test_the_compiler_refuses_a_broken_program_verbatim(self):
        self.store.save("broken", "-- broken\nthis is not kimiya at all\n")
        ok, said = self.store.check("broken.kim")
        self.assertFalse(ok)
        self.assertIn("error", said.lower())


class RunRailTests(unittest.TestCase):
    """A user program is queued exactly like a shipped one."""

    def test_the_command_speaks_the_programs_own_parameters(self):
        program, arguments = kimiya_arguments({
            "kind": "kimiya_program", "program": "my-zones.kim",
            "parameters": {"photos": "/p", "output": "/p/out.json",
                           "rounds": "4"}})
        self.assertEqual(program, "my-zones.kim")
        self.assertEqual(arguments,
                         ["photos=/p", "output=/p/out.json", "rounds=4"])

    def manager(self, root: Path) -> JobManager:
        return JobManager(
            root / "jobs.json", ROOT, providers=None, autostart=False,
            command_builder=lambda job: [],
            program_checker=lambda path: None)

    def test_a_run_without_an_output_is_refused(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProgramStore(root / "Programs", ROOT)
            store.save("my-run", "-- x\nparam output: text\n")
            manager = self.manager(root)
            try:
                with self.assertRaises(JobError):
                    manager.add_program("my-run.kim", {}, store=store)
            finally:
                manager.shutdown()

    def test_a_run_is_queued_with_its_parameters_and_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProgramStore(root / "Programs", ROOT)
            store.save("my-run", "-- x\nparam output: text\n")
            manager = self.manager(root)
            try:
                state = manager.add_program(
                    "my-run.kim",
                    {"output": str(root / "out.json"), "rounds": 2},
                    store=store)
                job = state["jobs"][0]
                self.assertEqual(job["kind"], "kimiya_program")
                self.assertEqual(job["program"], "my-run.kim")
                self.assertEqual(job["parameters"]["rounds"], "2")
                self.assertTrue(job["log"].endswith("out.json.log"))
            finally:
                manager.shutdown()

    def test_a_kernel_the_program_cannot_reach_fails_at_queue_time(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            store = ProgramStore(root / "Programs", ROOT)
            store.save("my-run", '-- x\nuse python "nowhere.py"\n')
            manager = self.manager(root)
            try:
                with self.assertRaises(JobError) as caught:
                    manager.add_program(
                        "my-run.kim", {"output": str(root / "o.json")},
                        store=store)
                self.assertIn("nowhere.py", str(caught.exception))
            finally:
                manager.shutdown()


if __name__ == "__main__":
    unittest.main()
