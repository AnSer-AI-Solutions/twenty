from __future__ import annotations

import os
import pathlib
import stat
import subprocess
import tempfile
import unittest

SCRIPT = pathlib.Path(__file__).parents[1] / "twenty-run"

# Sources the wrapper and replaces run_compose, so a dispatch is observable as
# the argument vector docker compose would have received. $0 is the harness
# name, never the wrapper path, so sourcing does not trigger main.
DISPATCH_HARNESS = """
set -euo pipefail
script="$1"
log="$2"
shift 2
source "$script"
run_compose() { printf '%s\\n' "$@" >"$log"; }
main "$@"
"""
HARNESS_ARGV0 = "twenty-run-dispatch-harness"

# Every flag here is destructive or state-changing on the verb it is paired
# with: -v/--volumes/--rmi drop the db-data volume the CRM lives on,
# --force-recreate and --build replace healthy containers, --scale server=0 and
# a bare service name reduce what is running.
DANGEROUS_UP_ARGS = (
    ["--force-recreate"],
    ["--build"],
    ["--no-deps", "db"],
    ["--scale", "server=0"],
    ["-d", "--force-recreate"],
    ["db"],
)
DANGEROUS_DOWN_ARGS = (
    ["-v"],
    ["--volumes"],
    ["--rmi", "all"],
    ["--remove-orphans"],
    ["-t", "0"],
    ["db"],
)
# Not destructive, but still unknown to a wrapper that owns the whole argument
# vector, so they must be refused rather than forwarded.
UNKNOWN_EXTRA_ARGS = (
    ["--help"],
    ["--"],
    [""],
    ["--dry-run"],
    ["extra", "args"],
)


class TwentyRunTestCase(unittest.TestCase):
    """Shared harness: a PATH that can only resolve fake docker and bws."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.tmp_path = pathlib.Path(self.tmp.name)

        self.call_log = self.tmp_path / "external-calls.log"
        self.compose_log = self.tmp_path / "compose-args.log"
        self.missing_token = self.tmp_path / "no-such-token"

        self.shim_dir = self.tmp_path / "shims"
        self.shim_dir.mkdir()
        for name in ("docker", "bws"):
            self._write_shim(name)

    def _write_shim(self, name: str) -> None:
        shim = self.shim_dir / name
        shim.write_text(
            "#!/usr/bin/env bash\n"
            f'printf "%s\\n" "{name} $*" >>"$TWENTY_RUN_TEST_CALL_LOG"\n'
            "exit 0\n"
        )
        shim.chmod(shim.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    def env(self) -> dict[str, str]:
        # PATH holds the shims plus the system directories tr/env need. It
        # deliberately excludes /usr/local/bin and ~/.local/bin so no real bws
        # is reachable by name from a test.
        return {
            "PATH": f"{self.shim_dir}:/usr/bin:/bin:/usr/sbin:/sbin",
            "HOME": str(self.tmp_path),
            "BWS_TOKEN_FILE": str(self.missing_token),
            "TWENTY_RUN_TEST_CALL_LOG": str(self.call_log),
        }

    def reset_logs(self) -> None:
        """Clear the observations between subtests, keeping the shims."""
        for path in (self.call_log, self.compose_log):
            path.unlink(missing_ok=True)

    def external_calls(self) -> list[str]:
        """The command lines the fake docker and bws shims saw."""
        if not self.call_log.exists():
            return []
        return self.call_log.read_text().splitlines()

    def dispatch(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the real dispatch with run_compose stubbed out."""
        return subprocess.run(
            [
                "bash",
                "-c",
                DISPATCH_HARNESS,
                HARNESS_ARGV0,
                str(SCRIPT),
                str(self.compose_log),
                *args,
            ],
            capture_output=True,
            text=True,
            env=self.env(),
            check=False,
        )

    def compose_args(self) -> list[str] | None:
        """The argv docker compose was handed, or None if it was never run."""
        if not self.compose_log.exists():
            return None
        return self.compose_log.read_text().splitlines()

    def wrapper(self, *args: str) -> subprocess.CompletedProcess[str]:
        """Run the wrapper end to end, with no token and no reachable tools."""
        return subprocess.run(
            [str(SCRIPT), *args],
            capture_output=True,
            text=True,
            env=self.env(),
            check=False,
        )


