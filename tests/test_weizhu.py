"""Offline safety regression tests for the locally reviewed WeiZhu package."""

import asyncio
import base64
import importlib
import json
import logging
import socket
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest


PACKAGE = Path(__file__).resolve().parents[1]
GROUP_A = "aiocqhttp:GroupMessage:10001"
GROUP_B = "aiocqhttp:GroupMessage:10002"
DIRECT_1 = "aiocqhttp:FriendMessage:101"
NAMED_QQ_GROUP = "qq-production:GroupMessage:10001"
NAMED_QQ_DIRECT = "qq-production:FriendMessage:101"
SYNTHETIC_QR_URL = "https://example.invalid/synthetic-qr-session"
SYNTHETIC_SCAN_ID = "fake-scan-session-id"
SYNTHETIC_UID = "private-fixture-uid"
BOT_QQ = "9199000101"


def _module(monkeypatch, name, **attrs):
    obj = ModuleType(name)
    obj.__dict__.update(attrs)
    monkeypatch.setitem(sys.modules, name, obj)
    return obj


def _register(*args, **kwargs):
    return lambda obj: obj


class CustomFilter:
    def __init__(self, raise_error=True):
        self.raise_error = raise_error


class RegisteredGroup:
    """Small model of v4.29.4's independently dispatched subcommand filters."""

    def __init__(self, name):
        self.name = name
        self._test_filters = []
        self.commands = {}

    def command(self, name, **options):
        def record(handler):
            commands = [name, *options.get("alias", ())]
            for command in commands:
                self.commands[command] = handler
            if not hasattr(handler, "_test_filters"):
                handler._test_filters = []
            return handler
        return record

    def custom_filter(self, filter_type, raise_error=True):
        def register(handler):
            # In the real SDK, the subcommand must already be registered with
            # this group before this decorator can find its CommandFilter.
            if handler in self.commands.values():
                handler._test_filters.append(filter_type(raise_error))
            return handler
        return register


def _group(name, **kwargs):
    return lambda _method: RegisteredGroup(name)


def _custom_filter(filter_type, raise_error=True, **options):
    def attach(handler):
        if not hasattr(handler, "_test_filters"):
            handler._test_filters = []
        if not hasattr(handler, "_test_custom_registrations"):
            handler._test_custom_registrations = []
        handler._test_filters.append(filter_type(raise_error))
        handler._test_custom_registrations.append((filter_type, options))
        return handler
    return attach


@dataclass
class At:
    qq: int | str


@dataclass
class AtAll(At):
    qq: str = "all"


@dataclass
class Plain:
    text: str


@dataclass
class Reply:
    id: str
    sender_id: int | str
    chain: list = field(default_factory=list)

    @property
    def qq(self):
        # The OneBot adapter also supplies a compatibility qq field for replies.
        return self.sender_id


class Event:
    def __init__(
        self, sender="101", umo=DIRECT_1, role="member", platform_name="aiocqhttp",
        *, addressed=False, self_id=BOT_QQ, components=None, message_str=None, wake=True,
    ):
        self.sender = sender
        self.unified_msg_origin = umo
        self.message_obj = SimpleNamespace(
            sender=SimpleNamespace(role=role),
            self_id=self_id,
            message=list(components or ()) + ([At(qq=int(BOT_QQ))] if addressed else []),
        )
        self.platform_name = platform_name
        self.message_str = message_str if message_str is not None else "".join(
            component.text for component in self.message_obj.message if isinstance(component, Plain)
        )
        # The framework permits "/" and a replied-to bot as wake conditions;
        # those flags do not demonstrate an actual mention.
        self.is_wake = wake
        self.is_at_or_wake_command = wake
        self.call_llm = False
        self.llm_flag_changes = []
        self.stopped = False

    def get_sender_id(self):
        return self.sender

    def get_group_id(self):
        return "" if self.is_private_chat() else self.unified_msg_origin.rsplit(":", 1)[1]

    def get_platform_id(self):
        return self.unified_msg_origin.split(":", 1)[0]

    def get_platform_name(self):
        return self.platform_name

    def get_self_id(self):
        return self.message_obj.self_id

    def get_messages(self):
        return self.message_obj.message

    def get_message_str(self):
        return self.message_str

    def is_private_chat(self):
        return ":FriendMessage:" in self.unified_msg_origin

    def is_wake_up(self):
        return self.is_wake

    def should_call_llm(self, value):
        # AstrBot v4.29.4's setter directly updates this field; ProcessStage
        # runs the default provider only while the field is false.
        self.call_llm = value
        self.llm_flag_changes.append(value)

    def stop_event(self):
        self.stopped = True

    def is_stopped(self):
        return self.stopped

    def plain_result(self, msg):
        return ("text", msg)

    def image_result(self, path):
        return ("image", path)


class MessageChain:
    def __init__(self):
        self.components = []

    def message(self, text):
        self.components.append(("text", text))
        return self

    def file_image(self, path):
        self.components.append(("image", path))
        return self


class Star:
    def __init__(self, context):
        self.context = context


class SklandError(Exception):
    pass


class SklandAuthError(SklandError):
    pass


