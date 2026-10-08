"""Tests for the in-server LLM chat endpoint (mocked provider clients)."""

import json
import os
import tempfile

import pytest
import yaml
from nornir import InitNornir
from starlette.testclient import TestClient

from nornir_srl.server.agent import ChatService, dumps_truncated
from nornir_srl.server.app import create_app
from nornir_srl.server.jsonrpc import JsonRpcUnreachable
from nornir_srl.server.llm import KEY_ENVS
from nornir_srl.server.store import FabricStore

from .fakes import FakeDevice
from .test_server_app import HOSTS, _responses


@pytest.fixture(autouse=True)
def no_provider_keys(monkeypatch):
    """Start every test from an environment with no provider configured."""
    for env in KEY_ENVS + ("FCLI_LLM_PROVIDER", "FCLI_CHAT_MAX_ROUNDS"):
        monkeypatch.delenv(env, raising=False)


@pytest.fixture
def fabric(monkeypatch):
    with tempfile.TemporaryDirectory() as tmp:
        host_file = os.path.join(tmp, "hosts.yml")
        group_file = os.path.join(tmp, "groups.yml")
        with open(host_file, "w") as handle:
            yaml.safe_dump(HOSTS, handle)
        with open(group_file, "w") as handle:
            yaml.safe_dump({}, handle)
        nornir = InitNornir(
            inventory={
                "plugin": "SimpleInventory",
                "options": {"host_file": host_file, "group_file": group_file},
            },
            runner={"plugin": "serial"},
            logging={"enabled": False},
        )
        devices = {name: FakeDevice(_responses(name)) for name in HOSTS}
        monkeypatch.setattr(
            "nornir.core.inventory.Host.get_connection",
            lambda self, name, config: devices[self.name],
        )
        yield nornir, devices


@pytest.fixture
def store(fabric):
    nornir, devices = fabric
    fabric_store = FabricStore(nornir, resync_interval=0, restart_debounce=0.02)
    fabric_store.start()
    yield fabric_store, devices
    fabric_store.stop()


class FakeFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, ident, name, arguments, index=0):
        self.id = ident
        self.index = index
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls or []


class FakeChoice:
    def __init__(self, message=None, delta=None):
        self.message = message
        self.delta = delta


class FakeCompletion:
    """A full Chat Completions response (non-stream)."""

    def __init__(self, content=None, tool_calls=None):
        self.choices = [
            FakeChoice(message=FakeMessage(content=content, tool_calls=tool_calls))
        ]


class FakeCompletions:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.rounds:
            raise AssertionError("unexpected extra completion call")
        return self.rounds.pop(0)


class FakeChat:
    def __init__(self, rounds):
        self.completions = FakeCompletions(rounds)


class FakeChatClient:
    """A Chat Completions client, as Grok is driven."""

    def __init__(self, rounds):
        self.chat = FakeChat(rounds)


class FakeResponses:
    def __init__(self, rounds, status="completed", incomplete_reason=""):
        self.rounds = list(rounds)
        self.calls = []
        self.status = status
        self.incomplete_reason = incomplete_reason

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.rounds:
            raise AssertionError("unexpected extra responses call")
        response = {"output": self.rounds.pop(0), "status": self.status}
        if self.incomplete_reason:
            response["incomplete_details"] = {"reason": self.incomplete_reason}
        return response


class FakeResponsesClient:
    """A Responses client, as OpenAI is driven: a turn is a list of items."""

    def __init__(self, rounds, status="completed", incomplete_reason=""):
        self.responses = FakeResponses(rounds, status, incomplete_reason)


def _message(text):
    return {"type": "message", "content": [{"type": "output_text", "text": text}]}


def _function_call(call_id, name, arguments="{}"):
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


class FakeAnthropicMessages:
    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if not self.rounds:
            raise AssertionError("unexpected extra messages call")
        return {"content": self.rounds.pop(0)}


class FakeAnthropic:
    """A Messages API client: tool calls are content blocks, not a field."""

    def __init__(self, rounds):
        self.messages = FakeAnthropicMessages(rounds)


