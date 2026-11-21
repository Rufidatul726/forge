import asyncio
import logging
import os
import signal
import time

import pytest
from forge.execution import (
    CommandPermissionDeniedError,
    CommandPolicyError,
    CommandRunner,
    CommandValidationError,
)


def run_command(runner, argv, **kwargs):
    return asyncio.run(runner.run(argv, **kwargs))


def test_command_requires_approval_and_runs_with_workspace_cwd(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    proposals = []
    runner = CommandRunner(tmp_path)

    result = run_command(
        runner,
        ["python", "-c", "import os; print(os.getcwd())"],
        cwd="nested",
        approve=lambda proposal: proposals.append(proposal) or True,
    )

    assert result.exit_code == 0
    assert result.stdout.strip() == str(nested.resolve())
    assert result.working_directory == "nested"
    assert proposals[0].argv[0] == "python"
    assert proposals[0].executable_path.is_absolute()


def test_declined_command_does_not_start_process(tmp_path, caplog):
    runner = CommandRunner(tmp_path)
    with caplog.at_level(logging.WARNING, logger="forge.execution"):
        with pytest.raises(CommandPermissionDeniedError):
            run_command(
                runner,
                ["python", "-c", "print('must not run')"],
                approve=lambda proposal: False,
            )

    assert "command_denied" in caplog.text
    assert "must not run" not in caplog.text


@pytest.mark.parametrize("command", ["powershell", "cmd", "curl", "bash", "sh"])
def test_command_policy_rejects_shells_and_unlisted_executables(tmp_path, command):
    runner = CommandRunner(tmp_path)
    approval_called = False

    def approve(proposal):
        nonlocal approval_called
        approval_called = True
        return True

    with pytest.raises(CommandPolicyError):
        run_command(runner, [command, "-c", "echo blocked"], approve=approve)
    assert approval_called is False


def test_command_policy_rejects_explicit_executable_paths(tmp_path):
    runner = CommandRunner(tmp_path)
    with pytest.raises(CommandPolicyError, match="Executable paths"):
        run_command(
            runner,
            ["C:\\Windows\\System32\\cmd.exe", "/c", "echo blocked"],
            approve=lambda proposal: True,
        )


@pytest.mark.parametrize(
    "argv",
    [
        [],
        [" "],
        ["python", "\x00"],
        ["python", "x" * 17_000],
        ["python", "\ud800"],
    ],
)
def test_command_arguments_are_validated(tmp_path, argv):
    runner = CommandRunner(tmp_path)
    with pytest.raises(CommandValidationError):
        run_command(runner, argv, approve=lambda proposal: True)


def test_command_working_directory_must_stay_inside_workspace(tmp_path):
    runner = CommandRunner(tmp_path)
    with pytest.raises(CommandValidationError, match="relative"):
        run_command(
            runner,
            ["python", "--version"],
            cwd="..",
            approve=lambda proposal: True,
        )


def test_command_output_is_bounded_and_exit_code_preserved(tmp_path):
    runner = CommandRunner(tmp_path, max_output_bytes=32)
    result = run_command(
        runner,
        [
            "python",
            "-c",
            "import sys; print('x' * 1000); "
            "print('y' * 1000, file=sys.stderr); sys.exit(7)",
        ],
        approve=lambda proposal: True,
    )

    assert result.exit_code == 7
    assert len(result.stdout.encode("utf-8")) <= 32
    assert len(result.stderr.encode("utf-8")) <= 32
    assert result.stdout_truncated is True
    assert result.stderr_truncated is True


def test_command_environment_does_not_inherit_secret_variables(tmp_path, monkeypatch):
    monkeypatch.setenv("FORGE_TEST_SECRET", "must-not-be-inherited")
    runner = CommandRunner(tmp_path)
    result = run_command(
        runner,
        [
            "python",
            "-c",
            "import os; print(os.getenv('FORGE_TEST_SECRET', '')); "
            "print(os.getenv('UV_OFFLINE'))",
        ],
        approve=lambda proposal: True,
    )

    assert result.exit_code == 0
    assert result.stdout.splitlines() == ["", "1"]
    assert "must-not-be-inherited" not in result.stdout


def test_command_audit_log_omits_arguments_and_output(tmp_path, caplog):
    sensitive_text = "FORGE_PRIVATE_TEST_ARGUMENT"
    runner = CommandRunner(tmp_path)
    with caplog.at_level(logging.INFO, logger="forge.execution"):
        result = run_command(
            runner,
            ["python", "-c", f"print({sensitive_text!r})"],
            approve=lambda proposal: True,
        )

    assert result.stdout.strip() == sensitive_text
    assert sensitive_text not in caplog.text
    assert "command_completed" in caplog.text


def test_command_timeout_terminates_process_and_returns_bounded_result(tmp_path):
    runner = CommandRunner(tmp_path)
    result = run_command(
        runner,
        ["python", "-c", "import time; time.sleep(10)"],
        timeout_seconds=0.1,
        approve=lambda proposal: True,
    )

    assert result.timed_out is True
    assert result.exit_code is not None
    assert result.duration_ms < 5000


@pytest.mark.skipif(os.name != "nt", reason="Verifies Windows process-tree termination")
def test_command_timeout_terminates_child_processes_on_windows(tmp_path):
    runner = CommandRunner(tmp_path)
    result = run_command(
        runner,
        [
            "python",
            "-c",
            "import subprocess, sys, time; "
            "child = subprocess.Popen([sys.executable, '-c', "
            "'import time; time.sleep(30)']); "
            "print(child.pid, flush=True); time.sleep(30)",
        ],
        timeout_seconds=1,
        approve=lambda proposal: True,
    )

    assert result.timed_out is True
    child_pid = int(result.stdout.strip())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(child_pid, 0)
        except OSError:
            break
        time.sleep(0.05)
    else:
        os.kill(child_pid, signal.SIGTERM)
        pytest.fail("Timed-out command left its child process running")


def test_cancellation_terminates_process_and_propagates(tmp_path):
    runner = CommandRunner(tmp_path)

    async def cancel_running_command():
        task = asyncio.create_task(
            runner.run(
                ["python", "-c", "import time; time.sleep(10)"],
                timeout_seconds=30,
                approve=lambda proposal: True,
            )
        )
        await asyncio.sleep(0.5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_running_command())


def test_command_timeout_validation_happens_before_approval(tmp_path):
    runner = CommandRunner(tmp_path)
    approval_called = False

    def approve(proposal):
        nonlocal approval_called
        approval_called = True
        return True

    with pytest.raises(CommandValidationError, match="timeout"):
        run_command(
            runner,
            ["python", "--version"],
            timeout_seconds=301,
            approve=approve,
        )
    assert approval_called is False