def no_network(*args, **kwargs):
    pytest.fail("Early guard or mock missing: network use is not allowed")


@pytest.fixture
def plugin_factory(monkeypatch, tmp_path):
    monkeypatch.setattr(socket, "create_connection", no_network)
    monkeypatch.setattr(socket, "getaddrinfo", no_network)
    monkeypatch.setattr(sys, "path", list(sys.path))
    _module(monkeypatch, "astrbot", __path__=[])
    _module(monkeypatch, "astrbot.api", __path__=[], AstrBotConfig=dict, logger=logging.getLogger("weizhu-tests"))
    _module(monkeypatch, "astrbot.api.message_components", At=At, AtAll=AtAll, Plain=Plain, Reply=Reply)
    _module(
        monkeypatch,
        "astrbot.api.event",
        AstrMessageEvent=Event,
        MessageChain=MessageChain,
        filter=SimpleNamespace(
            command_group=_group, custom_filter=_custom_filter, CustomFilter=CustomFilter,
        ),
    )
    _module(
        monkeypatch,
        "astrbot.api.star",
        Context=object,
        Star=Star,
        StarTools=SimpleNamespace(get_data_dir=lambda _name: tmp_path / "plugin-data"),
        register=_register,
    )
    alias = "_weizhu_under_test"
    _module(monkeypatch, alias, __path__=[str(PACKAGE)])
    _module(monkeypatch, "qrcode", QRCode=Mock(side_effect=no_network))
    _module(monkeypatch, alias + ".renderer", render_trend=Mock(), WeizhuRenderer=lambda *a, **kw: SimpleNamespace(
        render_match_card=AsyncMock(), render_multi_match_card=AsyncMock(),
        render_stats_card=AsyncMock(), render_list_card=AsyncMock(),
    ))
    _module(
        monkeypatch,
        alias + ".skland",
        SklandClient=lambda **kw: SimpleNamespace(
            close=AsyncMock(), get_arknights_binding=AsyncMock(side_effect=no_network),
            get_garrison_resource=AsyncMock(side_effect=no_network),
            get_garrison_history=AsyncMock(side_effect=no_network),
            get_player_info=AsyncMock(side_effect=no_network),
        ),
        SklandError=SklandError,
        SklandAuthError=SklandAuthError,
        qr_start=AsyncMock(side_effect=no_network),
        qr_poll=AsyncMock(side_effect=no_network),
        qr_finish=AsyncMock(side_effect=no_network),
    )
    monkeypatch.delitem(sys.modules, alias + ".main", raising=False)
    main = importlib.import_module(alias + ".main")
    # Skip only the upstream throttle delays; leave task scheduling and cancellation real.
    class OfflineAsyncio:
        sleep = AsyncMock()

        def __getattr__(self, name):
            return getattr(asyncio, name)

    monkeypatch.setattr(main, "asyncio", OfflineAsyncio())

    def make(config=None):
        return main.WeizhuPlugin(SimpleNamespace(send_message=AsyncMock()), config or {})

    try:
        yield main, make
    finally:
        sys.modules.pop(alias + ".main", None)


def run(awaitable):
    # Windows opens a private socket pair when the loop is constructed.
    with asyncio.Runner() as runner, pytest.MonkeyPatch.context() as guard:
        guard.setattr(socket.socket, "connect", no_network)
        guard.setattr(socket.socket, "connect_ex", no_network)
        return runner.run(awaitable)


async def drain(generator):
    return [response async for response in generator]


def binding(uid, umo, **overrides):
    return {
        "uid": uid,
        "nickname": f"synthetic-player-{uid}",
        "cred": f"fake-cred-for-{uid}",
        "channel": "test",
        "umo": umo,
        "season_id": "dummy-season",
        "last_ts": 100,
    } | overrides


@pytest.mark.parametrize(
    "event,allowed",
    [
        pytest.param(Event(umo=GROUP_A, components=[Plain("/卫戍 查询")]), False, id="bare-slash"),
        pytest.param(Event(umo=GROUP_A, components=[Plain(f"@{BOT_QQ} /卫戍 查询")]), False, id="plain-at-text"),
        pytest.param(Event(umo=GROUP_A, components=[At(qq="8000000100")]), False, id="mentions-other-user"),
        pytest.param(Event(umo=GROUP_A, components=[AtAll()]), False, id="mentions-everyone"),
        pytest.param(Event(umo=GROUP_A, components=[At(qq="all")]), False, id="all-in-at-component"),
        pytest.param(Event(umo=GROUP_A, components=[Reply(id="42", sender_id=BOT_QQ, chain=[At(qq=BOT_QQ)])]), False, id="only-replies-to-bot"),
        pytest.param(Event(umo=GROUP_A, self_id="", components=[At(qq=BOT_QQ)]), False, id="missing-bot-id"),
        pytest.param(Event(umo=GROUP_A, components=[At(qq=BOT_QQ)]), True, id="actual-at-by-string-id"),
        pytest.param(Event(umo=GROUP_A, addressed=True), True, id="actual-at-by-numeric-id"),
        pytest.param(Event(umo=GROUP_A, addressed=True, components=[AtAll(), Reply(id="42", sender_id=BOT_QQ)]), True, id="actual-at-with-other-segments"),
        pytest.param(Event(components=[Plain("/卫戍 查询")]), True, id="private-slash-unchanged"),
    ],
)
def test_all_command_routes_and_aliases_apply_actual_mention_filter(plugin_factory, event, allowed):
    main, _ = plugin_factory
    group = main.WeizhuPlugin.weizhu
    assert group.name == "卫戍"
    assert set(group.commands) >= {
        "帮助", "绑定", "解绑", "状态", "推送", "查询", "战绩", "对局", "查", "查统计", "用户",
        "help", "bind", "unbind", "info", "push", "统计", "stats", "history", "match",
        "detail", "查战绩", "看战绩", "看统计", "统计别人", "绑定用户", "用户列表",
    }
    routes = {"/卫戍": group, **group.commands}
    for label, route in routes.items():
        gates = [gate for gate in route._test_filters if isinstance(gate, main.RequireBotMention)]
        assert len(gates) == 1, f"{label} lacks its own bot mention filter"
        assert gates[0].raise_error is False, f"{label} should silently ignore unrelated traffic"
        assert gates[0].filter(event, {}) is allowed, f"{label} misclassified the message"


