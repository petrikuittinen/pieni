"""Offline tests for scripts/install.sh.

Nothing here touches the network or the real home directory: every test copies
the checkout into a temporary directory and installs into a temporary prefix.
When the dependency check has to be exercised, a stub interpreter on PATH stands
in for python3, so pip is never invoked.

    python3 -m unittest tests.test_install
"""

import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
INSTALLER = REPO / "scripts" / "install.sh"
BASH = shutil.which("bash")


class FakePython:
    """A stub interpreter that answers the installer's check calls."""

    PRESENT = """#!/usr/bin/env bash
case "$*" in
    *sys.version.split*) printf '3.12.9\\n' ;;
    *"import openai, openrouter, rich"*) exit 0 ;;
esac
exit 0
"""

    BROKEN = """#!/usr/bin/env bash
case "$*" in
    *sys.version.split*) printf '3.12.9\\n' ;;
    *"import openai, openrouter, rich"*) exit 1 ;;
esac
exit 0
"""

    # Creates a venv whose interpreter still cannot import the dependencies.
    MAKES_VENV = """#!/usr/bin/env bash
case "$*" in
    *sys.version.split*) printf '3.12.9\\n' ;;
    *"import openai, openrouter, rich"*) exit 1 ;;
    "-m venv "*)
        mkdir -p "$3/bin"
        cat > "$3/bin/python" <<'INNER'
#!/usr/bin/env bash
case "$*" in
    *"import openai, openrouter, rich"*) exit 1 ;;
esac
exit 0
INNER
        chmod +x "$3/bin/python"
        ;;
esac
exit 0
"""