def _parse_sse(body: bytes):
    events = []
    for block in body.decode().split("\n\n"):
        block = block.strip()
        if not block or block.startswith(":"):
            continue
        kind = "message"
        data = "{}"
        for line in block.split("\n"):
            if line.startswith("event: "):
                kind = line[7:].strip()
            elif line.startswith("data: "):
                data = line[6:]
        events.append((kind, json.loads(data)))
    return events


def test_reports_advertise_chat_disabled_without_a_key(fabric):
    nornir, _devices = fabric
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02)
    with TestClient(app) as test_client:
        payload = test_client.get("/api/reports").json()
        assert payload["chat"]["enabled"] is False
        assert payload["chat"]["providers"] == []


def test_reports_list_the_providers_that_have_keys(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02)
    with TestClient(app) as test_client:
        chat = test_client.get("/api/reports").json()["chat"]
    assert chat["enabled"] is True
    assert [p["id"] for p in chat["providers"]] == ["openai", "claude"]
    assert [p["default"] for p in chat["providers"]] == [True, False]
    assert chat["providers"][0]["model"] == "gpt-5.6-sol"
    assert chat["providers"][1]["model"] == "claude-sonnet-5"
    assert "xhigh" in chat["providers"][0]["efforts"]
    # Nothing configured, so the model's own default effort applies.
    assert chat["providers"][0]["effort"] == ""


def test_chat_is_503_without_a_key(fabric):
    nornir, _devices = fabric
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/chat", json={"messages": [{"role": "user", "content": "hi"}]}
        )
        assert response.status_code == 503


def test_chat_rejects_a_bad_body(fabric):
    nornir, _devices = fabric
    fake = FakeResponsesClient([[_message("unused")]])
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        assert test_client.post("/api/chat", json=[]).status_code == 400
        assert (
            test_client.post("/api/chat", json={"messages": "nope"}).status_code == 400
        )


def test_chat_tool_round_then_answer(fabric):
    nornir, _devices = fabric
    fake = FakeResponsesClient(
        [
            [
                {"type": "reasoning", "encrypted_content": "opaque"},
                _function_call("call_1", "lldp_neighbors"),
            ],
            [_message("leaf1 is cabled to spine1.")],
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        assert test_client.get("/api/reports").json()["chat"]["enabled"] is True
        response = test_client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "who is leaf1 next to?"}],
                "context": {"report": "lldp"},
            },
        )
        assert response.status_code == 200
        events = _parse_sse(response.content)
    kinds = [kind for kind, _ in events]
    assert "tool" in kinds
    assert "token" in kinds
    assert kinds[-1] == "done"
    tool = next(payload for kind, payload in events if kind == "tool")
    assert tool["name"] == "lldp_neighbors"
    assert tool["id"] == "call_1"
    # The drawer needs to know when the tool finished, not just that it started.
    result = next(payload for kind, payload in events if kind == "tool_result")
    assert result["id"] == "call_1"
    assert result["error"] is None
    assert result["bytes"] > 0
    assert result["ms"] >= 0
    assert kinds.index("tool") < kinds.index("tool_result")
    token = next(payload for kind, payload in events if kind == "token")
    assert "leaf1" in token["text"]
    first, second = fake.responses.calls
    assert first["instructions"].startswith("You are a read-only SR Linux")
    assert first["store"] is False
    assert "reasoning" not in first
    # The reasoning item is replayed, and the result points at the call it answers.
    assert second["input"][1]["type"] == "reasoning"
    assert second["input"][-1]["type"] == "function_call_output"
    assert second["input"][-1]["call_id"] == "call_1"


def test_a_failing_tool_is_reported_as_such(fabric):
    nornir, _devices = fabric
    fake = FakeResponsesClient(
        [
            [
                _function_call(
                    "call_1",
                    "node_cli",
                    '{"node": "leaf1", "command": "set interface ethernet-1/1"}',
                )
            ],
            [_message("I cannot change configuration.")],
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={"messages": [{"role": "user", "content": "shut leaf1 down"}]},
            ).content
        )
    result = next(payload for kind, payload in events if kind == "tool_result")
    assert "only show, info, and tree are allowed" in result["error"]
    # The turn carries on: the model is told why and answers anyway.
    assert events[-1][0] == "done"