def guarded_hook(main, plugin, event):
    """Model the outer adapter filter dispatch before the default provider stage."""
    hook = plugin.suppress_weizhu_llm
    assert hook._test_custom_registrations == [(main.WeizhuCommandInput, {"priority": 1000})]
    gates = hook._test_filters
    assert len(gates) == 1 and isinstance(gates[0], main.WeizhuCommandInput)
    assert gates[0].raise_error is False
    if gates[0].filter(event, {}):
        assert run(hook(event)) is None


@pytest.mark.parametrize(
    "message,wake,expect_suppression",
    [
        pytest.param("/卫戍", True, True, id="woken-with-slash-root"),
        pytest.param("/卫戍 查询", True, True, id="woken-with-slash-subcommand"),
        pytest.param("  /卫戍\t推送 开 ", True, True, id="woken-with-slash-arguments"),
        pytest.param("卫戍", True, True, id="woken-root"),
        pytest.param("卫戍 help", True, True, id="woken-english-alias"),
        pytest.param("卫戍 随手打错的命令", True, True, id="woken-unknown-subcommand"),
        pytest.param("卫戍 查询", False, False, id="plain-unwoken-text"),
        pytest.param("/卫戍 查询", False, False, id="slash-command-does-not-wake-event"),
        pytest.param("/", True, False, id="slash-alone"),
        pytest.param("/help", True, False, id="other-slash-command"),
        pytest.param("/卫戍协议", True, False, id="not-whole-command-word"),
        pytest.param("卫戍协议是什么", True, False, id="normal-topic-discussion"),
        pytest.param("请告诉我怎么用 /卫戍 查询", True, False, id="slash-command-mentioned-in-sentence"),
    ],
)
def test_default_llm_suppression_reserves_only_weizhu_namespace(
    plugin_factory, message, wake, expect_suppression,
):
    main, make = plugin_factory
    plugin = make()
    event = Event(umo=GROUP_A, message_str=message, wake=wake)
    guarded_hook(main, plugin, event)
    assert event.call_llm is expect_suppression
    assert event.llm_flag_changes == ([True] if expect_suppression else [])
    assert not event.is_stopped()
    plugin.context.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "umo,addressed,command_should_run",
    [
        pytest.param(GROUP_A, False, False, id="group-no-at-has-no-command-and-no-llm"),
        pytest.param(GROUP_A, True, True, id="group-at-keeps-command-but-no-llm"),
        pytest.param(DIRECT_1, False, True, id="private-keeps-command-but-no-llm"),
    ],
)
def test_default_agent_is_skipped_without_blocking_legitimate_command_dispatch(
    plugin_factory, umo, addressed, command_should_run,
):
    main, make = plugin_factory
    plugin = make()
    event = Event(umo=umo, addressed=addressed, message_str="卫戍 帮助")
    guarded_hook(main, plugin, event)
    help_handler = plugin.cmd_help
    passed = all(gate.filter(event, {}) for gate in help_handler._test_filters)
    assert passed is command_should_run
    if passed:
        replies = run(drain(help_handler(event)))
        assert any(kind == "text" and "卫戍协议助手" in text for kind, text in replies)
    else:
        plugin.context.send_message.assert_not_awaited()
    assert not event.is_stopped()
    # Mirrors v4.29.4's ordinary LLM fallback gate when nothing was sent.
    default_agent_would_run = event.is_at_or_wake_command and not event.call_llm
    assert not default_agent_would_run


def invite_fixture(main, plugin):
    main.qr_start.side_effect = None
    main.qr_start.return_value = {"scan_id": SYNTHETIC_SCAN_ID, "url": SYNTHETIC_QR_URL}
    path = plugin.qr_dir / "synthetic-private-qr.png"

    def render(url):
        assert url == SYNTHETIC_QR_URL
        path.write_bytes(b"synthetic-only; test fixture, not a login QR")
        return path

    plugin._render_qr = Mock(side_effect=render)
    return path


