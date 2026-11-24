"""卫戍协议助手：森空岛扫码绑定 / 战绩查询 / 新对局自动播报。"""
import asyncio
import base64
import hashlib
import json
import os
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

import qrcode

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, filter
from astrbot.api.message_components import At
from astrbot.api.star import Context, Star, StarTools, register

try:
    from .insights import active_layers, rankings, select_history
    from .renderer import WeizhuRenderer, render_trend
    from .skland import SklandAuthError, SklandClient, SklandError, qr_finish, qr_poll, qr_start
except ImportError:  # 兼容以非包方式加载
    from insights import active_layers, rankings, select_history
    from renderer import WeizhuRenderer, render_trend
    from skland import SklandAuthError, SklandClient, SklandError, qr_finish, qr_poll, qr_start


def _game_ts(record) -> int:
    try:
        return int(record.get("gameEndTs") or 0)
    except Exception:  # noqa: BLE001
        return 0


def _private_dir(path: Path):
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name == "posix":
        path.chmod(0o700)


def _scene_cache_key(umo: str, scene: str) -> str:
    return json.dumps([umo, scene], ensure_ascii=False, separators=(",", ":"))


def _onebot_id(value: object) -> int:
    text = str(value) if type(value) in (str, int) else ""
    if not (text.isascii() and text.isdecimal() and 1 <= len(text) <= 15 and text[0] != "0"):
        raise ValueError("invalid OneBot ID")
    return int(text)


@dataclass(frozen=True)
class _QQTemporaryChat:
    send_private: Callable[..., Awaitable[object]]
    user_id: int
    group_id: int
    self_id: int

    @classmethod
    def from_event(cls, event: AstrMessageEvent) -> "_QQTemporaryChat | None":
        if not event.is_private_chat() or event.get_platform_name() != "aiocqhttp":
            return None
        raw = getattr(event.message_obj, "raw_message", None)
        if not isinstance(raw, Mapping) or raw.get("sub_type") != "group":
            return None
        sender = raw.get("sender")
        send_private = getattr(getattr(event, "bot", None), "send_private_msg", None)
        if (
            raw.get("message_type") != "private"
            or not isinstance(sender, Mapping)
            or not callable(send_private)
        ):
            raise ValueError("invalid OneBot temporary message")
        user_id = _onebot_id(event.get_sender_id())
        self_id = _onebot_id(event.get_self_id())
        if (
            _onebot_id(raw.get("user_id")) != user_id
            or ("user_id" in sender and _onebot_id(sender["user_id"]) != user_id)
            or ("self_id" in raw and _onebot_id(raw["self_id"]) != self_id)
        ):
            raise ValueError("OneBot temporary message identities do not match")
        # SnowLuma supplies the source group in the sender of the incoming private event.
        return cls(send_private, user_id, _onebot_id(sender.get("group_id")), self_id)

    async def send(self, text: str, image: Path | None = None):
        segments = [{"type": "text", "data": {"text": text}}]
        if image is not None:
            encoded = base64.b64encode(image.read_bytes()).decode("ascii")
            segments.append({"type": "image", "data": {"file": f"base64://{encoded}"}})
        # This is a reply to the same user-initiated temporary session, not a group send.
        result = await asyncio.wait_for(
            self.send_private(
                user_id=self.user_id, group_id=self.group_id, self_id=self.self_id, message=segments,
            ),
            timeout=12,
        )
        message_id = result.get("message_id") if isinstance(result, Mapping) else None
        if type(message_id) is not int or not message_id:
            raise RuntimeError("OneBot temporary send was not acknowledged")