def test_chat_runs_a_tool_round_on_grok(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("XAI_API_KEY", "x")
    fake = FakeChatClient(
        [
            FakeCompletion(
                tool_calls=[FakeToolCall("call_1", "lldp_neighbors", "{}")]
            ),
            FakeCompletion(content="leaf1 is cabled to spine1."),
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={
                    "messages": [{"role": "user", "content": "who is leaf1 next to?"}],
                    "provider": "grok",
                    "effort": "low",
                },
            ).content
        )
    start = next(payload for kind, payload in events if kind == "start")
    assert start == {"provider": "grok", "model": "grok-4.6", "effort": "low"}
    tool = next(payload for kind, payload in events if kind == "tool")
    assert tool["name"] == "lldp_neighbors"
    assert fake.chat.completions.calls[0]["reasoning_effort"] == "low"


def test_chat_effort_reaches_the_provider(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    fake = FakeResponsesClient([[_message("all good")]])
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={
                    "messages": [{"role": "user", "content": "status?"}],
                    "effort": "xhigh",
                },
            ).content
        )
    start = next(payload for kind, payload in events if kind == "start")
    assert start["effort"] == "xhigh"
    assert fake.responses.calls[0]["reasoning"] == {"effort": "xhigh"}


def test_chat_rejects_an_effort_the_provider_does_not_take(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("XAI_API_KEY", "x")
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02)
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "provider": "grok",
                    "effort": "max",
                },
            ).content
        )
    assert events[0][0] == "error"
    assert "low, medium, high, xhigh" in events[0][1]["error"]


def test_chat_runs_a_tool_round_on_claude(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("ANTHROPIC_API_KEY", "a")
    fake = FakeAnthropic(
        [
            [
                {
                    "type": "tool_use",
                    "id": "toolu_1",
                    "name": "lldp_neighbors",
                    "input": {},
                }
            ],
            [{"type": "text", "text": "leaf1 is cabled to spine1."}],
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "who is leaf1 next to?"}],
                "provider": "claude",
            },
        )
        assert response.status_code == 200
        events = _parse_sse(response.content)
    start = next(payload for kind, payload in events if kind == "start")
    assert start == {"provider": "claude", "model": "claude-sonnet-5", "effort": ""}
    tool = next(payload for kind, payload in events if kind == "tool")
    assert tool["name"] == "lldp_neighbors"
    token = next(payload for kind, payload in events if kind == "token")
    assert "leaf1" in token["text"]
    # The result of the tool_use went back as a tool_result block.
    second_round = fake.messages.calls[1]["messages"]
    assert second_round[-1]["content"][0]["tool_use_id"] == "toolu_1"
    assert "lldp" in second_round[-1]["content"][0]["content"]


def test_chat_refuses_a_provider_without_a_key(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02)
    with TestClient(app) as test_client:
        response = test_client.post(
            "/api/chat",
            json={
                "messages": [{"role": "user", "content": "hi"}],
                "provider": "grok",
            },
        )
        events = _parse_sse(response.content)
    assert events[0][0] == "error"
    assert "XAI_API_KEY" in events[0][1]["error"]


def test_chat_rejects_an_unknown_provider(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("OPENAI_API_KEY", "o")
    app = create_app(nornir, resync_interval=0, restart_debounce=0.02)
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={
                    "messages": [{"role": "user", "content": "hi"}],
                    "provider": "llama",
                },
            ).content
        )
    assert events[0][0] == "error"
    assert "unknown provider" in events[0][1]["error"]


def test_a_silent_round_is_asked_again_for_the_answer(fabric):
    nornir, _devices = fabric
    # Round 2 comes back with neither text nor a tool call, which is what a
    # reasoning model does when thinking consumed the whole output budget.
    fake = FakeResponsesClient(
        [
            [_function_call("call_1", "bgp_peers")],
            [],
            [_message("Underlay is eBGP: leaf1 in AS 65001 peers with spine1.")],
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={
                    "messages": [
                        {"role": "user", "content": "how is the underlay routed?"}
                    ]
                },
            ).content
        )
    kinds = [kind for kind, _ in events]
    assert "error" not in kinds
    token = next(payload for kind, payload in events if kind == "token")
    assert "eBGP" in token["text"]
    assert kinds[-1] == "done"
    # The retry drops the tools, so it has to answer in words.
    assert "tools" not in fake.responses.calls[-1]
    # An empty assistant turn is never replayed to the provider.
    replayed = json.dumps(fake.responses.calls[-1]["input"], default=str)
    assert replayed.count("function_call_output") == 1