def assert_group_reply_only(responses):
    assert responses and all(kind == "text" for kind, _ in responses)
    assert any("私聊" in text for _, text in responses)
    assert not any(
        secret in text
        for _, text in responses
        for secret in (SYNTHETIC_QR_URL, SYNTHETIC_SCAN_ID, SYNTHETIC_UID, "synthetic-private-qr.png")
    )


def test_direct_binding_keeps_qr_in_command_response(plugin_factory):
    main, make = plugin_factory
    plugin = make()
    qr_path = invite_fixture(main, plugin)

    async def scenario():
        pending_scan = asyncio.Event()

        async def poll(scan_id, _proxy):
            assert scan_id == SYNTHETIC_SCAN_ID
            await pending_scan.wait()

        main.qr_poll.side_effect = poll
        replies = await drain(plugin.cmd_bind(Event()))
        assert ("image", str(qr_path)) in replies
        assert qr_path.is_file()
        plugin.context.send_message.assert_not_awaited()
        await plugin.terminate()

    run(scenario())
    main.qr_start.assert_awaited_once()
    assert not qr_path.exists()


def test_private_binding_sends_completion_only_to_originating_private_chat(plugin_factory):
    main, make = plugin_factory
    plugin = make()
    qr_path = invite_fixture(main, plugin)
    main.qr_finish.side_effect = None
    main.qr_finish.return_value = {"cred": "synthetic-cred"}
    plugin.client.get_arknights_binding = AsyncMock(return_value={
        "uid": SYNTHETIC_UID, "nickname": "synthetic-player", "channel": "test",
    })
    plugin.client.get_garrison_resource = AsyncMock(return_value={
        "versionInfo": {"seasonId": "dummy-season"},
    })
    plugin.client.get_garrison_history = AsyncMock(return_value=[])
    plugin.client.get_player_info = AsyncMock(return_value={})

    async def scenario():
        allow_synthetic_scan = asyncio.Event()
        completion = asyncio.Event()
        messages = []

        async def poll(scan_id, _proxy):
            assert scan_id == SYNTHETIC_SCAN_ID
            await allow_synthetic_scan.wait()
            return "synthetic-scan-code"

        async def private_send(umo, chain):
            assert umo == NAMED_QQ_DIRECT
            messages.append(chain.components)
            assert all(kind == "text" for kind, _ in chain.components)
            assert any(SYNTHETIC_UID in text for _, text in chain.components)
            completion.set()
            return True

        main.qr_poll.side_effect = poll
        plugin.context.send_message.side_effect = private_send
        replies = await drain(plugin.cmd_bind(Event(umo=NAMED_QQ_DIRECT)))
        assert ("image", str(qr_path)) in replies
        assert qr_path.is_file()
        assert not messages
        assert plugin.bindings == {}
        allow_synthetic_scan.set()
        await asyncio.wait_for(completion.wait(), timeout=2)
        assert len(messages) == 1

    run(scenario())
    main.qr_start.assert_awaited_once()
    main.qr_finish.assert_awaited_once()
    assert not qr_path.exists()
    saved = json.loads(plugin.bindings_file.read_text(encoding="utf-8"))["101"]
    assert saved["bind_umo"] == NAMED_QQ_DIRECT
    assert saved["push_enabled"] is False
    assert not saved["umo"]


def temporary_group_event(*, raw_user_id=101, raw_group_id=10001):
    # SnowLuma sends a private event with subtype group. AstrBot still represents
    # this as FriendMessage, but preserves the source group in the raw sender.
    event = Event(umo=NAMED_QQ_DIRECT)
    event.message_obj.raw_message = {
        "post_type": "message",
        "message_type": "private",
        "sub_type": "group",
        "self_id": int(BOT_QQ),
        "user_id": raw_user_id,
        "sender": {"user_id": raw_user_id, "group_id": raw_group_id},
    }
    event.bot = SimpleNamespace(send_private_msg=AsyncMock(side_effect=no_network))
    return event


def assert_temporary_chat_destination(kwargs):
    assert type(kwargs["user_id"]) is int and kwargs["user_id"] == 101
    assert type(kwargs["group_id"]) is int and kwargs["group_id"] == 10001
    assert str(kwargs["self_id"]) == BOT_QQ


