import asyncio

from forge.agent import AgentService, AgentTaskRequest
from forge.providers.models import ChatMessage, GenerationResult


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
