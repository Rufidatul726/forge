import asyncio

import pytest

from forge.agent import (
    AgentService,
    AgentTaskRequest,
    ExecutionPolicy,
    ExecutionPolicyConfig,
)
from forge.providers.errors import ProviderTimeoutError
from forge.providers.models import (
    ChatMessage,
    GenerationResult,
    ProviderResponse,
    ToolCall,
)


def run(coro):
    return asyncio.run(coro)


def test_autonomous_loop_reads_file_then_returns_summary(tmp_path):
    (tmp_path / "main.py").write_text("print('hello')\n", encoding="utf-8")

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return GenerationResult(
                    model=payload.model,
                    content=(
                        "<forge_tool_call>"
                        '{"name":"read_file","arguments":{"path":"main.py"}}'
                        "</forge_tool_call>"
                    ),
                    done=True,
                )
            return GenerationResult(
                model=payload.model,
                content="The repository entry point prints hello.",
                done=True,
            )

    provider = FakeProvider()
    result = run(
        AgentService(tmp_path, provider).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Explain main.py")],
            ),
            approve_write=lambda proposal: True,
            approve_command=lambda proposal: True,
        )
    )

    assert result.read_only is False
    assert result.generation.content == "The repository entry point prints hello."
    assert len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].name == "read_file"
    assert result.tool_outcomes[0].ok is True
    assert "print('hello')" in result.tool_outcomes[0].result


def test_autonomous_write_denial_is_reported_without_mutation(tmp_path):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return GenerationResult(
                    model=payload.model,
                    content=(
                        "<forge_tool_call>"
                        '{"name":"write_file","arguments":'
                        '{"path":"main.py","content":"print(42)\\n"}}'
                        "</forge_tool_call>"
                    ),
                    done=True,
                )
            return GenerationResult(
                model=payload.model,
                content="The write was not approved.",
                done=True,
            )

    target = tmp_path / "main.py"
    target.write_text("print('old')\n", encoding="utf-8")
    result = run(
        AgentService(tmp_path, FakeProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Update main.py")],
            ),
            approve_write=lambda proposal: False,
            approve_command=lambda proposal: True,
        )
    )

    assert target.read_text(encoding="utf-8") == "print('old')\n"
    assert len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].ok is False
    assert "not approved" in result.tool_outcomes[0].result


def test_structured_tool_call_native_execution(tmp_path):
    (tmp_path / "main.py").write_text("def add(a, b): return a + b\n", encoding="utf-8")

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                assert payload.tools
                assert any(t.name == "read_file" for t in payload.tools)
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(name="read_file", arguments={"path": "main.py"})],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="The file defines an add function.",
                done=True,
            )

    provider = FakeProvider()
    result = run(
        AgentService(tmp_path, provider).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="What does main.py do?")],
            ),
            approve_write=lambda proposal: True,
            approve_command=lambda proposal: True,
        )
    )

    assert result.read_only is False
    assert result.generation.content == "The file defines an add function."
    assert len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].name == "read_file"
    assert result.tool_outcomes[0].ok is True
    assert "def add(a, b)" in result.tool_outcomes[0].result


def test_multiple_sequential_tool_calls_in_single_turn(tmp_path):
    (tmp_path / "a.txt").write_text("File A content", encoding="utf-8")
    (tmp_path / "b.txt").write_text("File B content", encoding="utf-8")

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    model=payload.model,
                    content="Reading both files.",
                    tool_calls=[
                        ToolCall(id="call_a", name="read_file", arguments={"path": "a.txt"}),
                        ToolCall(id="call_b", name="read_file", arguments={"path": "b.txt"}),
                    ],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="Both files read successfully.",
                done=True,
            )

    provider = FakeProvider()
    result = run(
        AgentService(tmp_path, provider).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Read both files")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: True,
        )
    )

    assert len(result.tool_outcomes) == 2
    assert result.tool_outcomes[0].name == "read_file"
    assert "File A content" in result.tool_outcomes[0].result
    assert result.tool_outcomes[1].name == "read_file"
    assert "File B content" in result.tool_outcomes[1].result
    assert result.generation.content == "Both files read successfully."