def test_a_truncated_answer_is_reported_not_swallowed(fabric):
    nornir, _devices = fabric
    # Both the round and the retry come back empty and cut off.
    fake = FakeResponsesClient(
        [
            [_function_call("call_1", "bgp_peers")],
            [],
            [],
        ],
        status="incomplete",
        incomplete_reason="max_output_tokens",
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={"messages": [{"role": "user", "content": "explain the fabric"}]},
            ).content
        )
    notice = next(payload for kind, payload in events if kind == "notice")
    assert "cut off" in notice["text"]
    assert "max_output_tokens" in notice["text"]
    error = next(payload for kind, payload in events if kind == "error")
    assert "no answer" in error["error"]
    assert "max_output_tokens" in error["error"]
    # The user is told what to do about it, and the stream still closes cleanly.
    assert "reasoning effort" in error["error"]
    assert [kind for kind, _ in events][-1] == "done"


def test_running_out_of_rounds_still_answers(fabric, monkeypatch):
    nornir, _devices = fabric
    monkeypatch.setenv("FCLI_CHAT_MAX_ROUNDS", "2")
    # The model keeps asking for tools and never volunteers an answer.
    fake = FakeResponsesClient(
        [
            [_function_call("call_1", "lldp_neighbors")],
            [_function_call("call_2", "bgp_peers")],
            [_message("From what I gathered: leaf1 peers with spine1.")],
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={"messages": [{"role": "user", "content": "explain everything"}]},
            ).content
        )
    kinds = [kind for kind, _ in events]
    assert "error" not in kinds
    notice = next(payload for kind, payload in events if kind == "notice")
    assert "budget" in notice["text"]
    token = next(payload for kind, payload in events if kind == "token")
    assert "leaf1 peers with spine1" in token["text"]
    assert kinds[-1] == "done"
    # The answering round is asked without tools, so it cannot ask for more.
    assert "tools" not in fake.responses.calls[-1]


def test_an_identical_call_is_not_run_twice(fabric):
    nornir, _devices = fabric
    fake = FakeResponsesClient(
        [
            [_function_call("call_1", "lldp_neighbors")],
            [_function_call("call_2", "lldp_neighbors")],
            [_message("leaf1 is cabled to spine1.")],
        ]
    )
    app = create_app(
        nornir,
        resync_interval=0,
        restart_debounce=0.02,
        chat_client_factory=lambda: fake,
    )
    with TestClient(app) as test_client:
        events = _parse_sse(
            test_client.post(
                "/api/chat",
                json={"messages": [{"role": "user", "content": "who is next to leaf1?"}]},
            ).content
        )
    results = [payload for kind, payload in events if kind == "tool_result"]
    assert [r["repeat"] for r in results] == [False, True]
    assert results[1]["bytes"] < results[0]["bytes"]


def test_a_fabric_without_jsonrpc_is_only_probed_once(store):
    fabric_store, _devices = store
    attempts = []

    def jsonrpc(*args):
        attempts.append(args)
        raise JsonRpcUnreachable("connection refused")

    chat = ChatService(
        fabric_store, client_factory=lambda: None, jsonrpc_call=jsonrpc
    )
    first = json.loads(
        chat.execute_tool("node_cli", {"node": "leaf1", "command": "show version"})
    )
    assert "connection refused" in first["error"]
    # A second node must not cost another round to learn the same thing.
    second = json.loads(
        chat.execute_tool("node_cli", {"node": "spine1", "command": "show version"})
    )
    assert "not reachable on this fabric" in second["error"]
    assert "node_get" in second["error"]
    assert len(attempts) == 1


def test_a_rejected_command_does_not_disable_jsonrpc(store):
    fabric_store, _devices = store
    attempts = []

    def jsonrpc(*args):
        attempts.append(args)
        raise RuntimeError("unknown command")

    chat = ChatService(
        fabric_store, client_factory=lambda: None, jsonrpc_call=jsonrpc
    )
    for node in ("leaf1", "spine1"):
        payload = json.loads(
            chat.execute_tool("node_cli", {"node": node, "command": "show nope"})
        )
        assert payload["error"] == "unknown command"
    assert len(attempts) == 2