@pytest.mark.parametrize(
    "outcome,end_text",
    [("success", "绑定成功"), ("time-out", "过期"), ("auth-rejected", "登录失败")],
)
def test_temporary_private_binding_delivers_before_poll_and_routes_terminal_result(
    plugin_factory, monkeypatch, outcome, end_text,
):
    main, make = plugin_factory
    # Even if friend-chat default push is enabled, a temporary session is not
    # persisted as a destination for notifications or announcements.
    plugin = make({"push_enabled_default": True})
    event = temporary_group_event()
    qr_path = invite_fixture(main, plugin)
    main.qr_finish.side_effect = (
        SklandAuthError("SECRET_PROVIDER_ERROR") if outcome == "auth-rejected" else None
    )
    main.qr_finish.return_value = {"cred": "synthetic-cred"}
    plugin.client.get_arknights_binding = AsyncMock(return_value={
        "uid": SYNTHETIC_UID, "nickname": "synthetic-player", "channel": "test",
    })
    plugin.client.get_garrison_resource = AsyncMock(return_value={
        "versionInfo": {"seasonId": "dummy-season"},
    })
    plugin.client.get_garrison_history = AsyncMock(return_value=[])
    plugin.client.get_player_info = AsyncMock(return_value={})
    elapsed = 1.0
    monkeypatch.setattr(main, "time", SimpleNamespace(
        time=time.time, time_ns=time.time_ns, monotonic=lambda: elapsed,
    ))

    async def scenario():
        sending_qr = asyncio.Event()
        allow_delivery = asyncio.Event()
        deliver_finished = asyncio.Event()
        messages = []

        async def send(**kwargs):
            assert_temporary_chat_destination(kwargs)
            parts = kwargs["message"]
            messages.append(parts)
            if len(messages) == 1:
                assert [part["type"] for part in parts] == ["text", "image"]
                assert "二维码" in parts[0]["data"]["text"]
                assert qr_path.is_file()
                file_uri = parts[1]["data"]["file"]
                assert file_uri.startswith("base64://")
                assert base64.b64decode(file_uri.removeprefix("base64://"), validate=True) == qr_path.read_bytes()
                sending_qr.set()
                await allow_delivery.wait()
            else:
                assert [part["type"] for part in parts] == ["text"]
                text = parts[0]["data"]["text"]
                assert end_text in text and "SECRET_PROVIDER_ERROR" not in text
                if outcome == "success":
                    assert SYNTHETIC_UID in text and "synthetic-cred" not in text
                deliver_finished.set()
            return {"message_id": len(messages)}

        async def poll(scan_id, _proxy):
            assert allow_delivery.is_set() and scan_id == SYNTHETIC_SCAN_ID
            nonlocal elapsed
            if outcome == "time-out":
                elapsed += plugin.qr_timeout + 1
                return ""
            return "synthetic-scan-code"

        main.qr_poll.side_effect = poll
        event.bot.send_private_msg.side_effect = send
        handler = asyncio.create_task(drain(plugin.cmd_bind(event)))
        await asyncio.wait_for(sending_qr.wait(), timeout=2)
        main.qr_poll.assert_not_awaited()
        assert not plugin.bindings
        allow_delivery.set()
        assert await asyncio.wait_for(handler, timeout=2) == []
        await asyncio.wait_for(deliver_finished.wait(), timeout=2)
        await asyncio.sleep(0)
        assert len(messages) == 2

    run(scenario())
    main.qr_start.assert_awaited_once()
    main.qr_poll.assert_awaited_once()
    assert main.qr_finish.await_count == (0 if outcome == "time-out" else 1)
    plugin.context.send_message.assert_not_awaited()
    assert not qr_path.exists()
    if outcome == "success":
        saved = json.loads(plugin.bindings_file.read_text(encoding="utf-8"))
        assert set(saved) == {"101"}
        assert saved["101"]["uid"] == SYNTHETIC_UID
        assert not saved["101"]["bind_umo"]
        assert not saved["101"]["push_enabled"]
        assert not saved["101"]["umo"]
    else:
        assert not plugin.bindings


@pytest.mark.parametrize(
    "raw_user_id,raw_group_id",
    [
        pytest.param(202, 10001, id="different-sender"),
        pytest.param(101, None, id="missing-source-group"),
        pytest.param(101, False, id="boolean-source-group"),
        pytest.param(101, -1, id="negative-source-group"),
    ],
)
def test_invalid_temporary_chat_route_never_requests_a_login(plugin_factory, raw_user_id, raw_group_id):
    main, make = plugin_factory
    plugin = make()
    event = temporary_group_event(raw_user_id=raw_user_id, raw_group_id=raw_group_id)
    responses = run(drain(plugin.cmd_bind(event)))
    assert all(kind == "text" for kind, _ in responses)
    event.bot.send_private_msg.assert_not_awaited()
    plugin.context.send_message.assert_not_awaited()
    main.qr_start.assert_not_awaited()
    main.qr_poll.assert_not_awaited()
    assert not plugin.bindings


@pytest.mark.parametrize(
    "delivery",
    [
        pytest.param(RuntimeError("SECRET_QQ_TRANSPORT_ERROR"), id="transport-error"),
        pytest.param({"message_id": 0}, id="missing-acknowledgement"),
    ],
)
def test_temporary_chat_qr_delivery_error_never_polls_or_falls_back(plugin_factory, delivery):
    main, make = plugin_factory
    plugin = make()
    event = temporary_group_event()
    qr_path = invite_fixture(main, plugin)
    if isinstance(delivery, Exception):
        event.bot.send_private_msg.side_effect = delivery
    else:
        event.bot.send_private_msg.side_effect = None
        event.bot.send_private_msg.return_value = delivery
    responses = run(drain(plugin.cmd_bind(event)))
    assert all(kind == "text" and "SECRET_QQ_TRANSPORT_ERROR" not in text for kind, text in responses)
    nested_calls = event.bot.send_private_msg.await_args_list
    assert nested_calls
    for call in nested_calls:
        assert not call.args
        assert_temporary_chat_destination(call.kwargs)
    assert len([part for part in nested_calls[0].kwargs["message"] if part["type"] == "image"]) == 1
    plugin.context.send_message.assert_not_awaited()
    main.qr_start.assert_awaited_once()
    main.qr_poll.assert_not_awaited()
    main.qr_finish.assert_not_awaited()
    assert not qr_path.exists()
    assert not plugin.bindings
    assert not plugin._binding_tasks