@unittest.skipUnless(BASH, "bash is not available")
class InstallScriptTests(unittest.TestCase):

    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempdir.cleanup)
        self.root = Path(self.tempdir.name)
        self.repo = self.root / "repo"
        self.prefix = self.root / "prefix"
        self.copy_checkout()

    def copy_checkout(self):
        """A throwaway copy of the files the installer needs."""
        (self.repo / "scripts").mkdir(parents=True)
        for name in ("pieni", "pieni.py", "requirements.txt"):
            shutil.copy2(REPO / name, self.repo / name)
        shutil.copy2(INSTALLER, self.repo / "scripts" / "install.sh")
        (self.repo / "scripts" / "install.sh").chmod(0o755)
        (self.repo / "pieni").chmod(0o755)

    def install(self, *arguments, environment=None, path=None, cwd=None):
        env = dict(os.environ)
        env["HOME"] = str(self.root / "home")  # never the real home directory
        (self.root / "home").mkdir(exist_ok=True)
        if path:
            # Prepend, so the rest of the system tools stay reachable.
            env["PATH"] = f"{path}{os.pathsep}{env['PATH']}"
        if environment:
            env.update(environment)
        return subprocess.run(
            [BASH, str(self.repo / "scripts" / "install.sh"),
             "--prefix", str(self.prefix), *arguments],
            capture_output=True, text=True, env=env, cwd=cwd or self.repo, timeout=60)

    def stub_python(self, body, name="fake-python"):
        bin_dir = self.root / "bin"
        bin_dir.mkdir(exist_ok=True)
        stub = bin_dir / name
        stub.write_text(body, encoding="utf-8")
        stub.chmod(0o755)
        return bin_dir, stub

    # -- the script itself ---------------------------------------------------

    def test_script_parses(self):
        completed = subprocess.run([BASH, "-n", str(INSTALLER)], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0, completed.stderr)

    def test_help_explains_the_options(self):
        completed = subprocess.run([BASH, str(INSTALLER), "--help"],
                                   capture_output=True, text=True)
        self.assertEqual(completed.returncode, 0)
        for text in ("--prefix", "--skip-deps", "--dry-run", "Uninstall"):
            self.assertIn(text, completed.stdout)

    def test_unknown_option_fails_with_usage(self):
        completed = self.install("--nonsense")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("unknown option", completed.stderr)
        self.assertIn("usage: scripts/install.sh", completed.stderr)

    def test_prefix_without_a_value_fails(self):
        completed = subprocess.run([BASH, str(INSTALLER), "--prefix"],
                                   capture_output=True, text=True)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("needs a directory", completed.stderr)

    def test_missing_source_file_is_reported(self):
        (self.repo / "pieni.py").unlink()
        completed = self.install("--skip-deps")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("is missing", completed.stderr)

    def test_syntax_error_in_the_launcher_is_reported(self):
        (self.repo / "pieni").write_text("#!/usr/bin/env bash\nif then\n", encoding="utf-8")
        completed = self.install("--skip-deps")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("syntax error", completed.stderr)
        self.assertFalse((self.prefix / "bin" / "pieni").exists())

    def test_launcher_without_a_shebang_is_reported(self):
        launcher = self.repo / "pieni"
        launcher.write_text("exec python3 pieni.py\n", encoding="utf-8")
        launcher.chmod(0o755)
        completed = self.install("--skip-deps")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("no shebang", completed.stderr)

    # -- dry run -------------------------------------------------------------

    def test_dry_run_changes_nothing(self):
        completed = self.install("--dry-run", "--skip-deps")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[dry-run] mkdir -p", completed.stdout)
        self.assertIn("[dry-run] ln -s", completed.stdout)
        self.assertIn("smoke test skipped", completed.stdout)
        self.assertFalse(self.prefix.exists())

    # -- installing ----------------------------------------------------------

    def test_installs_a_command_that_works(self):
        completed = self.install("--skip-deps")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        dest = self.prefix / "bin" / "pieni"
        self.assertTrue(dest.is_symlink(), completed.stdout)
        self.assertEqual(dest.resolve(), (self.repo / "pieni").resolve())
        self.assertTrue(os.access(dest, os.X_OK))
        self.assertIn("--skip-deps", completed.stderr)  # says deps were not checked
        self.assertIn("dependencies: not checked", completed.stdout)

        # The installed command really runs, through the symlink.
        run = subprocess.run([str(dest), "--help"], capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("usage: pieni", run.stdout)

    def test_installed_command_reports_the_banner(self):
        self.install("--skip-deps")
        run = subprocess.run([str(self.prefix / "bin" / "pieni")],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(run.returncode, 0)
        self.assertIn("Pieni agent v", run.stdout)

    def test_relative_prefix_is_made_absolute(self):
        completed = subprocess.run(
            [BASH, "scripts/install.sh", "--prefix", "local-prefix", "--skip-deps"],
            capture_output=True, text=True, cwd=self.repo, timeout=60,
            env={**os.environ, "HOME": str(self.root / "home")})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertTrue((self.repo / "local-prefix" / "bin" / "pieni").is_symlink())
        self.assertIn(f"uninstall:    rm {self.repo}/local-prefix/bin/pieni", completed.stdout)

    def test_reinstall_is_idempotent(self):
        self.assertEqual(self.install("--skip-deps").returncode, 0)
        again = self.install("--skip-deps")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertIn("already points here", again.stdout)

    def test_existing_foreign_file_is_not_overwritten(self):
        (self.prefix / "bin").mkdir(parents=True)
        other = self.prefix / "bin" / "pieni"
        other.write_text("#!/bin/sh\necho someone else's pieni\n", encoding="utf-8")
        completed = self.install("--skip-deps")
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("already exists", completed.stderr)
        self.assertEqual(other.read_text(encoding="utf-8"),
                         "#!/bin/sh\necho someone else's pieni\n")

    def test_missing_executable_bit_is_added(self):
        launcher = self.repo / "pieni"
        launcher.chmod(0o644)
        self.assertFalse(os.access(launcher, os.X_OK))
        completed = self.install("--skip-deps")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("adding the executable bit", completed.stdout)
        self.assertTrue(os.access(launcher, os.X_OK))
        self.assertTrue(stat.S_IMODE(launcher.stat().st_mode) & stat.S_IXUSR)

    def test_missing_python_is_reported(self):
        completed = self.install("--skip-deps", environment={"PYTHON": "definitely-not-here"})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("not found", completed.stderr)
        self.assertFalse((self.prefix / "bin").exists())

    def test_python_that_is_too_old_is_reported(self):
        bin_dir, stub = self.stub_python("#!/usr/bin/env bash\nexit 1\n", "old-python")
        completed = self.install("--skip-deps", environment={"PYTHON": str(stub)})
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("too old", completed.stderr)

    # -- dependencies --------------------------------------------------------

    def test_present_dependencies_are_detected_and_pip_is_not_used(self):
        bin_dir, stub = self.stub_python(FakePython.PRESENT)
        completed = self.install(environment={"PYTHON": str(stub)}, path=bin_dir)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn(f"deps:     already available with {stub}", completed.stdout)
        self.assertNotIn("pip install", completed.stdout)
        self.assertFalse((self.repo / ".venv").exists())
        self.assertIn(f"dependencies: already available with {stub}", completed.stdout)

    def test_dependencies_that_stay_missing_are_reported(self):
        bin_dir, stub = self.stub_python(FakePython.MAKES_VENV)
        completed = self.install(environment={"PYTHON": str(stub)}, path=bin_dir)
        self.assertNotEqual(completed.returncode, 0, completed.stdout)
        self.assertIn("missing; installing openai, openrouter and rich", completed.stdout)
        self.assertIn("dependencies are still missing", completed.stderr)
        self.assertIn("pip install -r", completed.stderr)

    def test_venv_that_produces_no_interpreter_is_reported(self):
        bin_dir, stub = self.stub_python(FakePython.BROKEN)
        completed = self.install(environment={"PYTHON": str(stub)}, path=bin_dir)
        self.assertNotEqual(completed.returncode, 0)
        self.assertIn("cannot create", completed.stderr)
        self.assertIn("python3-venv", completed.stderr)
        self.assertFalse((self.repo / ".venv").exists())

    def test_dry_run_does_not_create_a_venv(self):
        bin_dir, stub = self.stub_python(FakePython.BROKEN)
        completed = self.install("--dry-run", environment={"PYTHON": str(stub)}, path=bin_dir)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertIn("[dry-run]", completed.stdout)
        self.assertFalse((self.repo / ".venv").exists())

    # -- path handling -------------------------------------------------------

    def test_path_hint_is_printed_for_a_prefix_outside_path(self):
        completed = self.install("--skip-deps", environment={"PATH": "/usr/bin:/bin"})
        self.assertEqual(completed.returncode, 0)
        self.assertIn("is not on your PATH", completed.stderr)
        self.assertIn(f'export PATH="{self.prefix}/bin:$PATH"', completed.stderr)

    def test_no_path_hint_when_the_prefix_is_on_path(self):
        completed = self.install("--skip-deps",
                                 environment={"PATH": f"{self.prefix}/bin:/usr/bin:/bin"})
        self.assertEqual(completed.returncode, 0, completed.stderr)
        self.assertNotIn("is not on your PATH", completed.stderr)


if __name__ == "__main__":
    unittest.main()