class SupportedShapeTest(TwentyRunTestCase):
    def test_should_deploy_detached_and_remove_orphans_when_up_has_no_args(self):
        result = self.dispatch("up")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_args(), ["up", "-d", "--remove-orphans"])

    def test_should_default_to_up_when_no_action_is_given(self):
        result = self.dispatch()

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_args(), ["up", "-d", "--remove-orphans"])

    def test_should_stop_without_touching_volumes_when_down_has_no_args(self):
        result = self.dispatch("down")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_args(), ["down"])

    def test_should_keep_quiet_config_behaviour(self):
        result = self.dispatch("config")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.compose_args(), ["config", "--quiet"])

    def test_should_still_forward_arguments_for_read_only_verbs(self):
        # pull, ps and logs cannot delete state, so their pass-through is
        # unchanged; this pins that the guard did not widen its reach.
        cases = {
            ("pull",): ["pull"],
            ("pull", "server"): ["pull", "server"],
            ("ps",): ["ps"],
            ("ps", "-a"): ["ps", "-a"],
            ("logs",): ["logs"],
            ("logs", "-f", "worker"): ["logs", "-f", "worker"],
        }
        for args, expected in cases.items():
            with self.subTest(args=args):
                self.reset_logs()
                result = self.dispatch(*args)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.compose_args(), expected)

    def test_should_reject_an_unknown_action_with_usage(self):
        result = self.dispatch("nuke")

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: twenty-run {config|up|pull|ps|logs|down}", result.stderr)
        self.assertIsNone(self.compose_args())


class ArgumentGuardTest(TwentyRunTestCase):
    def assert_refused(self, action: str, extra: list[str]) -> None:
        result = self.dispatch(action, *extra)

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn(f"twenty-run {action} takes no extra arguments", result.stderr)
        self.assertIn("usage: twenty-run {config|up|pull|ps|logs|down}", result.stderr)
        self.assertIsNone(
            self.compose_args(),
            f"{action} {extra} reached docker compose",
        )
        self.assertEqual(self.external_calls(), [])

    def test_should_refuse_dangerous_extra_args_on_up(self):
        for extra in DANGEROUS_UP_ARGS:
            with self.subTest(extra=extra):
                self.reset_logs()
                self.assert_refused("up", extra)

    def test_should_refuse_dangerous_extra_args_on_down(self):
        for extra in DANGEROUS_DOWN_ARGS:
            with self.subTest(extra=extra):
                self.reset_logs()
                self.assert_refused("down", extra)

    def test_should_refuse_unknown_extra_args_on_both_verbs(self):
        for action in ("up", "down"):
            for extra in UNKNOWN_EXTRA_ARGS:
                with self.subTest(action=action, extra=extra):
                    self.reset_logs()
                    self.assert_refused(action, extra)

    def test_should_name_the_rejected_arguments(self):
        result = self.dispatch("down", "-v")

        self.assertIn("(got: -v)", result.stderr)


class FailClosedTest(TwentyRunTestCase):
    """End-to-end runs of the real wrapper, with nothing real reachable."""

    def test_shims_record_calls(self):
        # Positive control: without this, "no external calls" proves nothing.
        subprocess.run(
            ["bash", "-c", "docker compose up -d; bws run"],
            capture_output=True,
            text=True,
            env=self.env(),
            check=True,
        )

        self.assertEqual(self.external_calls(), ["docker compose up -d", "bws run"])

    def test_should_refuse_before_reading_the_bitwarden_token(self):
        # The token is missing, so a guard that ran late would fail with the
        # token error instead of the argument error.
        for args in (["up", "--force-recreate"], ["down", "-v"]):
            with self.subTest(args=args):
                self.reset_logs()
                result = self.wrapper(*args)

                self.assertEqual(result.returncode, 2)
                self.assertIn("takes no extra arguments", result.stderr)
                self.assertNotIn("missing Twenty runtime token", result.stderr)
                self.assertEqual(self.external_calls(), [])

    def test_should_reach_the_token_step_for_a_supported_invocation(self):
        # The mirror of the test above: bare up/down clear the guard and stop
        # at the token, which is still upstream of bws and docker.
        for args in (["up"], ["down"], []):
            with self.subTest(args=args):
                self.reset_logs()
                result = self.wrapper(*args)

                self.assertEqual(result.returncode, 1)
                self.assertIn("missing Twenty runtime token", result.stderr)
                self.assertNotIn("takes no extra arguments", result.stderr)
                self.assertEqual(self.external_calls(), [])

    def test_should_refuse_an_unknown_action_before_the_token(self):
        result = self.wrapper("nuke")

        self.assertEqual(result.returncode, 2)
        self.assertIn("usage: twenty-run", result.stderr)
        self.assertNotIn("missing Twenty runtime token", result.stderr)
        self.assertEqual(self.external_calls(), [])


class ScriptHygieneTest(unittest.TestCase):
    def test_should_parse_as_bash(self):
        result = subprocess.run(
            ["bash", "-n", str(SCRIPT)],
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_should_be_executable(self):
        self.assertTrue(os.access(SCRIPT, os.X_OK))

    def test_should_never_forward_operator_arguments_to_up_or_down(self):
        # A regression fence on the text itself: the original bug was a "$@"
        # on the up and down branches.
        body = SCRIPT.read_text()

        self.assertNotIn('run_compose up -d --remove-orphans "$@"', body)
        self.assertNotIn('run_compose down "$@"', body)


if __name__ == "__main__":
    unittest.main()