def test_temporary_chat_help_replies_privately_without_starting_login(plugin_factory):
    main, make = plugin_factory
    plugin = make()
    event = temporary_group_event()
    event.bot.send_private_msg.side_effect = None
    event.bot.send_private_msg.return_value = {"message_id": 1}
    assert run(drain(plugin.cmd_help(event))) == []
    event.bot.send_private_msg.assert_awaited_once()
    kwargs = event.bot.send_private_msg.await_args.kwargs
    assert_temporary_chat_destination(kwargs)
    assert [part["type"] for part in kwargs["message"]] == ["text"]
    assert "/卫戍 绑定" in kwargs["message"][0]["data"]["text"]
    main.qr_start.assert_not_awaited()
    plugin.context.send_message.assert_not_awaited()


def test_temporary_chat_user_can_cancel_in_flight_binding(plugin_factory):
    main, make = plugin_factory
    plugin = make()
    qr_path = invite_fixture(main, plugin)
    bind_event, cancel_event = temporary_group_event(), temporary_group_event()
    for event in (bind_event, cancel_event):
        event.bot.send_private_msg.side_effect = None
        event.bot.send_private_msg.return_value = {"message_id": 1}

    async def scenario():
        polling_started = asyncio.Event()

        async def poll_pending(_scan_id, _proxy):
            polling_started.set()
            await asyncio.Event().wait()

        main.qr_poll.side_effect = poll_pending
        assert await asyncio.wait_for(drain(plugin.cmd_bind(bind_event)), timeout=2) == []
        await asyncio.wait_for(polling_started.wait(), timeout=2)
        assert qr_path.is_file()
        assert await asyncio.wait_for(drain(plugin.cmd_unbind(cancel_event)), timeout=2) == []

    run(scenario())
    bind_event.bot.send_private_msg.assert_awaited_once()
    cancel_event.bot.send_private_msg.assert_awaited_once()
    kwargs = cancel_event.bot.send_private_msg.await_args.kwargs
    assert_temporary_chat_destination(kwargs)
    assert [part["type"] for part in kwargs["message"]] == ["text"]
    assert "已取消" in kwargs["message"][0]["data"]["text"]
    main.qr_finish.assert_not_awaited()
    plugin.context.send_message.assert_not_awaited()
    assert not qr_path.exists()
    assert not plugin.bindings
    assert not plugin._binding_tasks


@pytest.mark.parametrize("already_bound", [False, True])
def test_group_binding_only_replies_with_entry_and_never_starts_login(plugin_factory, already_bound):
    main, make = plugin_factory
    plugin = make()
    if already_bound:
        plugin.bindings["101"] = binding(SYNTHETIC_UID, NAMED_QQ_DIRECT)
    before = plugin.bindings.copy()
    responses = run(drain(plugin.cmd_bind(Event(umo=NAMED_QQ_GROUP, addressed=True))))
    assert_group_reply_only(responses)
    assert len(responses) == 1
    assert "机器人头像" in responses[0][1] and "/卫戍 绑定" in responses[0][1]
    plugin.context.send_message.assert_not_awaited()
    main.qr_start.assert_not_awaited()
    main.qr_poll.assert_not_awaited()
    main.qr_finish.assert_not_awaited()
    assert not list(plugin.qr_dir.iterdir())
    assert plugin.bindings == before
    assert plugin._binding_tasks == {}


@pytest.mark.parametrize(
    "event",
    [
        Event(umo="slack-main:GroupMessage:10001", platform_name="slack", addressed=True),
        Event(sender="", umo=NAMED_QQ_GROUP, addressed=True),
        Event(umo=":GroupMessage:10001", addressed=True),
        Event(umo="qq-production:GroupMessage:", addressed=True),
    ],
)
def test_group_binding_entry_does_not_need_a_private_route(plugin_factory, event):
    main, make = plugin_factory
    plugin = make()
    responses = run(drain(plugin.cmd_bind(event)))
    main.qr_start.assert_not_awaited()
    main.qr_poll.assert_not_awaited()
    plugin.context.send_message.assert_not_awaited()
    assert_group_reply_only(responses)
    assert plugin.bindings == {}