def test_chat_service_runs_a_live_report(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(chat.execute_tool("lldp_neighbors", {}))
    assert payload["report"] == "lldp"
    assert {row["Node"] for row in payload["rows"]} == set(HOSTS)


def test_chat_service_offers_the_lenses_as_tools(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    tools = {t["name"]: t for t in chat._tools}
    assert {"locate_address", "trace_path", "service_detail"} <= set(tools)
    assert tools["trace_path"]["parameters"]["required"] == ["source", "destination"]
    assert "inv_filter" in tools["locate_address"]["parameters"]["properties"]


def test_chat_service_runs_a_lens_and_hands_back_its_records(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("trace_path", {"source": "leaf1", "destination": "10.1.1.5"})
    )
    assert payload["lens"] == "path"
    assert [r["outcome"] for r in payload["records"]] == ["delivered", "no-neighbor"]
    assert payload["records"][0]["node"] == "leaf1"


def test_chat_service_reports_what_a_lens_cannot_answer(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    assert "needs address" in json.loads(chat.execute_tool("locate_address", {}))["error"]
    missing = json.loads(chat.execute_tool("service_detail", {"name": "nothing"}))
    assert "no network-instance matching" in missing["error"]


def test_chat_service_honours_field_filter(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("lldp_neighbors", {"field_filter": "Nbr-System=spine2"})
    )
    assert {row["Nbr-System"] for row in payload["rows"]} == {"spine2"}


def test_node_cli_allowlist_is_enforced(store):
    fabric_store, _devices = store
    called = []

    def jsonrpc(*args):
        called.append(args)
        return {"ok": True}

    chat = ChatService(
        fabric_store, client_factory=lambda: None, jsonrpc_call=jsonrpc
    )
    rejected = json.loads(
        chat.execute_tool("node_cli", {"node": "leaf1", "command": "set interface ethernet-1/1"})
    )
    assert "error" in rejected
    assert called == []
    ok = json.loads(
        chat.execute_tool(
            "node_cli", {"node": "leaf1", "command": "info from state system name"}
        )
    )
    assert ok["command"] == "info from state system name"
    assert called and called[0][0] == "leaf1"
    assert called[0][4] == "info from state system name"


def test_node_get_reads_via_the_existing_session(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool(
            "node_get",
            {"node": "leaf1", "path": "/system/lldp/interface[name=*]/neighbor"},
        )
    )
    assert payload["node"] == "leaf1"
    assert payload["result"]


def test_node_config_reads_an_area_from_the_config_datastore(store):
    fabric_store, _devices = store
    calls = []

    def spy(node, path, datatype="state"):
        calls.append((node, path, datatype))
        return [{"autonomous-system": 65001}]

    fabric_store.node_get = spy
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_config", {"node": "leaf1", "area": "bgp"})
    )
    assert payload["node"] == "leaf1"
    assert payload["network_instance"] == "default"
    # The area is asked for as config, not state, and resolves to a real path.
    assert calls == [
        ("leaf1", "/network-instance[name=default]/protocols/bgp", "config")
    ]


def test_node_config_groups_the_routing_protocols(store):
    fabric_store, _devices = store
    asked = []
    fabric_store.node_get = lambda node, path, datatype="state": asked.append(path)
    chat = ChatService(fabric_store, client_factory=lambda: None)
    chat.execute_tool(
        "node_config",
        {"node": "leaf1", "area": "routing", "network_instance": "ipvrf-1"},
    )
    assert asked == [
        "/network-instance[name=ipvrf-1]/protocols/bgp",
        "/network-instance[name=ipvrf-1]/protocols/isis",
        "/network-instance[name=ipvrf-1]/protocols/ospf",
        "/network-instance[name=ipvrf-1]/static-routes",
    ]


def test_node_config_says_when_a_protocol_is_not_configured(store):
    fabric_store, _devices = store
    # gNMI answers an unconfigured subtree with an empty container.
    fabric_store.node_get = lambda node, path, datatype="state": (
        [{}] if "isis" in path or "ospf" in path else [{"admin-state": "enable"}]
    )
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_config", {"node": "leaf1", "area": "routing"})
    )
    assert len(payload["config"]) == 2
    assert [p.rsplit("/", 1)[-1] for p in payload["not_configured"]] == [
        "isis",
        "ospf",
    ]


def test_node_config_drops_provenance_annotations(store):
    fabric_store, _devices = store
    fabric_store.node_get = lambda node, path, datatype="state": [
        {
            "bgp": {
                "autonomous-system": 101,
                "_annotate_autonomous-system": "EDA Source CRs: Fabric/dc1",
                "group": [{"name": "spines", "_annotate": "EDA"}],
            }
        }
    ]
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_config", {"node": "leaf1", "area": "bgp"})
    )
    assert "_annotate" not in json.dumps(payload["config"])
    assert "annotate" in payload["note"]
    bgp = payload["config"]["/network-instance[name=default]/protocols/bgp"][0]["bgp"]
    assert bgp["autonomous-system"] == 101
    assert bgp["group"] == [{"name": "spines"}]