def test_sequential_tool_calls_across_turns_and_tool_results_returned(tmp_path):
    (tmp_path / "auth.py").write_text("def authenticate(): return True\n", encoding="utf-8")

    captured_requests = []

    class FakeProvider:
        async def generate(self, payload):
            captured_requests.append(payload)
            turn = len(captured_requests)
            if turn == 1:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(id="c1", name="search_text", arguments={"query": "authenticate"})],
                    done=True,
                )
            if turn == 2:
                # Verify that tool result from turn 1 is present in messages
                tool_msg = next((m for m in payload.messages if m.role == "tool"), None)
                assert tool_msg is not None
                assert tool_msg.tool_call_id == "c1"
                assert "auth.py:1:" in tool_msg.content
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(id="c2", name="read_file", arguments={"path": "auth.py"})],
                    done=True,
                )
            # Turn 3
            tool_msgs = [m for m in payload.messages if m.role == "tool"]
            assert len(tool_msgs) == 2
            assert tool_msgs[1].tool_call_id == "c2"
            assert "def authenticate():" in tool_msgs[1].content
            return ProviderResponse(
                model=payload.model,
                content="Authentication returns True.",
                done=True,
            )

    result = run(
        AgentService(tmp_path, FakeProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Find authentication")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: True,
        )
    )

    assert result.generation.content == "Authentication returns True."
    assert len(result.tool_outcomes) == 2
    assert result.tool_outcomes[0].name == "search_text"
    assert result.tool_outcomes[1].name == "read_file"


def test_invalid_tool_names_rejected_gracefully(tmp_path):
    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(name="unsupported_dangerous_tool", arguments={"foo": "bar"})],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="Handled unknown tool error.",
                done=True,
            )

    result = run(
        AgentService(tmp_path, FakeProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Do bad things")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: True,
        )
    )

    assert len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].ok is False
    assert "Unknown tool: 'unsupported_dangerous_tool'" in result.tool_outcomes[0].result
    assert result.generation.content == "Handled unknown tool error."


def test_invalid_arguments_extra_fields_and_wrong_types_rejected(tmp_path):
    (tmp_path / "main.py").write_text("code", encoding="utf-8")

    class FakeProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                # Extra field should be rejected by strict schema
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="read_file",
                            arguments={"path": "main.py", "extra_injected": "surprise"},
                        )
                    ],
                    done=True,
                )
            if self.calls == 2:
                # Wrong type for path
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(name="read_file", arguments={"path": 12345})],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="Recovered from invalid arguments.",
                done=True,
            )

    result = run(
        AgentService(tmp_path, FakeProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Test bad args")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: True,
        )
    )

    assert len(result.tool_outcomes) == 2
    assert result.tool_outcomes[0].ok is False
    assert "Invalid arguments for tool 'read_file'" in result.tool_outcomes[0].result
    assert "extra_forbidden" in result.tool_outcomes[0].result or "Extra inputs are not permitted" in result.tool_outcomes[0].result
    assert result.tool_outcomes[1].ok is False
    assert "Invalid arguments for tool 'read_file'" in result.tool_outcomes[1].result


def test_provider_failures_propagate(tmp_path):
    class FailingProvider:
        async def generate(self, payload):
            raise ProviderTimeoutError

    with pytest.raises(ProviderTimeoutError):
        run(
            AgentService(tmp_path, FailingProvider()).run_autonomous(
                AgentTaskRequest(
                    model="test-model",
                    messages=[ChatMessage(role="user", content="Hello")],
                ),
                approve_write=lambda p: True,
                approve_command=lambda p: True,
            )
        )


def test_maximum_iteration_limits(tmp_path):
    (tmp_path / "loop.txt").write_text("data", encoding="utf-8")

    class EndlessProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls <= 4:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(name="read_file", arguments={"path": "loop.txt"})],
                    done=True,
                )
            # Final summary call
            assert "tool step limit was reached" in payload.messages[-1].content
            return ProviderResponse(
                model=payload.model,
                content="Summary after hitting step limit.",
                done=True,
            )

    provider = EndlessProvider()
    result = run(
        AgentService(tmp_path, provider).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Loop forever")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: True,
            max_steps=4,
        )
    )

    assert len(result.tool_outcomes) == 4
    assert result.generation.content == "Summary after hitting step limit."