@pytest.mark.parametrize(
    "method, arguments, renderer",
    [
        ("cmd_query", (), "render_stats_card"),
        ("cmd_history", ("3",), "render_list_card"),
        ("cmd_match", ("1",), "render_match_card"),
    ],
)
def test_group_can_query_only_requesters_bound_account(plugin_factory, method, arguments, renderer):
    _, make = plugin_factory
    plugin = make()
    plugin.bindings["101"] = binding("own-fixture-uid", DIRECT_1, push_enabled=False)
    plugin.bindings["202"] = binding("other-fixture-uid", GROUP_A, push_enabled=False)
    plugin.client.get_garrison_resource = AsyncMock(return_value={
        "versionInfo": {"seasonId": "dummy-season"},
    })
    plugin.client.get_garrison_history = AsyncMock(return_value=[
        {"id": "own-fixture-match", "sceneId": "fixture-scene", "gameEndTs": 200},
    ])
    plugin.client.get_player_info = AsyncMock(return_value={})

    command = getattr(plugin, method)
    unbound = run(drain(command(Event(sender="303", umo=GROUP_A, addressed=True), *arguments)))
    assert unbound and all(kind == "text" for kind, _ in unbound)
    assert any("还没有绑定" in text for _, text in unbound)
    plugin.client.get_garrison_history.assert_not_awaited()

    responses = run(drain(command(Event(umo=GROUP_A, addressed=True), *arguments)))
    plugin.client.get_garrison_resource.assert_awaited_once_with("fake-cred-for-own-fixture-uid", "own-fixture-uid")
    plugin.client.get_garrison_history.assert_awaited_once_with(
        "fake-cred-for-own-fixture-uid", "own-fixture-uid", "dummy-season",
    )
    plugin.client.get_player_info.assert_awaited_once_with("fake-cred-for-own-fixture-uid", "own-fixture-uid")
    render_call = getattr(plugin.renderer, renderer)
    render_call.assert_awaited_once()
    assert render_call.await_args.args[0]["uid"] == "own-fixture-uid"
    assert any(kind == "image" for kind, _ in responses)
    assert not any(
        "other-fixture-uid" in value or "fake-cred" in value
        for _, value in responses
    )
    plugin.context.send_message.assert_not_awaited()


@pytest.mark.parametrize(
    "config,event",
    [
        ({"admin_ids": ""}, Event(umo=GROUP_A, role="owner", addressed=True)),
        ({"admin_ids": "101"}, Event(umo=GROUP_A, role="admin", addressed=True)),
        ({"admin_ids": ""}, Event(role="owner")),
        ({}, Event(sender="1423687955")),  # upstream config used to authorize a built-in ID
        ({"admin_ids": "909"}, Event()),
    ],
)
def test_third_party_lookup_and_user_list_denied(plugin_factory, config, event):
    _, make = plugin_factory
    plugin = make(config)
    plugin.bindings["202"] = binding("202", GROUP_B)
    plugin._fetch_target_data = AsyncMock(side_effect=no_network)
    assert not plugin._is_admin(event)
    for command in (
        plugin.cmd_admin_lookup(event, "202"),
        plugin.cmd_admin_stats(event, "202"),
        plugin.cmd_admin_users(event),
    ):
        responses = run(drain(command))
        assert responses and all(kind == "text" for kind, _ in responses)
        assert not any("202" in content for _, content in responses)
    plugin._fetch_target_data.assert_not_awaited()


def test_configured_admin_can_list_bindings_only_in_private(plugin_factory):
    _, make = plugin_factory
    plugin = make({"admin_ids": "909， 101"})
    plugin.bindings["202"] = binding("202", GROUP_B)
    replies = run(drain(plugin.cmd_admin_users(Event())))
    assert any("synthetic-player-202" in content for _, content in replies)
    assert not any("fake-cred" in content for _, content in replies)


def test_fresh_private_binding_and_polling_are_opt_in(plugin_factory):
    main, make = plugin_factory
    schema = json.loads((PACKAGE / "_conf_schema.json").read_text(encoding="utf-8-sig"))
    assert schema["push_enabled_default"]["default"] is False
    assert not schema["admin_ids"]["default"]
    plugin = make()
    main.qr_poll.side_effect = None
    main.qr_poll.return_value = "synthetic-scan-code"
    main.qr_finish.side_effect = None
    main.qr_finish.return_value = {"cred": "synthetic-cred", "token": "synthetic-token"}
    plugin.client.get_arknights_binding = AsyncMock(return_value={
        "uid": "202", "nickname": "synthetic-player", "channel": "test",
    })
    plugin.client.get_garrison_resource = AsyncMock(return_value={
        "versionInfo": {"seasonId": "dummy-season"},
    })
    plugin.client.get_garrison_history = AsyncMock(return_value=[])
    plugin.client.get_player_info = AsyncMock(return_value={})
    run(plugin._wait_scan("101", DIRECT_1, "offline-scan-id"))
    main.qr_finish.assert_awaited_once()
    assert plugin.bindings["101"]["push_enabled"] is False
    assert json.loads(plugin.bindings_file.read_text(encoding="utf-8"))["101"]["push_enabled"] is False
    plugin.bindings["unconfigured"] = binding("999", GROUP_A)
    plugin._collect_binding_new = AsyncMock(side_effect=no_network)
    run(plugin._poll_once())
    plugin._collect_binding_new.assert_not_awaited()