def test_node_config_never_hands_the_model_a_secret(store):
    fabric_store, _devices = store
    fabric_store.node_get = lambda node, path, datatype="state": [
        {
            "srl_nokia-network-instance:network-instance/protocols/bgp": {
                "autonomous-system": 101,
                "group": [{"name": "spines", "authentication": {"keychain": "k1"}}],
                "neighbor": [{"peer-address": "10.0.0.1", "authentication-key": "$aes1$hush"}],
            }
        }
    ]
    chat = ChatService(fabric_store, client_factory=lambda: None)
    text = chat.execute_tool("node_config", {"node": "leaf1", "area": "bgp"})
    assert "hush" not in text
    payload = json.loads(text)
    bgp = payload["config"]["/network-instance[name=default]/protocols/bgp"][0]
    neighbor = next(iter(bgp.values()))["neighbor"][0]
    assert neighbor["authentication-key"].startswith("<redacted ")
    assert neighbor["peer-address"] == "10.0.0.1"
    assert "redacted" in payload["secrets"]


def test_node_get_and_node_cli_never_hand_the_model_a_secret(store):
    fabric_store, _devices = store
    fabric_store.node_get = lambda node, path, datatype="state": [{"/": "$y$j9T$hush"}]
    chat = ChatService(
        fabric_store,
        client_factory=lambda: None,
        jsonrpc_call=lambda *args: [{"user": [{"name": "admin", "password": "$y$hush"}]}],
    )
    leaf = chat.execute_tool(
        "node_get",
        {"node": "leaf1", "path": "/system/aaa/authentication/admin-user/password", "datatype": "config"},
    )
    assert "hush" not in leaf and "<redacted " in leaf
    cli = chat.execute_tool("node_cli", {"node": "leaf1", "command": "info system aaa"})
    assert "hush" not in cli and "<redacted " in cli


def test_node_config_keeps_what_it_could_read(store):
    fabric_store, _devices = store

    def flaky(node, path, datatype="state"):
        if "isis" in path:
            raise RuntimeError("no such path")
        return [{"path": path}]

    fabric_store.node_get = flaky
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_config", {"node": "leaf1", "area": "routing"})
    )
    # A protocol that is simply not configured must not lose the others.
    assert len(payload["config"]) == 3
    assert "isis" in "".join(payload["errors"])


def test_node_config_rejects_an_unknown_area(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_config", {"node": "leaf1", "area": "firewall"})
    )
    assert "unknown config area" in payload["error"]
    assert "routing_policy" in payload["error"]


def test_node_get_rejects_cli_origin(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_get", {"node": "leaf1", "path": "cli:/show version"})
    )
    assert "error" in payload


def test_dumps_truncated_caps_huge_payloads():
    text = dumps_truncated({"x": "a" * 80_000})
    assert "truncated" in text
    assert len(text) < 80_000


def test_unknown_node_is_an_error(store):
    fabric_store, _devices = store
    chat = ChatService(fabric_store, client_factory=lambda: None)
    payload = json.loads(
        chat.execute_tool("node_cli", {"node": "nope", "command": "show version"})
    )
    assert "unknown node" in payload["error"]