def test_cancellation_propagates(tmp_path):
    class HangingProvider:
        async def generate(self, payload):
            await asyncio.sleep(100)
            return ProviderResponse(model=payload.model, content="never", done=True)

    async def run_with_cancel():
        task = asyncio.create_task(
            AgentService(tmp_path, HangingProvider()).run_autonomous(
                AgentTaskRequest(
                    model="test-model",
                    messages=[ChatMessage(role="user", content="Hang")],
                ),
                approve_write=lambda p: True,
                approve_command=lambda p: True,
            )
        )
        await asyncio.sleep(0.01)
        task.cancel()
        await task

    with pytest.raises(asyncio.CancelledError):
        run(run_with_cancel())


def test_plan_mode_restrictions(tmp_path):
    target = tmp_path / "plan_test.txt"
    approval_called = False

    def approve_write(proposal):
        nonlocal approval_called
        approval_called = True
        return True

    class MutatingProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="write_file",
                            arguments={"path": "plan_test.txt", "content": "mutated!"},
                        )
                    ],
                    done=True,
                )
            if self.calls == 2:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[
                        ToolCall(
                            name="run_command",
                            arguments={"argv": ["python", "--version"]},
                        )
                    ],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="Plan finished without mutations.",
                done=True,
            )

    result = run(
        AgentService(tmp_path, MutatingProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Plan changes")],
            ),
            approve_write=approve_write,
            approve_command=lambda p: True,
            policy=ExecutionPolicy.PLAN,
        )
    )

    assert result.read_only is True
    assert not target.exists()
    assert not approval_called
    assert len(result.tool_outcomes) == 2
    assert result.tool_outcomes[0].ok is False
    assert "not permitted in plan mode" in result.tool_outcomes[0].result
    assert result.tool_outcomes[1].ok is False
    assert "not permitted in plan mode" in result.tool_outcomes[1].result
    assert result.generation.content == "Plan finished without mutations."


def test_command_approval_denied_in_normal_mode(tmp_path):
    class CommandProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[ToolCall(name="run_command", arguments={"argv": ["python", "--version"]})],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="Command was not approved.",
                done=True,
            )

    result = run(
        AgentService(tmp_path, CommandProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Run a command")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: False,
        )
    )

    assert len(result.tool_outcomes) == 1
    assert result.tool_outcomes[0].ok is False
    assert "not approved" in result.tool_outcomes[0].result


def test_workspace_traversal_and_sensitive_file_protections(tmp_path):
    (tmp_path / ".env").write_text("SECRET=123", encoding="utf-8")

    class TraversalProvider:
        def __init__(self):
            self.calls = 0

        async def generate(self, payload):
            self.calls += 1
            if self.calls == 1:
                return ProviderResponse(
                    model=payload.model,
                    content="",
                    tool_calls=[
                        ToolCall(name="read_file", arguments={"path": "../secret.txt"}),
                        ToolCall(name="read_file", arguments={"path": ".env"}),
                    ],
                    done=True,
                )
            return ProviderResponse(
                model=payload.model,
                content="Protected sensitive files.",
                done=True,
            )

    result = run(
        AgentService(tmp_path, TraversalProvider()).run_autonomous(
            AgentTaskRequest(
                model="test-model",
                messages=[ChatMessage(role="user", content="Steal files")],
            ),
            approve_write=lambda p: True,
            approve_command=lambda p: True,
        )
    )

    assert len(result.tool_outcomes) == 2
    assert result.tool_outcomes[0].ok is False
    assert "relative path" in result.tool_outcomes[0].result or "Workspace access" in result.tool_outcomes[0].result
    assert result.tool_outcomes[1].ok is False
    assert "Cannot access excluded" in result.tool_outcomes[1].result or "path" in result.tool_outcomes[1].result.lower()