def test_legacy_group_push_without_consent_is_disabled_on_load(plugin_factory):
    _, make = plugin_factory
    original = make()
    original.bindings_file.write_text(json.dumps({
        "101": binding("101", GROUP_A, push_enabled=True, token="unneeded-fake-token"),
        "202": binding("202", GROUP_B, push_enabled=True, push_since=123),
    }), encoding="utf-8")
    loaded = make()
    saved = json.loads(loaded.bindings_file.read_text(encoding="utf-8"))
    assert saved["101"]["push_enabled"] is False
    assert not saved["101"]["umo"]
    assert "token" not in saved["101"]
    assert saved["202"]["push_enabled"] is True
    assert saved["202"]["umo"] == GROUP_B


def test_only_the_opted_in_users_push_destination_changes(plugin_factory, monkeypatch):
    main, make = plugin_factory
    plugin = make()
    monkeypatch.setattr(main, "time", SimpleNamespace(time=lambda: 200.0, time_ns=time.time_ns))
    plugin.bindings["101"] = binding("101", DIRECT_1, push_enabled=False)
    untouched = binding("202", GROUP_B, push_enabled=True)
    plugin.bindings["202"] = untouched.copy()
    run(drain(plugin.cmd_push(Event(umo=GROUP_A, addressed=True), "开")))
    state = json.loads(plugin.bindings_file.read_text(encoding="utf-8"))
    assert state["101"]["umo"] == GROUP_A
    assert state["101"]["push_enabled"] is True
    assert state["202"] == untouched
    plugin._get_resource_cached = AsyncMock(return_value={"versionInfo": {"seasonId": "dummy-season"}})
    plugin.client.get_garrison_history = AsyncMock(return_value=[
        {"id": "before-confirmation", "gameEndTs": 190},
        {"id": "after-confirmation", "gameEndTs": 210},
    ])
    new = run(plugin._collect_binding_new("101", plugin.bindings["101"]))
    assert [record["id"] for record in new[2]] == ["after-confirmation"]
    run(drain(plugin.cmd_push(Event(umo=GROUP_A, addressed=True), "关")))
    assert plugin.bindings["101"]["push_enabled"] is False
    assert plugin.bindings["202"] == untouched


def test_same_scene_only_merges_within_destination_and_dedup_is_scoped(plugin_factory):
    _, make = plugin_factory
    plugin = make()
    plugin.bindings = {
        "101": binding("101", GROUP_A, push_enabled=True),
        "202": binding("202", GROUP_B, push_enabled=True),
        "303": binding("303", GROUP_A, push_enabled=True),
    }
    plugin.client.get_garrison_resource = AsyncMock(return_value={
        "versionInfo": {"seasonId": "dummy-season"},
    })
    plugin.client.get_garrison_history = AsyncMock(return_value=[
        {"id": "fixture-match", "sceneId": "shared-scene", "gameEndTs": 200},
    ])
    plugin.client.get_player_info = AsyncMock(return_value={})
    run(plugin._poll_once())
    sent = [
        (call.args[0], kind)
        for call in plugin.context.send_message.await_args_list
        for kind, _ in call.args[1].components
    ]
    assert sorted(sent) == sorted([
        (GROUP_A, "text"), (GROUP_A, "image"), (GROUP_B, "text"), (GROUP_B, "image"),
    ])
    multi_players = plugin.renderer.render_multi_match_card.await_args.args[0]
    assert {player["uid"] for player, _rec in multi_players} == {"101", "303"}
    assert plugin.renderer.render_match_card.await_args.args[0]["uid"] == "202"

    # For the same scene arriving later at another destination, scope persistent
    # deduplication to the destination even after a process restart.
    reloaded = make()
    reloaded.bindings["404"] = binding("404", DIRECT_1, push_enabled=True)
    reloaded.client.get_garrison_resource = plugin.client.get_garrison_resource
    reloaded.client.get_garrison_history = plugin.client.get_garrison_history
    reloaded.client.get_player_info = plugin.client.get_player_info
    run(reloaded._poll_once())
    assert {item.args[0] for item in reloaded.context.send_message.await_args_list} == {DIRECT_1}


def test_group_push_cancelled_while_card_is_rendering(plugin_factory):
    _, make = plugin_factory
    plugin = make()
    plugin.bindings["101"] = binding("101", GROUP_A, push_enabled=True, push_since=120)
    plugin.client.get_garrison_resource = AsyncMock(return_value={
        "versionInfo": {"seasonId": "dummy-season"},
    })
    plugin.client.get_garrison_history = AsyncMock(return_value=[
        {"id": "fixture-match", "sceneId": "shared-scene", "gameEndTs": 200},
    ])
    plugin.client.get_player_info = AsyncMock(return_value={})

    async def close_push_before_render_returns(*args):
        await drain(plugin.cmd_push(Event(umo=GROUP_A, addressed=True), "关"))

    plugin.renderer.render_match_card.side_effect = close_push_before_render_returns
    run(plugin._poll_once())
    plugin.renderer.render_match_card.assert_awaited_once()
    plugin.context.send_message.assert_not_awaited()