class RequireBotMention(filter.CustomFilter):
    """A slash prefix or a reply may wake AstrBot; neither is a direct mention."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if event.is_private_chat():
            return True
        bot_id = str(event.get_self_id() or "")
        if not bot_id or bot_id.lower() == "all":
            return False
        return any(
            isinstance(component, At) and str(component.qq) == bot_id
            for component in (event.get_messages() or ())
        )


class WeizhuCommandInput(filter.CustomFilter):
    """Match the entire command group name, independently of the @ requirement."""

    def filter(self, event: AstrMessageEvent, cfg: AstrBotConfig) -> bool:
        if not event.is_at_or_wake_command:
            return False
        # WakingStage may already have stripped the leading slash.
        text = event.get_message_str().strip().removeprefix("/")
        return text.split(maxsplit=1)[:1] == ["卫戍"]


@register(
    "astrbot_plugin_weizhu",
    "授权信息见 LICENSE",
    "卫戍协议助手：森空岛扫码绑定，查询战绩并自动播报新对局",
    "v1.2.1",
)
class WeizhuPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.proxy = str(config.get("proxy", "") or "").strip()
        try:
            self.poll_interval = max(120, int(config.get("poll_interval_seconds", 300) or 300))
        except (TypeError, ValueError):
            self.poll_interval = 300
        try:
            self.max_push = max(1, int(config.get("max_push_per_round", 3) or 3))
        except (TypeError, ValueError):
            self.max_push = 3
        try:
            self.qr_timeout = max(30, int(config.get("qr_timeout_seconds", 100) or 100))
        except (TypeError, ValueError):
            self.qr_timeout = 100
        self.push_default = bool(config.get("push_enabled_default", False))
        raw_admins = config.get("admin_ids", "")
        if isinstance(raw_admins, (list, tuple)):
            self.admin_ids = [str(x).strip() for x in raw_admins if str(x).strip()]
        else:
            self.admin_ids = [
                x.strip()
                for x in str(raw_admins or "").replace("，", ",").split(",")
                if x.strip()
            ]

        self.data_dir: Path = StarTools.get_data_dir("astrbot_plugin_weizhu")
        _private_dir(self.data_dir)
        self.cards_dir = self.data_dir / "cards"
        _private_dir(self.cards_dir)
        self.qr_dir = self.data_dir / "qr"
        _private_dir(self.qr_dir)
        self.bindings_file = self.data_dir / "bindings.json"

        self.client = SklandClient(proxy=self.proxy)
        self.renderer = WeizhuRenderer(str(self.data_dir), proxy=self.proxy)
        self._player_cache: dict[str, tuple[float, dict]] = {}
        self._poller: asyncio.Task | None = None
        self._binding_tasks: dict[str, asyncio.Task] = {}
        self._polling = False
        self._resource_cache: dict[str, dict] = {}
        self._resource_ts: dict[str, float] = {}
        self.bindings: dict[str, dict] = self._load_bindings()
        if self._migrate_bindings():
            self._save_bindings()
        self._pushed_scenes: dict[str, float] = {}
        self._load_pushed_scenes()

    # ================= 生命周期 =================
    async def initialize(self):
        logger.info(
            f"[卫戍] 插件初始化完成，绑定数 {len(self.bindings)}，"
            f"轮询间隔 {self.poll_interval}s，代理 {self.proxy or '无'}"
        )
        self._poller = asyncio.create_task(self._poll_loop())

    async def terminate(self):
        if self._poller is not None:
            self._poller.cancel()
            try:
                await self._poller
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._poller = None
        tasks = list(self._binding_tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self.client.close()

    # ================= 存储 =================
    def _load_bindings(self) -> dict:
        try:
            if self.bindings_file.is_symlink():
                raise OSError("绑定存储不能是符号链接")
            if os.name == "posix":
                self.bindings_file.chmod(0o600)
            data = json.loads(self.bindings_file.read_text(encoding="utf-8"))
            return {k: v for k, v in data.items() if isinstance(v, dict)} if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.error(f"[卫戍] 无法读取绑定存储: {exc}")
            raise

    def _migrate_bindings(self) -> bool:
        changed = False
        for binding in self.bindings.values():
            if "token" in binding:
                binding.pop("token")
                changed = True
            if binding.get("push_enabled") and not binding.get("push_since"):
                binding["push_enabled"] = False
                binding["umo"] = ""
                changed = True
        return changed

    def _save_bindings(self):
        tmp = None
        try:
            # mkstemp on POSIX creates a 0600 file before any credential is written.
            fd, tmp = tempfile.mkstemp(prefix=".bindings-", suffix=".tmp", dir=self.data_dir)
            with os.fdopen(fd, "w", encoding="utf-8") as out:
                json.dump(self.bindings, out, ensure_ascii=False, indent=2)
                out.flush()
                os.fsync(out.fileno())
            os.replace(tmp, self.bindings_file)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[卫戍] 保存绑定失败: {exc}")
            raise
        finally:
            if tmp is not None:
                Path(tmp).unlink(missing_ok=True)

    def _load_pushed_scenes(self):
        path = self.data_dir / "pushed_scenes.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                now = time.time()
                self._pushed_scenes = {
                    str(k): float(v) for k, v in data.items() if now - float(v) < 172800
                }
        except Exception:  # noqa: BLE001
            self._pushed_scenes = {}

    def _save_pushed_scenes(self):
        try:
            path = self.data_dir / "pushed_scenes.json"
            path.write_text(
                json.dumps(self._pushed_scenes, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:  # noqa: BLE001
            pass

    # ================= 数据获取 =================
    async def _get_resource(self, cred: str, uid: str, force: bool = False) -> dict:
        resource = await self.client.get_garrison_resource(cred, uid)
        season = (resource.get("versionInfo") or {}).get("seasonId") or "unknown"
        self._resource_cache[season] = resource
        self._resource_ts[season] = time.time()
        return resource

    async def _get_resource_cached(self, cred: str, uid: str) -> dict:
        resource = await self.client.get_garrison_resource(cred, uid)
        season = (resource.get("versionInfo") or {}).get("seasonId") or "unknown"
        self._resource_cache[season] = resource
        self._resource_ts[season] = time.time()
        return resource

    async def _get_active_season(self, cred: str, uid: str) -> str:
        resource = await self._get_resource_cached(cred, uid)
        return (resource.get("versionInfo") or {}).get("seasonId") or ""

    # ================= 轮询推送 =================
    async def _poll_loop(self):
        await asyncio.sleep(15)
        while True:
            try:
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[卫戍] 轮询异常: {exc}")
            await asyncio.sleep(self.poll_interval)

    async def _poll_once(self):
        if self._polling:
            return
        self._polling = True
        try:
            collected = []
            for sender_id, binding in list(self.bindings.items()):
                if not binding.get("cred"):
                    continue
                if not binding.get("push_enabled", self.push_default) or not binding.get("umo"):
                    continue
                umo = binding["umo"]
                revision = binding.get("push_revision")
                try:
                    item = await self._collect_binding_new(sender_id, binding)
                    if item and self._push_is_current(sender_id, binding, umo, revision):
                        collected.append((umo, revision, item))
                except SklandAuthError as exc:
                    logger.warning(f"[卫戍] {sender_id} 凭证失效: {exc}")
                    await self._notify_invalid(binding)
                except SklandError as exc:
                    logger.warning(f"[卫戍] {sender_id} 拉取失败: {exc}")
                except Exception as exc:  # noqa: BLE001
                    logger.exception(f"[卫戍] {sender_id} 轮询异常: {exc}")
                await asyncio.sleep(1.2)

            if not collected:
                return

            # 只合并明确选择同一会话的玩家；同局在不同会话分别播报。
            groups: dict[tuple[str, str], list] = {}
            for umo, revision, (sender_id, binding, new_records, resource) in collected:
                for record in new_records:
                    scene = str(record.get("sceneId") or "")
                    key = f"scene:{scene}" if scene else f"record:{sender_id}:{record.get('id')}"
                    groups.setdefault((umo, key), []).append(
                        (sender_id, binding, record, resource, revision)
                    )

            pushed = 0
            for (umo, scene), candidates in groups.items():
                if pushed >= self.max_push:
                    break
                key = _scene_cache_key(umo, scene)
                if key in self._pushed_scenes:
                    logger.info(f"[卫戍] 对局 {key} 已播报过，跳过")
                    continue
                items = [
                    (sender_id, binding, record, resource)
                    for sender_id, binding, record, resource, revision in candidates
                    if self._push_is_current(sender_id, binding, umo, revision)
                ]
                if not items:
                    continue
                try:
                    if await self._push_match_group(key, items) is False:
                        continue
                    self._pushed_scenes[key] = time.time()
                    pushed += 1
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[卫戍] 播报对局失败 scene={key}: {exc}")
                await asyncio.sleep(1.5)
            self._save_pushed_scenes()
        finally:
            self._polling = False

    def _push_is_current(self, sender_id: str, binding: dict, umo: str, revision) -> bool:
        return (
            self.bindings.get(sender_id) is binding
            and bool(binding.get("push_enabled", self.push_default))
            and binding.get("umo") == umo
            and binding.get("push_revision") == revision
        )

    async def _collect_binding_new(self, sender_id: str, binding: dict):
        """拉取该用户战绩，返回 (sender_id, binding, new_records, resource) 或 None。"""
        cred = binding["cred"]
        umo = binding.get("umo")
        revision = binding.get("push_revision")
        uid = binding.get("uid") or ""
        if not uid:
            return None
        resource = await self._get_resource_cached(cred, uid)
        season = (resource.get("versionInfo") or {}).get("seasonId") or ""
        if not season:
            return None
        records = await self.client.get_garrison_history(cred, uid, season)
        if not self._push_is_current(sender_id, binding, umo, revision):
            return None
        records = [r for r in records if _game_ts(r) > 0]
        if not records:
            return None
        max_ts = max(_game_ts(r) for r in records)

        # 第一次绑定/赛季切换：只记基线，不补发历史
        if binding.get("season_id") != season or not binding.get("last_ts"):
            binding["season_id"] = season
            binding["last_ts"] = max_ts
            binding["last_id"] = self._latest_id(records)
            self._save_bindings()
            logger.info(f"[卫戍] {sender_id} 基线更新: season={season} ts={max_ts}")
            return None

        last_ts = max(int(binding.get("last_ts") or 0), int(binding.get("push_since") or 0))
        new_records = [r for r in records if _game_ts(r) > last_ts]
        if not new_records:
            return None
        new_records.sort(key=_game_ts)
        binding["last_ts"] = max_ts
        binding["last_id"] = self._latest_id(records)
        binding["last_push_at"] = int(time.time())
        self._save_bindings()
        logger.info(f"[卫戍] {sender_id} 发现 {len(new_records)} 条新对局")
        return sender_id, binding, new_records[: self.max_push], resource

    async def _push_match_group(self, key: str, items: list):
        """播报一组同局对局：1 人正常单局卡，多人合成组队战报大图。"""
        sender_id, binding, record, resource = items[0]
        umo = binding.get("umo") or ""
        if not umo:
            return False
        selected = [(sid, b, b.get("push_revision")) for sid, b, _, _ in items]
        if not all(self._push_is_current(sid, b, umo, r) for sid, b, r in selected):
            return False

        if len(items) == 1:
            player = await self._get_player(binding)
            self.renderer.player_avatar_url = player.get("avatar_url") or ""
            path = self.cards_dir / f"match_{sender_id}_{_game_ts(record)}.png"
            await self.renderer.render_match_card(player, record, str(path), resource)
            label = "🆕 卫戍协议 · 新对局完成"
        else:
            players_records = []
            for sid, b, rec, _res in items:
                players_records.append((await self._get_player(b), rec))
            digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
            path = self.cards_dir / f"multi_{digest}.png"
            await self.renderer.render_multi_match_card(players_records, str(path), resource)
            label = f"🆕 卫戍协议 · 组队战报（{len(items)} 人同局）"
        # 渲染期间发生解绑、关闭或切换目的地时，不发送过时的卡片。
        if not all(self._push_is_current(sid, b, umo, r) for sid, b, r in selected):
            return False
        await self.context.send_message(umo, MessageChain().message(label).file_image(str(path)))
        logger.info(f"[卫戍] 对局已播报，玩家数={len(items)}")
        return True

    @staticmethod
    def _latest_id(records) -> str:
        try:
            latest = max(records, key=_game_ts)
            return str(latest.get("id") or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _player_from_binding(binding: dict) -> dict:
        return {
            "uid": binding.get("uid") or "",
            "nickname": binding.get("nickname") or "博士",
            "channel": binding.get("channel") or "官服",
        }

    async def _notify_invalid(self, binding: dict):
        if binding.get("invalid_notified"):
            return
        binding["invalid_notified"] = True
        binding["push_enabled"] = False
        self._save_bindings()
        umo = binding.get("bind_umo")
        if umo:
            try:
                await self.context.send_message(
                    umo,
                    MessageChain().message(
                        "⚠️ 卫戍协议绑定已失效（森空岛凭证过期），请重新发送 /卫戍 绑定"
                    ),
                )
            except Exception:  # noqa: BLE001
                pass

    async def _get_player(self, binding: dict) -> dict:
        """玩家信息（含游戏内头像 URL），10 分钟内复用。"""
        player = self._player_from_binding(binding)
        uid = player.get("uid") or ""
        cached = self._player_cache.get(uid)
        if cached and time.time() - cached[0] < 600:
            player.update(cached[1])
            return player
        try:
            info = await self.client.get_player_info(binding["cred"], uid)
            status = (info or {}).get("status") or {}
            avatar = status.get("avatar") or {}
            secretary = status.get("secretary") or {}
            patch = {
                "avatar_url": avatar.get("url") or "",
                "avatar_id": avatar.get("id") or "",
                "secretary_char": secretary.get("charId") or "",
            }
            self._player_cache[uid] = (time.time(), patch)
            player.update(patch)
        except Exception as exc:  # noqa: BLE001
            logger.debug(f"[卫戍] 获取游戏内头像失败: {exc}")
        return player

    # ================= 扫码绑定 =================
    def _render_qr(self, url: str) -> Path:
        qr = qrcode.QRCode(box_size=8, border=2)
        qr.add_data(url)
        qr.make(fit=True)
        img = qr.make_image(fill_color="black", back_color="white")
        fd, name = tempfile.mkstemp(prefix="bind_", suffix=".png", dir=self.qr_dir)
        path = Path(name)
        try:
            with os.fdopen(fd, "wb") as out:
                img.save(out, format="PNG")
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        return path

    def _qr_instructions(self) -> str:
        return (
            "请本人用【森空岛 App】扫码登录，不要转发此登录二维码。\n"
            f"二维码 {self.qr_timeout} 秒内有效，扫码后自动完成绑定"
        )

    async def _send_binding_text(self, umo: str, text: str, temporary: _QQTemporaryChat | None):
        try:
            if temporary is not None:
                await temporary.send(text)
            else:
                await self.context.send_message(umo, MessageChain().message(text))
        except Exception as exc:  # noqa: BLE001
            # A failed notification must neither change routes nor re-run a completed login.
            logger.warning(f"[卫戍] 发送绑定通知失败: {type(exc).__name__}")

    async def _run_binding(
        self, sender_id: str, umo: str, ready: asyncio.Future, temporary: _QQTemporaryChat | None = None,
    ):
        qr_path = None
        try:
            start = await qr_start(self.proxy)
            qr_path = self._render_qr(start["url"])
            if ready.cancelled():
                return
            if temporary is not None:
                await temporary.send(self._qr_instructions(), qr_path)
                if ready.cancelled():
                    return
            ready.set_result(qr_path)
            await self._wait_scan(sender_id, umo, start["scan_id"], temporary)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # Do not send API error payloads or request details to a chat.
            logger.warning(f"[卫戍] 绑定流程失败: {type(exc).__name__}")
        finally:
            if qr_path is not None:
                qr_path.unlink(missing_ok=True)

    def _binding_done(self, sender_id: str, ready: asyncio.Future, task: asyncio.Task):
        if self._binding_tasks.get(sender_id) is task:
            self._binding_tasks.pop(sender_id, None)
        if not ready.done():
            ready.set_result(None)
        if not task.cancelled() and task.exception() is not None:
            logger.warning(f"[卫戍] 绑定任务结束异常: {type(task.exception()).__name__}")

    async def _wait_scan(
        self, sender_id: str, umo: str, scan_id: str, temporary: _QQTemporaryChat | None = None,
    ):
        deadline = time.monotonic() + self.qr_timeout
        while time.monotonic() < deadline:
            try:
                scan_code = await qr_poll(scan_id, self.proxy)
                if scan_code:
                    info = await qr_finish(scan_code, self.proxy)
                    cred = info.get("cred") or ""
                    if not cred:
                        raise SklandError("未获得 cred")
                    ark = await self.client.get_arknights_binding(cred)
                    season = await self._get_active_season(cred, ark["uid"])
                    history = await self.client.get_garrison_history(cred, ark["uid"], season)
                    history = [r for r in history if _game_ts(r) > 0]
                    avatar_url = ""
                    try:
                        pinfo = await self.client.get_player_info(cred, ark["uid"])
                        avatar_url = (((pinfo or {}).get("status") or {}).get("avatar") or {}).get("url") or ""
                    except Exception:  # noqa: BLE001
                        pass
                    now = int(time.time())
                    # The temporary chat route is for this request only, not unsolicited notifications.
                    push_enabled = self.push_default and temporary is None
                    self.bindings[sender_id] = {
                        "cred": cred,
                        "avatar_url": avatar_url,
                        "uid": ark["uid"],
                        "nickname": ark["nickname"],
                        "channel": ark["channel"],
                        "bind_umo": umo if temporary is None else "",
                        "umo": umo if push_enabled else "",
                        "push_enabled": push_enabled,
                        "season_id": season,
                        "last_ts": max((_game_ts(r) for r in history), default=0),
                        "last_id": self._latest_id(history) if history else "",
                        "push_since": now if push_enabled else 0,
                        "push_revision": time.time_ns(),
                        "created_at": now,
                        "invalid_notified": False,
                    }
                    try:
                        self._save_bindings()
                    except OSError:
                        self.bindings.pop(sender_id, None)
                        raise
                    await self._send_binding_text(
                        umo,
                        f"✅ 卫戍协议绑定成功\n"
                        f"账号: {ark['nickname']}（{ark['channel']}）\n"
                        f"UID: {ark['uid']}\n"
                        f"新对局播报: {'开启' if push_enabled else '关闭'}"
                        + ("\n回到群里 @机器人 /卫戍 查询 可看战绩。" if temporary is not None else ""),
                        temporary,
                    )
                    logger.info(f"[卫戍] {sender_id} 绑定成功 uid={ark['uid']}")
                    return
            except SklandAuthError as exc:
                logger.warning(f"[卫戍] 扫码换取凭证失败: {type(exc).__name__}")
                await self._send_binding_text(
                    umo, "⚠️ 扫码登录失败，请重新 /卫戍 绑定", temporary,
                )
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[卫戍] 轮询扫码状态异常: {type(exc).__name__}")
            await asyncio.sleep(2.0)
        await self._send_binding_text(
            umo, "⌛ 二维码已过期，请重新发送 /卫戍 绑定", temporary,
        )

    # ================= 指令 =================
    @filter.custom_filter(WeizhuCommandInput, False, priority=1000)
    async def suppress_weizhu_llm(self, event: AstrMessageEvent):
        # AstrBot 4.29.4's fallback checks `not event.call_llm`. True
        # marks the fallback as handled without stopping the command's reply.
        event.should_call_llm(True)

    # AstrBot dispatches each subcommand separately; guard the root and each command.
    @filter.custom_filter(RequireBotMention, False)
    @filter.command_group("卫戍")
    def weizhu(self):
        """卫戍协议助手"""

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("帮助", alias={"help"})
    async def cmd_help(self, event: AstrMessageEvent):
        try:
            temporary = _QQTemporaryChat.from_event(event)
        except ValueError:
            return
        if temporary is not None:
            await self._send_binding_text(
                "", "卫戍协议助手\n临时私聊：/卫戍 绑定 获取登录二维码，/卫戍 解绑 取消或解除绑定。\n"
                "查战绩请回群发送 @机器人 /卫戍 查询。", temporary,
            )
            return
        yield event.plain_result(
            "卫戍协议助手\n"
            "群聊请先 @机器人 再输入以下指令；私聊不需要 @。\n"
            "/卫戍 绑定 —— 群聊查看绑定入口，私聊发送指令获取登录二维码\n"
            "/卫戍 查询 —— 在当前会话查看自己的赛季战绩\n"
            "/卫戍 战绩 [N] —— 最近 N 局（默认 10）；如 /卫戍 战绩 10 多人 中断\n"
            "/卫戍 趋势 [盟约] [模式] —— 最近 10 场叠层；如 /卫戍 趋势 炎 终极\n"
            "/卫戍 对局 [N] —— 第 N 近的一局详情（默认 1）\n"
            "/卫戍 推送 开/关 —— 在想接收播报的会话开启，默认关闭\n"
            "/卫戍 状态 —— 查看绑定信息\n"
            "/卫戍 解绑 —— 解除绑定"
        )


    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("绑定", alias={"bind"})
    async def cmd_bind(self, event: AstrMessageEvent):
        if not event.is_private_chat():
            yield event.plain_result(
                "卫戍协议 · 绑定森空岛\n"
                "点本条消息旁的机器人头像 → 发消息 → 发送 /卫戍 绑定（不用 @）。\n"
                "支持从本群打开的临时私聊，登录二维码只回在私聊里。"
                "若 QQ 不允许临时会话，请先加机器人为好友。"
            )
            return
        try:
            temporary = _QQTemporaryChat.from_event(event)
        except ValueError:
            logger.warning("[卫戍] 无法验证 QQ 临时私聊路由，未生成登录二维码")
            return
        sender_id = event.get_sender_id()
        umo = event.unified_msg_origin
        problem = None
        if not sender_id or not umo:
            problem = "无法识别当前私聊会话，请重新发送 /卫戍 绑定。"
        elif sender_id in self.bindings and self.bindings[sender_id].get("cred"):
            problem = "你已经绑定过森空岛账号了。要换号请先 /卫戍 解绑"
        elif sender_id in self._binding_tasks:
            problem = "已有一个绑定正在等待扫码。可用 /卫戍 解绑 取消"
        if problem:
            if temporary is not None:
                await self._send_binding_text(umo, problem, temporary)
            else:
                yield event.plain_result(problem)
            return
        ready = asyncio.get_running_loop().create_future()
        task = asyncio.create_task(self._run_binding(sender_id, umo, ready, temporary))
        self._binding_tasks[sender_id] = task
        task.add_done_callback(lambda done: self._binding_done(sender_id, ready, done))
        try:
            qr_path = await ready
        except asyncio.CancelledError:
            task.cancel()
            raise
        if temporary is not None:
            if qr_path is None and not task.cancelled():
                await self._send_binding_text(
                    umo, "未能发送登录二维码；可重试临时私聊，或加机器人好友后再绑定。", temporary,
                )
            return
        if qr_path is None or task.done():
            yield event.plain_result("未能生成二维码，绑定已取消或森空岛服务暂不可用")
            return
        yield event.plain_result(self._qr_instructions())
        if not task.done():
            yield event.image_result(str(qr_path))

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("解绑", alias={"unbind"})
    async def cmd_unbind(self, event: AstrMessageEvent):
        try:
            temporary = _QQTemporaryChat.from_event(event)
        except ValueError:
            return
        sender_id = event.get_sender_id()
        pending = self._binding_tasks.get(sender_id)
        if pending and not event.is_private_chat():
            yield event.plain_result("已有扫码绑定正在等待，需私聊机器人取消")
            return
        if pending:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        if sender_id in self.bindings:
            binding = self.bindings.pop(sender_id)
            try:
                self._save_bindings()
            except OSError:
                self.bindings[sender_id] = binding
                text = "解绑失败：无法更新本地绑定存储"
            else:
                self.client.drop_token(binding.get("cred", ""))
                text = "✅ 已解绑卫戍协议账号"
        elif pending:
            text = "✅ 已取消本次扫码绑定"
        else:
            text = "你还没有绑定"
        if temporary is not None:
            await self._send_binding_text("", text, temporary)
        else:
            yield event.plain_result(text)

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("状态", alias={"info"})
    async def cmd_status(self, event: AstrMessageEvent):
        sender_id = event.get_sender_id()
        binding = self.bindings.get(sender_id)
        if not binding:
            yield event.plain_result("你还没有绑定，发送 /卫戍 绑定 扫码绑定")
            return
        last = binding.get("last_push_at")
        last_txt = (
            time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "暂无"
        )
        yield event.plain_result(
            f"卫戍协议绑定信息\n"
            f"账号: {binding.get('nickname')}（{binding.get('channel')}）\n"
            f"UID: {binding.get('uid')}\n"
            f"新对局播报: {'开启' if binding.get('push_enabled') else '关闭'}\n"
            f"当前赛季: {binding.get('season_id')}\n"
            f"上次播报: {last_txt}\n"
            f"轮询间隔: {self.poll_interval}s"
        )

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("推送", alias={"push"})
    async def cmd_push(self, event: AstrMessageEvent, action: str = ""):
        sender_id = event.get_sender_id()
        binding = self.bindings.get(sender_id)
        if not binding:
            yield event.plain_result("你还没有绑定，发送 /卫戍 绑定")
            return
        action = (action or "").strip().lower()
        if action in ("开", "开启", "on", "1", "true"):
            umo = event.unified_msg_origin
            if not umo:
                yield event.plain_result("无法确认播报目的地，设置未更改")
                return
            if not binding.get("push_enabled") or binding.get("umo") != umo:
                binding["push_since"] = int(time.time())
                binding["push_revision"] = time.time_ns()
            binding["umo"] = umo
            binding["push_enabled"] = True
            binding["invalid_notified"] = False
            self._save_bindings()
            yield event.plain_result("✅ 新对局只播报到当前会话；原播报目的地已取消")
        elif action in ("关", "关闭", "off", "0", "false"):
            binding["push_enabled"] = False
            binding["push_revision"] = time.time_ns()
            binding["umo"] = ""
            self._save_bindings()
            yield event.plain_result("✅ 已关闭新对局自动播报")
        else:
            yield event.plain_result(
                f"当前播报状态: {'开启' if binding.get('push_enabled') else '关闭'}\n"
                "用法: /卫戍 推送 开 或 /卫戍 推送 关"
            )

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        sender_id = str(event.get_sender_id() or "")
        return bool(event.is_private_chat() and sender_id and sender_id in self.admin_ids)

    def _bound_user_list_text(self) -> str:
        if not self.bindings:
            return "（还没有人绑定）"
        lines = []
        for sender_id, b in self.bindings.items():
            lines.append(
                f"• {b.get('nickname') or '未知'}  UID:{b.get('uid') or '?'}  (微信:{sender_id})"
            )
        return "\n".join(lines)

    def _resolve_bound_user(self, target: str):
        """按 昵称/森空岛UID/微信ID 找到已绑定用户，返回 (sender_id, binding)。"""
        target = (target or "").strip()
        if not target:
            return None, None
        for sid, b in self.bindings.items():
            if target in (str(sid), str(b.get("uid") or ""), str(b.get("nickname") or "")):
                return sid, b
        for sid, b in self.bindings.items():
            nick = str(b.get("nickname") or "")
            uid = str(b.get("uid") or "")
            if target in nick or (nick.split("#")[0] == target) or (uid and target in uid):
                return sid, b
        return None, None

    async def _fetch_target_data(self, binding: dict):
        """拉取指定绑定用户的数据，返回 (player, resource, records)。"""
        cred = binding.get("cred") or ""
        uid = str(binding.get("uid") or "")
        player = {
            "uid": uid,
            "nickname": binding.get("nickname") or f"UID {uid}",
            "channel": binding.get("channel") or "官服",
            "avatar_url": binding.get("avatar_url") or "",
        }
        try:
            info = await self.client.get_player_info(cred, uid)
            status = (info or {}).get("status") or {}
            avatar = status.get("avatar") or {}
            player["avatar_url"] = avatar.get("url") or player["avatar_url"]
            if status.get("name"):
                player["nickname"] = str(status.get("name"))
        except Exception:  # noqa: BLE001
            pass
        resource, records = await self._load_user_data(binding)
        return player, resource, records

    def _require_binding(self, event: AstrMessageEvent) -> dict | None:
        return self.bindings.get(event.get_sender_id())

    async def _load_user_data(self, binding: dict):
        cred = binding["cred"]
        uid = binding.get("uid") or ""
        resource = await self._get_resource_cached(cred, uid)
        season = (resource.get("versionInfo") or {}).get("seasonId") or ""
        records = await self.client.get_garrison_history(cred, uid, season)
        records = [r for r in records if _game_ts(r) > 0]
        records.sort(key=_game_ts, reverse=True)
        return resource, records

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("查询", alias={"统计", "stats"})
    async def cmd_query(self, event: AstrMessageEvent):
        binding = self._require_binding(event)
        if not binding:
            yield event.plain_result("你还没有绑定，发送 /卫戍 绑定 扫码绑定")
            return
        yield event.plain_result("正在拉取本赛季数据…")
        try:
            resource, records = await self._load_user_data(binding)
        except SklandAuthError:
            yield event.plain_result("森空岛凭证已失效，请重新 /卫戍 绑定")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拉取失败: {exc}")
            return
        if not records:
            yield event.plain_result("本赛季还没有卫戍协议战绩")
            return
        out = self.cards_dir / f"stats_{event.get_sender_id()}.png"
        player = await self._get_player(binding)
        self.renderer.player_avatar_url = player.get("avatar_url") or ""
        try:
            await self.renderer.render_stats_card(
                player, records, str(out), resource
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception(f"[卫戍] 统计卡渲染失败: {exc}")
            yield event.plain_result(f"渲染失败: {exc}")
            return
        yield event.image_result(str(out))

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("战绩", alias={"history"})
    async def cmd_history(self, event: AstrMessageEvent, limit: str = "10", mode: str = "全部", result: str = "全部"):
        binding = self._require_binding(event)
        if not binding:
            yield event.plain_result("你还没有绑定，发送 /卫戍 绑定 扫码绑定")
            return
        try:
            n = max(1, min(20, int(str(limit).strip() or 10)))
        except Exception:  # noqa: BLE001
            n = 10
        try:
            resource, records = await self._load_user_data(binding)
        except SklandAuthError:
            yield event.plain_result("森空岛凭证已失效，请重新 /卫戍 绑定")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拉取失败: {exc}")
            return
        if not records:
            yield event.plain_result("本赛季还没有卫戍协议战绩")
            return
        try:
            selected = select_history(records, mode, result)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        if not selected:
            yield event.plain_result("本赛季没有符合筛选条件的对局")
            return
        indices, records = zip(*selected)
        if n == 1 and mode == result == "全部":
            out = self.cards_dir / f"match_{event.get_sender_id()}_1.png"
            player = await self._get_player(binding)
            self.renderer.player_avatar_url = player.get("avatar_url") or ""
            await self.renderer.render_match_card(
                player, records[0], str(out), resource
            )
            yield event.image_result(str(out))
            yield event.plain_result(
                "💡 这是最近一局的详情卡；看最近 N 局列表用 /卫戍 战绩 N"
            )
            return
        out = self.cards_dir / f"list_{event.get_sender_id()}.png"
        player = await self._get_player(binding)
        self.renderer.player_avatar_url = player.get("avatar_url") or ""
        await self.renderer.render_list_card(
            player, records, str(out), n, resource, indices=indices, scope=f"{mode} / {result}"
        )
        yield event.image_result(str(out))
        yield event.plain_result(
            "💡 查看单局干员/盟约详情：/卫戍 对局 <序号>（1 = 最近一局）"
        )

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("趋势", alias={"叠层"})
    async def cmd_trend(self, event: AstrMessageEvent, name: str = "", mode: str = "全部"):
        binding = self._require_binding(event)
        if not binding:
            yield event.plain_result("你还没有绑定，发送 /卫戍 绑定 扫码绑定")
            return
        try:
            resource, records = await self._load_user_data(binding)
        except SklandAuthError:
            yield event.plain_result("森空岛凭证已失效，请重新 /卫戍 绑定")
            return
        except Exception:
            yield event.plain_result("战绩拉取失败，请稍后重试")
            return
        try:
            games = select_history(records, mode)
        except ValueError as exc:
            yield event.plain_result(str(exc))
            return
        ranked = rankings(r for _, r in games)
        if not ranked:
            yield event.plain_result("该范围暂无激活盟约记录")
            return
        names = {b["bondId"]: b.get("name") or b["bondId"] for b in resource.get("bonds") or [] if b.get("bondId")}
        bid = next((k for k, *_ in ranked if not name or name in (k, names.get(k))), None)
        if bid is None:
            choices = "、".join(names.get(k, k) for k, *_ in ranked)
            yield event.plain_result("该范围内可查的盟约：" + choices)
            return
        points = [(i, active_layers(r).get(bid)) for i, r in reversed(games[:10])]
        key = hashlib.sha256(f"{bid}/{mode}".encode()).hexdigest()[:12]
        out = self.cards_dir / f"trend_{event.get_sender_id()}_{key}.png"
        await asyncio.to_thread(render_trend, names.get(bid, bid), mode, points, str(out))
        yield event.image_result(str(out))

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("对局", alias={"match", "detail"})
    async def cmd_match(self, event: AstrMessageEvent, index: str = "1"):
        binding = self._require_binding(event)
        if not binding:
            yield event.plain_result("你还没有绑定，发送 /卫戍 绑定 扫码绑定")
            return
        try:
            idx = max(1, int(str(index).strip() or 1))
        except Exception:  # noqa: BLE001
            idx = 1
        try:
            resource, records = await self._load_user_data(binding)
        except SklandAuthError:
            yield event.plain_result("森空岛凭证已失效，请重新 /卫戍 绑定")
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拉取失败: {exc}")
            return
        if not records:
            yield event.plain_result("本赛季还没有卫戍协议战绩")
            return
        if idx > len(records):
            yield event.plain_result(f"只有 {len(records)} 局战绩")
            return
        record = records[idx - 1]
        out = self.cards_dir / f"match_{event.get_sender_id()}_{idx}.png"
        player = await self._get_player(binding)
        self.renderer.player_avatar_url = player.get("avatar_url") or ""
        await self.renderer.render_match_card(
            player, record, str(out), resource
        )
        yield event.image_result(str(out))

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("查", alias={"查战绩", "看战绩"})
    async def cmd_admin_lookup(
        self, event: AstrMessageEvent, target: str = "", limit: str = "20"
    ):
        """[管理员] 查询指定用户的最近战绩（含通过次数）

        用法: /卫戍 查 <昵称/UID/微信ID> [场数，默认20]
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可用")
            return
        if not target:
            yield event.plain_result(
                "用法: /卫戍 查 <昵称/UID/微信ID> [场数]\n"
                "已绑定用户：\n" + self._bound_user_list_text()
            )
            return
        sender_id, binding = self._resolve_bound_user(target)
        if not binding:
            yield event.plain_result(
                f"❌ 没找到用户: {target}\n已绑定用户：\n" + self._bound_user_list_text()
            )
            return
        try:
            n = max(1, min(50, int(str(limit).strip() or 20)))
        except Exception:  # noqa: BLE001
            n = 20
        yield event.plain_result(f"正在查 {binding.get('nickname')} 的战绩…")
        try:
            player, resource, records = await self._fetch_target_data(binding)
        except SklandAuthError:
            yield event.plain_result(
                f"❌ {binding.get('nickname')} 的森空岛凭证已失效，需要他重新 /卫戍 绑定"
            )
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拉取失败: {exc}")
            return
        if not records:
            yield event.plain_result(f"{binding.get('nickname')} 本赛季还没有卫戍协议战绩")
            return
        rows = records[:n]
        passed = sum(1 for r in rows if self.renderer.is_success(r))
        self.renderer.player_avatar_url = player.get("avatar_url") or ""
        out = self.cards_dir / f"admin_list_{sender_id or 'x'}.png"
        await self.renderer.render_list_card(player, records, str(out), n, resource)
        yield event.image_result(str(out))
        yield event.plain_result(
            f"👤 {player.get('nickname')}（UID {player.get('uid')}）\n"
            f"最近 {len(rows)} 场：通过 {passed} 次 / 失败 {len(rows) - passed} 次"
            f"（通过率 {passed / len(rows) * 100:.0f}%）"
        )

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("查统计", alias={"看统计", "统计别人"})
    async def cmd_admin_stats(self, event: AstrMessageEvent, target: str = ""):
        """[管理员] 查询指定用户的赛季统计数据

        用法: /卫戍 查统计 <昵称/UID/微信ID>
        """
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可用")
            return
        if not target:
            yield event.plain_result(
                "用法: /卫戍 查统计 <昵称/UID/微信ID>\n"
                "已绑定用户：\n" + self._bound_user_list_text()
            )
            return
        sender_id, binding = self._resolve_bound_user(target)
        if not binding:
            yield event.plain_result(
                f"❌ 没找到用户: {target}\n已绑定用户：\n" + self._bound_user_list_text()
            )
            return
        yield event.plain_result(f"正在统计 {binding.get('nickname')} 的数据…")
        try:
            player, resource, records = await self._fetch_target_data(binding)
        except SklandAuthError:
            yield event.plain_result(
                f"❌ {binding.get('nickname')} 的森空岛凭证已失效，需要他重新 /卫戍 绑定"
            )
            return
        except Exception as exc:  # noqa: BLE001
            yield event.plain_result(f"拉取失败: {exc}")
            return
        if not records:
            yield event.plain_result(f"{binding.get('nickname')} 本赛季还没有卫戍协议战绩")
            return
        self.renderer.player_avatar_url = player.get("avatar_url") or ""
        out = self.cards_dir / f"admin_stats_{sender_id or 'x'}.png"
        await self.renderer.render_stats_card(player, records, str(out), resource)
        yield event.image_result(str(out))
        total = len(records)
        passed = sum(1 for r in records if self.renderer.is_success(r))
        yield event.plain_result(
            f"📊 {player.get('nickname')}：总场次 {total}，通过 {passed} 次，"
            f"通过率 {passed / total * 100:.1f}%"
        )

    @weizhu.custom_filter(RequireBotMention, False)
    @weizhu.command("用户", alias={"绑定用户", "用户列表"})
    async def cmd_admin_users(self, event: AstrMessageEvent):
        """[管理员] 查看已绑定的卫戍用户"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 仅管理员可用")
            return
        yield event.plain_result("已绑定的卫戍用户：\n" + self._bound_user_list_text())

    async def cmd_help2(self, event: AstrMessageEvent):
        yield event.plain_result(
            "卫戍协议助手\n"
            "/卫戍 绑定 / 解绑 / 状态 / 推送 开|关\n"
            "/卫戍 查询 / 战绩 [N] / 对局 [N]"
        )
