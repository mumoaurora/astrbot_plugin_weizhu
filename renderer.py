"""卫戍协议卡片渲染：赛季统计卡 / 单局详情卡 / 战绩列表卡（卫戍视觉风格）。

资源与图标来自森空岛 garrison resource 接口：
- 赛季背景/LOGO（bbs.hycdn.cn）
- 盟约图标（白色单色，按金色着色）
- 分队头像（干员立绘）
"""
import os
from collections import Counter
from datetime import datetime
from functools import lru_cache
from pathlib import Path

try:
    from .insights import active_layers, rankings
except ImportError:
    from insights import active_layers, rankings

import httpx
from PIL import Image, ImageDraw, ImageFont

# Keep the Chinese glyphs available in headless Linux containers as well as Windows.
FONT_PATH = Path(__file__).resolve().parent / "fonts" / "NotoSansSC.ttf"

# 卫戍协议视觉风格配色（深绿黑 + 黄铜金）
BG = (11, 14, 13)
BG2 = (17, 22, 20)
PANEL = (20, 26, 24)
PANEL2 = (26, 33, 30)
BORDER = (43, 54, 49)
BORDER_HI = (92, 82, 56)
TEXT = (232, 236, 233)
DIM = (143, 160, 154)
GOLD = (201, 179, 126)
GOLD_HI = (230, 211, 163)
GREEN = (88, 199, 133)
RED = (226, 96, 90)
TEAL = (96, 190, 178)

MODE_LABELS = {"LOCAL": "教学模式", "SINGLE": "单人模拟", "MULTI": "协同模拟"}
DIFF_LABELS = {
    "TRAINING": "入门协议",
    "FUNNY": "标准模拟",
    "NORMAL": "险境模拟",
    "HARD": "绝境模拟",
    "ABYSS": "终极模拟",
    "FAST": "极速模拟",
}
SOLO_RANGE = {"FUNNY": "1–4", "NORMAL": "5–8", "HARD": "9–14", "ABYSS": "15–20"}


@lru_cache(maxsize=64)
def font(size, bold=False):
    try:
        face = ImageFont.truetype(str(FONT_PATH), size)
    except OSError as exc:
        raise OSError("无法加载卫戍中文字体，请重新安装包含 fonts/NotoSansSC.ttf 的完整插件 ZIP") from exc
    face.set_variation_by_axes([700 if bold else 400])
    return face


def text_w(draw, text, f):
    box = draw.textbbox((0, 0), str(text), font=f)
    return box[2] - box[0]


def cut(draw, text, f, max_w):
    text = str(text)
    if text_w(draw, text, f) <= max_w:
        return text
    while text and text_w(draw, text + "…", f) > max_w:
        text = text[:-1]
    return text + "…"


def fmt_date(ts, fmt="%m-%d %H:%M"):
    try:
        v = int(str(ts))
        if v > 10**12:
            v //= 1000
        return datetime.fromtimestamp(v).strftime(fmt)
    except Exception:  # noqa: BLE001
        return "未知"


def duration_sec(record):
    try:
        return int(record["gameEndTs"]) - int(record["gameStartTs"])
    except Exception:  # noqa: BLE001
        return 0


def fmt_dur(sec):
    m, s = divmod(max(0, int(sec)), 60)
    if m < 60:
        return f"{m}分{s:02d}秒"
    return f"{m // 60}时{m % 60}分"


def _cover(img, w, h):
    scale = max(w / img.width, h / img.height)
    nw, nh = int(img.width * scale) + 1, int(img.height * scale) + 1
    img2 = img.resize((nw, nh), Image.LANCZOS)
    left = (nw - w) // 2
    top = (nh - h) // 2
    return img2.crop((left, top, left + w, top + h))


def _circle(img, size):
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size, size), fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _rounded(img, size, radius=8):
    img = img.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, size, size), radius, fill=255)
    out = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    out.paste(img, (0, 0), mask)
    return out


def _tint(img, color):
    solid = Image.new("RGBA", img.size, color + (255,))
    solid.putalpha(img.getchannel("A"))
    return solid


class WeizhuRenderer:
    def __init__(self, data_dir: str, proxy: str = ""):
        self.data_dir = data_dir
        self.cache_dir = os.path.join(data_dir, "avatar_cache")
        self.assets_dir = os.path.join(data_dir, "assets")
        os.makedirs(self.cache_dir, exist_ok=True)
        os.makedirs(self.assets_dir, exist_ok=True)
        self.proxy = (proxy or "").strip()
        self.chars = {}
        self.items = {}
        self.strategies = {}
        self.bonds = {}
        self.version = {}
        self.season_bg_url = ""
        self.season_logo_url = ""
        self.player_avatar_url = ""
        self.player_avatar_path = os.path.join(self.assets_dir, "player_avatar.png")
        self._img_cache = {}
        self._draw = None
        self.boss_names: dict = {}
        try:
            import json as _json

            boss_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "boss_names.json")
            if os.path.exists(boss_file):
                self.boss_names = _json.load(open(boss_file, encoding="utf-8"))
        except Exception:  # noqa: BLE001
            self.boss_names = {}

    # ---------- 资源映射 ----------
    def set_resource(self, resource: dict):
        self.chars = {c.get("charId"): c for c in resource.get("chars") or []}
        self.items = {str(i["itemId"]): i for i in resource.get("items") or [] if i.get("itemId")}
        self.strategies = {s.get("strategyId"): s for s in resource.get("strategies") or []}
        self.bonds = {b.get("bondId"): b for b in resource.get("bonds") or []}
        self.version = resource.get("versionInfo") or {}
        imgs = resource.get("image") or []
        if imgs:
            img = imgs[0]
            self.season_bg_url = img.get("backgroundPc") or img.get("backgroundMobile") or ""
            self.season_logo_url = img.get("logoPc") or img.get("logoMobile") or ""

    def season_label(self) -> str:
        v = self.version
        return f"{v.get('phaseName') or '序列'} · {v.get('versionId') or ''}".strip(" ·")

    def mode_label(self, record) -> str:
        return MODE_LABELS.get(record.get("modeType"), record.get("modeType") or "未知")

    def diff_label(self, record) -> str:
        code = record.get("modeDifficulty") or ""
        if record.get("modeType") == "SINGLE" and self.version.get("seasonId") == "act3autochess":
            rng = SOLO_RANGE.get(code)
            if rng:
                return f"规程等级 {rng}"
        return DIFF_LABELS.get(code, code or "未知")

    def band_label(self, band_id) -> str:
        s = self.strategies.get(band_id) or {}
        init = s.get("initiatorName") or band_id or "未知分队"
        name = s.get("name") or ""
        return f"{init} · {name}" if name else init

    def bond_name(self, bond_id) -> str:
        return (self.bonds.get(bond_id) or {}).get("name") or str(bond_id)

    def is_success(self, record) -> bool:
        return bool(record.get("completeState"))

    def trophy_delta(self, record) -> int:
        try:
            return int(record.get("trophyAfter") or 0) - int(record.get("trophyBefore") or 0)
        except Exception:  # noqa: BLE001
            return 0

    def squad(self, record) -> list:
        return record.get("chessSquad") or []

    def squad_equipment(self, member, info):
        """Read only this squad slot; repeated equipment is not deduplicated."""
        empty = False
        for owner in (member, info):
            for key in ("equips", "equipIds", "equipList", "equipmentIds", "equipments"):
                slots = owner.get(key)
                if not isinstance(slots, list):
                    continue
                if not slots:
                    empty = True
                    continue
                equipment = []
                for slot in slots:
                    if isinstance(slot, dict):
                        eid = slot.get("itemId") or slot.get("equipId") or slot.get("id")
                        label = slot.get("name") or slot.get("itemName") or slot.get("equipName")
                    elif isinstance(slot, (str, int)) and not isinstance(slot, bool):
                        eid, label = slot, None
                    else:
                        eid, label = None, None
                    quality = None
                    if isinstance(eid, (int, str)) and not isinstance(eid, bool) and eid:
                        eid = str(eid)
                        item = self.items.get(eid) or {}
                        label = item.get("name") or label
                        if eid.startswith("chess_item_") and eid.endswith(("_e_a", "_e_b")):
                            # Skland may list only the ordinary variant in the catalog.
                            if eid.endswith("_e_b"):
                                quality = "精锐"
                                label = label or (self.items.get(eid[:-1] + "a") or {}).get("name")
                            elif eid == "chess_item_6_12_e_a":  # V3_1 天马之耀 is already golden.
                                quality = "精锐"
                            else:
                                quality = "普通"
                        label = label or eid
                    if isinstance(label, str) and label.strip():
                        equipment.append((label.strip(), quality))
                if equipment:
                    return equipment, "已记录"
        return [], "装备列表为空" if empty else "无可读装备数据"

    def squad_members(self, record) -> list:
        out = []
        for q in self.squad(record):
            info = q.get("chessInfo") or {}
            cid = info.get("charId") or q.get("charId") or ""
            char = self.chars.get(cid) or {}
            equips, equip_status = self.squad_equipment(q, info)
            # 卫戍的干员等级/精英化以「合成等级(mergeLevel)」对应的 traits 为准（官方卡片逻辑）
            try:
                merge_level = int(q.get("mergeLevel") or 0)
            except (TypeError, ValueError):
                merge_level = 0
            traits = char.get("traits") or []
            trait = None
            for tr in traits:
                try:
                    if int((tr or {}).get("mergeLevel") or 0) == merge_level:
                        trait = tr
                        break
                except (TypeError, ValueError):
                    continue
            if trait is None and char.get("isDiyChar") and not traits and merge_level in (0, 1):
                trait = {"evolvePhase": 2, "level": 1 if merge_level == 0 else 60}
            level = trait.get("level") if trait else info.get("charLevel")
            evolve = trait.get("evolvePhase") if trait else info.get("evolvePhase")
            try:
                tier = int(info.get("chessLevel") or char.get("shopLevel") or 0)
            except (TypeError, ValueError):
                tier = 0
            out.append(
                {
                    "char_id": cid,
                    "name": char.get("name") or info.get("charName") or "未知",
                    "level": level,
                    "evolve": evolve,
                    "tier": tier,
                    "merge_level": merge_level,
                    "equips": equips, "equip_status": equip_status,
                }
            )
        return out

    def active_bonds(self, record) -> list:
        return sorted(((bid, self.bond_name(bid), v) for bid, v in active_layers(record).items()),
                      key=lambda row: -(row[2] or 0))

    # ---------- 资源下载 ----------
    async def _fetch(self, client, url, path, force=False):
        if not url or (os.path.exists(path) and not force):
            return
        try:
            resp = await client.get(url)
            if resp.status_code == 200 and resp.content:
                with open(path, "wb") as fh:
                    fh.write(resp.content)
        except Exception:  # noqa: BLE001
            pass

    def _http_client(self):
        kwargs = dict(timeout=25.0, headers={"User-Agent": "Mozilla/5.0"})
        if self.proxy:
            try:
                return httpx.AsyncClient(proxy=self.proxy, **kwargs)
            except TypeError:
                return httpx.AsyncClient(proxies=self.proxy, **kwargs)
        return httpx.AsyncClient(**kwargs)

    async def prepare_assets(self, records) -> None:
        need = {"season_bg": self.season_bg_url, "season_logo": self.season_logo_url}
        for r in records:
            for b in r.get("bondInfo") or []:
                bid = b.get("bondId")
                if bid:
                    need[f"bond_{bid}"] = (self.bonds.get(bid) or {}).get("iconUrl") or ""
            band = r.get("bandId")
            if band:
                need[f"band_{band}"] = (self.strategies.get(band) or {}).get("iconUrl") or ""
        todo = {
            k: (v, os.path.join(self.assets_dir, k + (".jpg" if k == "season_bg" else ".png")))
            for k, v in need.items()
            if v and not os.path.exists(os.path.join(self.assets_dir, k + (".jpg" if k == "season_bg" else ".png")))
        }
        if not todo:
            return
        client = self._http_client()
        try:
            for name, (url, path) in list(todo.items())[:60]:
                await self._fetch(client, url, path)
        finally:
            await client.aclose()

    async def prepare_avatars(self, records, force: bool = False) -> None:
        need = set()
        for r in records:
            for q in self.squad(r):
                info = q.get("chessInfo") or {}
                cid = info.get("charId") or q.get("charId") or ""
                if cid and (force or not os.path.exists(os.path.join(self.cache_dir, f"{cid}.png"))):
                    need.add(cid)
        if not need:
            return
        client = self._http_client()
        try:
            for cid in list(need)[:40]:
                url = (self.chars.get(cid) or {}).get("avatarUrl")
                await self._fetch(client, url, os.path.join(self.cache_dir, f"{cid}.png"), force=force)
        finally:
            await client.aclose()

    async def prepare_player_avatar(self) -> None:
        """实时抓取玩家游戏内头像（每次渲染都刷新，失败保留旧缓存）。"""
        if not self.player_avatar_url:
            return
        client = self._http_client()
        try:
            await self._fetch(client, self.player_avatar_url, self.player_avatar_path, force=True)
        finally:
            await client.aclose()

    # ---------- 图像工具 ----------
    def _asset(self, name, ext=".png"):
        path = os.path.join(self.assets_dir, name + ext)
        if path in self._img_cache:
            return self._img_cache[path]
        img = None
        if os.path.exists(path):
            try:
                img = Image.open(path).convert("RGBA")
            except Exception:  # noqa: BLE001
                img = None
        self._img_cache[path] = img
        return img

    def _avatar(self, char_id, size):
        path = os.path.join(self.cache_dir, f"{char_id}.png")
        if not os.path.exists(path):
            return None
        try:
            return _circle(Image.open(path).convert("RGBA"), size)
        except Exception:  # noqa: BLE001
            return None

    def _bond_icon(self, bond_id, size, color=GOLD_HI):
        img = self._asset(f"bond_{bond_id}")
        if img is None:
            return None
        return _tint(_rounded(img, size, radius=int(size * 0.22)), color)

    def _band_icon(self, band_id, size):
        img = self._asset(f"band_{band_id}")
        if img is None:
            return None
        return _rounded(img, size, radius=int(size * 0.22))

    # ---------- 页眉 / 页脚 ----------
    def _header(self, img, draw, subtitle="", badge_text="", badge_color=GOLD, height=172):
        bg = self._asset("season_bg", ext=".jpg")
        if bg is not None:
            img.paste(_cover(bg, img.width, height), (0, 0))
            overlay = Image.new("RGBA", (img.width, height), (5, 8, 7, 168))
            img.paste(overlay, (0, 0), overlay)
        draw.rectangle((0, 0, img.width, 3), fill=GOLD)
        logo = self._asset("season_logo")
        if logo is not None:
            lw = int(logo.width * 58 / logo.height)
            logo_s = logo.resize((lw, 58), Image.LANCZOS)
            img.paste(logo_s, (48, 40), logo_s)
        else:
            draw.text((48, 40), "卫戍协议 · 规程", font=font(40, True), fill=GOLD_HI)
        if subtitle:
            draw.text((50, 118), subtitle, font=font(16), fill=DIM)
        if badge_text:
            f = font(19, True)
            w = text_w(draw, badge_text, f) + 40
            draw.rounded_rectangle(
                (img.width - 48 - w, 56, img.width - 48, 106), 8,
                fill=(12, 16, 14), outline=badge_color, width=2,
            )
            draw.text((img.width - 48 - w + 20, 68), badge_text, font=f, fill=badge_color)

    def _player_line(self, img, draw, y, player, extra="", width=None):
        width = width or img.width
        draw.rounded_rectangle((40, y, width - 40, y + 84), 12, fill=PANEL, outline=BORDER, width=1)
        draw.rectangle((40, y, 46, y + 84), fill=GOLD)
        p_av = None
        if os.path.exists(self.player_avatar_path):
            try:
                p_av = _circle(Image.open(self.player_avatar_path).convert("RGBA"), 54)
            except Exception:  # noqa: BLE001
                p_av = None
        if p_av is not None:
            img.paste(p_av, (62, y + 15), p_av)
            draw.ellipse((61, y + 14, 117, y + 70), outline=GOLD, width=2)
        else:
            av = Image.new("RGBA", (54, 54), (0, 0, 0, 0))
            d = ImageDraw.Draw(av)
            d.ellipse((0, 0, 54, 54), fill=(34, 42, 39), outline=GOLD, width=2)
            d.text((16, 8), (player.get("nickname") or "博")[:1], font=font(24, True), fill=GOLD_HI)
            img.paste(av, (62, y + 15), av)
        draw.text((134, y + 12), player.get("nickname") or "博士", font=font(25, True), fill=TEXT)
        draw.text(
            (136, y + 50),
            f"UID {player.get('uid', '')} · {player.get('channel', '官服')}"
            + (f" · {extra}" if extra else ""),
            font=font(16),
            fill=DIM,
        )

    def _section(self, draw, y, text, right=""):
        draw.rectangle((48, y + 8, 56, y + 26), fill=GOLD)
        draw.text((66, y), text, font=font(24, True), fill=TEXT)
        if right:
            draw.text(
                (66 + text_w(draw, text, font(24, True)) + 16, y + 8),
                right,
                font=font(16),
                fill=DIM,
            )

    def _footer(self, draw, img, y):
        draw.line((40, y, img.width - 40, y), fill=BORDER, width=1)
        draw.text((48, y + 16), "数据来源：森空岛 · 卫戍协议战绩接口", font=font(15), fill=DIM)
        draw.text(
            (img.width - 360, y + 16),
            f"生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            font=font(14),
            fill=DIM,
        )

    # ---------- 统计卡 ----------
    async def render_stats_card(self, player, records, out_path, resource=None) -> str:
        if resource is not None:
            self.set_resource(resource)
        await self.prepare_assets(records[:60])
        await self.prepare_player_avatar()
        await self.prepare_avatars(records[:1])
        total = len(records)
        success = sum(1 for r in records if self.is_success(r))
        fail = total - success
        win_rate = success / total * 100 if total else 0
        durs = [duration_sec(r) for r in records]
        kills = [int(r.get("killCnt") or 0) for r in records]
        avg_dur = sum(durs) / len(durs) if durs else 0
        max_dur = max(durs) if durs else 0
        avg_kill = sum(kills) / len(kills) if kills else 0
        max_kill = max(kills) if kills else 0
        trophy_now = records[0].get("trophyAfter", "—") if records else "—"
        trophy_peak = max((int(r.get("trophyAfter") or 0) for r in records), default=0)
        trophy_min = min((int(r.get("trophyAfter") or 0) for r in records), default=0)
        band_cnt = Counter(r.get("bandId") for r in records)
        band_rows = []
        for bid, cnt in band_cnt.most_common(5):
            wins = sum(1 for r in records if r.get("bandId") == bid and self.is_success(r))
            band_rows.append((bid, self.band_label(bid), cnt, wins / cnt * 100 if cnt else 0))
        bond_rows = rankings(records)[:6]
        diff_cnt = Counter(self.diff_label(r) for r in records)
        mode_cnt = Counter(self.mode_label(r) for r in records)
        boss_killed = sum(1 for r in records for b in r.get("acBossInfo") or [] if b.get("isKilled"))
        boss_total = sum(1 for r in records for b in r.get("acBossInfo") or [])
        rows = records[:9]

        width = 1200
        band_h = 46 + 5 * 52 + 18
        bond_h = 46 + 6 * 50 + 14
        table_h = 46 + (len(rows) + 1) * 50 + 12
        height = 172 + 104 + 176 + 132 + band_h + 22 + bond_h + 22 + table_h + 70
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        self._draw = draw
        self._header(img, draw, f"STRONGHOLD PROTOCOL · {self.season_label()}", self.season_label(), GOLD)
        y = 172 + 18
        self._player_line(img, draw, y, player, f"当前奖杯 {trophy_now}", width)
        y += 104

        stats = [
            ("总场次", str(total), "协同 + 单人模拟", TEAL),
            ("成功 / 中断", f"{success} / {fail}", f"胜率 {win_rate:.1f}%", GREEN),
            ("平均时长", f"{avg_dur / 60:.1f} 分", f"最长 {max_dur / 60:.0f} 分", GOLD),
            ("平均击杀", f"{avg_kill:.0f}", f"最高 {max_kill}", RED),
        ]
        card_w = (width - 80 - 3 * 14) / 4
        for i, (label, value, sub, color) in enumerate(stats):
            x0 = 40 + i * (card_w + 14)
            draw.rounded_rectangle((x0, y, x0 + card_w, y + 156), 12, fill=PANEL, outline=BORDER, width=1)
            draw.rectangle((x0, y, x0 + card_w, y + 4), fill=color)
            draw.text((x0 + 20, y + 20), label, font=font(18), fill=DIM)
            draw.text((x0 + 20, y + 52), value, font=font(40, True), fill=GOLD_HI if color == GOLD else TEXT)
            draw.text((x0 + 20, y + 112), sub, font=font(16), fill=color)
        y += 176

        draw.rounded_rectangle((40, y, width - 40, y + 112), 12, fill=PANEL, outline=BORDER, width=1)
        draw.text((62, y + 14), "难度分布", font=font(15), fill=DIM)
        draw.text(
            (62, y + 40),
            cut(draw, " · ".join(f"{k} {v}" for k, v in diff_cnt.most_common(5)), font(17), 620),
            font=font(17),
            fill=TEXT,
        )
        draw.text((720, y + 14), "模式分布", font=font(15), fill=DIM)
        draw.text(
            (720, y + 40),
            cut(draw, " · ".join(f"{k} {v}" for k, v in mode_cnt.most_common()), font(17), 400),
            font=font(17),
            fill=TEXT,
        )
        draw.text(
            (62, y + 76),
            f"Boss 击杀累计 {boss_killed} / {boss_total} · 总击杀 {sum(kills)} · 奖杯区间 {trophy_min} → {trophy_peak}",
            font=font(16),
            fill=DIM,
        )
        y += 132

        self._section(draw, y, "常用分队", "TOP 5")
        y += 46
        max_cnt = max((c for _, _, c, _ in band_rows), default=1)
        for i, (bid, name, cnt, wr) in enumerate(band_rows):
            ry = y + i * 52
            draw.rounded_rectangle((40, ry, width - 40, ry + 46), 10, fill=PANEL, outline=BORDER, width=1)
            icon = self._band_icon(bid, 40)
            if icon is not None:
                img.paste(icon, (52, ry + 3), icon)
                tx = 104
            else:
                tx = 62
            draw.text((tx, ry + 11), cut(draw, name, font(19, True), 330), font=font(19, True), fill=TEXT)
            bar_w = int(340 * cnt / max_cnt) if max_cnt else 0
            draw.rectangle((tx + 360, ry + 18, tx + 360 + max(bar_w, 6), ry + 28), fill=GOLD)
            info = f"{cnt} 场 · 胜率 {wr:.0f}%"
            draw.text((width - 60 - text_w(draw, info, font(17)), ry + 12), info, font=font(17), fill=GOLD_HI)
        y += band_h - 46

        self._section(draw, y, "常用盟约", "按激活场次 · TOP 6")
        y += 46
        max_count = bond_rows[0][1] if bond_rows else 1
        for i, (bid, cnt, peak, avg) in enumerate(bond_rows):
            name = self.bond_name(bid)
            ry = y + i * 50
            draw.rounded_rectangle((40, ry, width - 40, ry + 44), 10, fill=PANEL, outline=BORDER, width=1)
            icon = self._bond_icon(bid, 34)
            if icon is not None:
                img.paste(icon, (52, ry + 5), icon)
                tx = 100
            else:
                tx = 62
            draw.text((tx, ry + 10), cut(draw, name, font(19, True), 190), font=font(19, True), fill=TEXT)
            bar_w = int(420 * cnt / max_count)
            draw.rectangle((tx + 230, ry + 17, tx + 230 + max(bar_w, 6), ry + 27), fill=TEAL)
            info = f"{cnt} 场 · 最高 {peak} · 均层 {avg:.0f}" if peak is not None else f"{cnt} 场 · 无层数"
            draw.text((width - 60 - text_w(draw, info, font(17)), ry + 11), info, font=font(17), fill=GOLD_HI)
        y += bond_h - 46

        self._section(draw, y, "最近对局", f"最近 {len(rows)} 局 / 共 {total} 局")
        y += 46
        draw.rounded_rectangle((40, y, width - 40, y + (len(rows) + 1) * 50 + 8), 12, fill=PANEL, outline=BORDER, width=1)
        heads = ["时间", "模式", "难度", "结果", "分队", "击杀", "奖杯", "时长"]
        xs = [58, 175, 290, 450, 570, 770, 870, 965]
        for i, h in enumerate(heads):
            draw.text((xs[i], y + 14), h, font=font(16, True), fill=DIM)
        for i, r in enumerate(rows):
            ry = y + 50 * (i + 1)
            draw.line((58, ry - 4, width - 58, ry - 4), fill=(32, 40, 36), width=1)
            ok = self.is_success(r)
            delta = self.trophy_delta(r)
            vals = [
                fmt_date(r.get("gameEndTs")),
                self.mode_label(r),
                self.diff_label(r),
                "成功" if ok else "中断",
                self.band_label(r.get("bandId")),
                str(r.get("killCnt") or 0),
                ("+" if delta >= 0 else "") + str(delta),
                fmt_dur(duration_sec(r)),
            ]
            for j, v in enumerate(vals):
                color = TEXT
                if j == 3:
                    color = GREEN if ok else RED
                elif j == 6:
                    color = GREEN if delta >= 0 else RED
                elif j == 0:
                    color = DIM
                draw.text((xs[j], ry + 8), cut(draw, v, font(16), 190 if j == 4 else 120), font=font(16), fill=color)

        self._footer(draw, img, height - 62)
        img.save(out_path)
        return out_path

    # ---------- 单局详情卡 ----------
    async def render_match_card(self, player, record, out_path, resource=None) -> str:
        if resource is not None:
            self.set_resource(resource)
        await self.prepare_assets([record])
        await self.prepare_player_avatar()
        await self.prepare_avatars([record], force=True)
        members = self.squad_members(record)
        bonds = self.active_bonds(record)
        bosses = record.get("acBossInfo") or []
        events = record.get("events") or []
        ok = self.is_success(record)
        delta = self.trophy_delta(record)
        squad_rows = max(1, (len(members) + 4) // 5) if members else 1
        width = 1200
        bond_rows_n = ((len(bonds) + 3) // 4) if bonds else 1
        height = 172 + 100 + 22 + 2 * 104 + 22 + 96
        squad_pitch = max(150, 106 + 24 * max((len(m["equips"]) for m in members), default=0))
        height += 44 + squad_rows * squad_pitch + 10
        height += 44 + 48 * bond_rows_n + 14
        if bosses:
            height += 44 + 20 + 40 * min(len(bosses), 10)
        if events:
            height += 44 + 28 * min(len(events), 8)
        height += 80
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        self._draw = draw
        self._header(
            img,
            draw,
            f"{fmt_date(record.get('gameEndTs'), '%Y-%m-%d %H:%M')} · {self.season_label()}",
            "模拟成功" if ok else "模拟中断",
            GREEN if ok else RED,
        )
        y = 172 + 14
        self._player_line(img, draw, y, player)
        y += 100

        # Three prominent values remain readable in a compact QQ preview.
        for i, (label, value) in enumerate((
            ("本场奖杯", f"{delta:+d}" if all(record.get(k) is not None for k in ("trophyAfter", "trophyBefore")) else "—"),
            ("击杀", record.get("killCnt")), ("花费资金", record.get("coinCost")),
        )):
            x = 40 + 377 * i
            draw.rounded_rectangle((x, y, x + 362, y + 78), 8, fill=PANEL2, outline=BORDER)
            draw.text((x + 16, y + 7), label, font=font(16), fill=DIM)
            draw.text((x + 16, y + 28), str(value if value is not None else "—"), font=font(29, True), fill=TEAL)
        y += 96
        info = [
            ("难度", self.diff_label(record), GOLD_HI),
            ("模式", self.mode_label(record), TEXT),
            ("分队", self.band_label(record.get("bandId")), TEXT),
            ("奖杯变化", f"{record.get('trophyBefore')} → {record.get('trophyAfter')}（{'+' if delta >= 0 else ''}{delta}）", GOLD if delta >= 0 else RED),
            ("同步次数", str(record.get("syncCnt") or 0), TEXT),
            ("回合", str(record.get("passRound") or 0), TEXT),
            ("时长", fmt_dur(duration_sec(record)), TEXT),
            ("Boss 击杀", f"{sum(1 for b in bosses if b.get('isKilled'))} / {len(bosses)}", TEXT),
        ]
        cell_w = (width - 80 - 3 * 14) / 4
        for i, (k, v, color) in enumerate(info):
            col = i % 4
            row = i // 4
            x0 = int(40 + col * (cell_w + 14))
            y0 = int(y + row * 104)
            draw.rounded_rectangle((x0, y0, x0 + cell_w, y0 + 92), 12, fill=PANEL, outline=BORDER, width=1)
            draw.rectangle((x0, y0, x0 + 3, y0 + 92), fill=GOLD if row == 0 else BORDER_HI)
            draw.text((x0 + 16, y0 + 12), k, font=font(14), fill=DIM)
            if k == "分队":
                band_icon = self._band_icon(record.get("bandId"), 26)
                vx = x0 + 16
                if band_icon is not None:
                    img.paste(band_icon, (x0 + 14, y0 + 36), band_icon)
                    vx = x0 + 48
                s = self.strategies.get(record.get("bandId")) or {}
                init = s.get("initiatorName") or v
                name = s.get("name") or ""
                draw.text((vx, y0 + 32), cut(draw, init, font(15, True), cell_w - (vx - x0) - 16), font=font(15, True), fill=color)
                if name:
                    draw.text((vx, y0 + 58), cut(draw, name, font(13), cell_w - (vx - x0) - 16), font=font(13), fill=DIM)
            else:
                draw.text((x0 + 16, y0 + 40), cut(draw, v, font(18, True), cell_w - 28), font=font(18, True), fill=color)
        y += 2 * 104 + 22

        self._section(draw, y, "本局阵容", f"{len(members)} 名干员")
        y += 44
        draw.rounded_rectangle((40, y, width - 40, y + squad_rows * squad_pitch + 6), 12, fill=PANEL, outline=BORDER, width=1)
        if not members:
            draw.text((64, y + 24), "这局没有记录到阵容数据", font=font(17), fill=DIM)
        for i, m in enumerate(members):
            col = i % 5
            row = i // 5
            cx = 62 + col * 224
            cy = y + 16 + row * squad_pitch
            av = self._avatar(m["char_id"], 70)
            if av is not None:
                img.paste(av, (cx, cy), av)
                draw.ellipse((cx, cy, cx + 70, cy + 70), outline=GOLD, width=2)
            else:
                draw.ellipse((cx, cy, cx + 70, cy + 70), fill=(34, 42, 39), outline=GOLD, width=2)
                draw.text((cx + 22, cy + 18), m["name"][:1], font=font(26, True), fill=GOLD_HI)
            ev = m.get("evolve")
            ev_txt = ""
            if ev is not None:
                try:
                    ev_txt = ["精0", "精1", "精2"][min(2, max(0, int(ev)))]
                except (TypeError, ValueError):
                    ev_txt = ""
            draw.text((cx + 84, cy + 2), cut(draw, m["name"], font(18, True), 124), font=font(18, True), fill=TEXT)
            lv = f"Lv{m['level']}" if m.get("level") is not None else ""
            extra = ""
            if int(m.get("merge_level") or 0) >= 1:
                extra = " · 合体"
            elif ev_txt:
                extra = f" · {ev_txt}"
            draw.text((cx + 84, cy + 28), cut(draw, lv + extra, font(13), 138), font=font(13), fill=GOLD_HI)
            tier = int(m.get("tier") or 0)
            if tier > 0:
                stars = "★" * min(6, tier)
                draw.text((cx + 84, cy + 46), stars, font=font(12), fill=GOLD)
            lines = m["equips"] or [(m["equip_status"], None)]
            for j, (name, quality) in enumerate(lines):
                ey = cy + 78 + 24 * j
                draw.rounded_rectangle((cx, ey, cx + 203, ey + 22), 4, fill=PANEL2)
                label = f"{quality or '装备'} · {name}" if m["equips"] else name
                color = GOLD_HI if quality == "精锐" else TEAL if m["equips"] else DIM
                draw.text((cx + 5, ey + 1), cut(draw, label, font(15), 193),
                          font=font(15), fill=color)
        y += squad_rows * squad_pitch + 10

        self._section(draw, y, "本局盟约", f"{len(bonds)} 个激活")
        y += 44
        if bonds:
            cx = 40
            for bid, name, layer in bonds[:12]:
                txt = f"{name}  {layer}" if layer is not None else name
                w = text_w(draw, txt, font(18, True)) + 76
                if cx + w > width - 40:
                    cx = 40
                    y += 48 + 4
                draw.rounded_rectangle((cx, y, cx + w, y + 42), 10, fill=PANEL2, outline=BORDER_HI, width=1)
                icon = self._bond_icon(bid, 30)
                if icon is not None:
                    img.paste(icon, (cx + 8, y + 6), icon)
                else:
                    draw.ellipse((cx + 10, y + 9, cx + 36, y + 35), fill=(42, 50, 45))
                draw.text((cx + 46, y + 8), txt, font=font(18, True), fill=GOLD_HI)
                cx += w + 12
            y += 48 + 14
        else:
            draw.text((64, y), "本局没有激活的盟约（对局中断或盟约未成形时常见）", font=font(17), fill=DIM)
            y += 48 + 14

        if bosses:
            self._section(draw, y, "Boss 击杀明细", f"{sum(1 for b in bosses if b.get('isKilled'))} / {len(bosses)}")
            y += 44
            draw.rounded_rectangle((40, y, width - 40, y + 20 + 40 * min(len(bosses), 10)), 12, fill=PANEL, outline=BORDER, width=1)
            for i, b in enumerate(bosses[:10]):
                ry = y + 14 + i * 40
                bid = str(b.get("bossId") or "?")
                binfo = self.boss_names.get(bid) or {}
                bname = binfo.get("name") or bid
                draw.text((64, ry), cut(draw, bname, font(17, True), 300), font=font(17, True), fill=TEXT)
                color = GREEN if b.get("isKilled") else RED
                draw.text((400, ry), "已击杀" if b.get("isKilled") else "未击杀", font=font(17, True), fill=color)
                hidden = b.get("isHidden") or binfo.get("hidden")
                if hidden:
                    draw.text((520, ry), "隐藏 Boss", font=font(15), fill=DIM)
            y += 20 + 40 * min(len(bosses), 10) + 16

        if events:
            self._section(draw, y, "对局事件")
            y += 44
            for ev in events[:8]:
                draw.text((64, y), cut(draw, str(ev), font(15), width - 120), font=font(15), fill=DIM)
                y += 28
            y += 8

        self._footer(draw, img, height - 62)
        img.save(out_path)
        return out_path

    # ---------- 同局多人组队战报 ----------
    async def render_multi_match_card(self, players_records, out_path, resource=None) -> str:
        """把同一局的多名绑定玩家拼成一张大图。

        players_records: [(player_dict, record), ...]（按顺序）
        """
        if resource is not None:
            self.set_resource(resource)
        records = [r for _, r in players_records]
        await self.prepare_assets(records)
        await self.prepare_player_avatar()
        for _, r in players_records:
            await self.prepare_avatars([r])

        width = 1400
        meta_h = 96
        panel_h = 330
        first_rec = records[0]
        killed_names = []
        for b in first_rec.get("acBossInfo") or []:
            if b.get("isKilled"):
                binfo = self.boss_names.get(str(b.get("bossId") or "")) or {}
                killed_names.append(binfo.get("name") or str(b.get("bossId") or ""))
        boss_strip_h = 40 if killed_names else 0
        height = 172 + meta_h + boss_strip_h + panel_h * len(players_records) + 76
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        self._draw = draw
        ok_all = all(self.is_success(r) for r in records)
        ok_any = any(self.is_success(r) for r in records)
        badge = "组队成功" if ok_all else ("组队中断" if not ok_any else "部分成功")
        badge_color = GREEN if ok_all else (RED if not ok_any else GOLD)
        first = records[0]
        self._header(
            img,
            draw,
            f"{fmt_date(first.get('gameEndTs'), '%Y-%m-%d %H:%M')} · {self.season_label()} · {len(records)} 人同局",
            badge,
            badge_color,
        )

        y = 172
        # 公共信息条
        draw.rounded_rectangle((40, y, width - 40, y + meta_h - 16), 12, fill=PANEL, outline=BORDER, width=1)
        draw.rectangle((40, y, 46, y + meta_h - 16), fill=GOLD)
        boss_killed = sum(1 for b in first.get("acBossInfo") or [] if b.get("isKilled"))
        boss_total = len(first.get("acBossInfo") or [])
        meta = [
            ("难度", self.diff_label(first)),
            ("模式", self.mode_label(first)),
            ("时长", fmt_dur(duration_sec(first))),
            ("Boss 击杀", f"{boss_killed} / {boss_total}"),
            ("对局ID", str(first.get("sceneId") or "-")[:14]),
        ]
        cx = 64
        for label, value in meta:
            draw.text((cx, y + 14), label, font=font(14), fill=DIM)
            draw.text((cx, y + 40), cut(draw, value, font(19, True), 150), font=font(19, True), fill=TEXT)
            cx += 186
        y += meta_h
        if killed_names:
            draw.rounded_rectangle((40, y, width - 40, y + 34), 10, fill=PANEL2, outline=BORDER_HI, width=1)
            draw.text(
                (64, y + 7),
                cut(draw, "击杀 Boss：" + "、".join(killed_names), font(16), width - 160),
                font=font(16),
                fill=GOLD_HI,
            )
            y += boss_strip_h

        # 每人一块
        for idx, (player, record) in enumerate(players_records):
            top = y + idx * panel_h
            ok = self.is_success(record)
            delta = self.trophy_delta(record)
            draw.rounded_rectangle((40, top, width - 40, top + panel_h - 16), 14, fill=PANEL, outline=BORDER, width=1)
            draw.rectangle((40, top, 46, top + panel_h - 16), fill=GREEN if ok else RED)
            # 头像
            av = self._avatar(player.get("uid") or "", 0)  # 占位，下面用玩家头像
            p_av = None
            if self.player_avatar_path and os.path.exists(self.player_avatar_path):
                try:
                    p_av = _circle(Image.open(self.player_avatar_path).convert("RGBA"), 76)
                except Exception:  # noqa: BLE001
                    p_av = None
            # 玩家头像每个玩家不同，这里用昵称首字（保持通用）；有 avatar_url 则临时下载
            if player.get("avatar_url"):
                try:
                    import httpx

                    kwargs = dict(timeout=15.0, headers={"User-Agent": "Mozilla/5.0"})
                    if self.proxy:
                        try:
                            client = httpx.Client(proxy=self.proxy, **kwargs)
                        except TypeError:
                            client = httpx.Client(proxies=self.proxy, **kwargs)
                    else:
                        client = httpx.Client(**kwargs)
                    resp = client.get(player["avatar_url"])
                    client.close()
                    if resp.status_code == 200:
                        import io as _io

                        p_av = _circle(Image.open(_io.BytesIO(resp.content)).convert("RGBA"), 76)
                except Exception:  # noqa: BLE001
                    pass
            px, py = 64, top + 18
            if p_av is not None:
                img.paste(p_av, (px, py), p_av)
                draw.ellipse((px - 1, py - 1, px + 77, py + 77), outline=GOLD, width=2)
            else:
                draw.ellipse((px, py, px + 76, py + 76), fill=(34, 42, 39), outline=GOLD, width=2)
                draw.text((px + 24, py + 18), (player.get("nickname") or "博")[:1], font=font(30, True), fill=GOLD_HI)
            draw.text((px + 92, py + 2), cut(draw, player.get("nickname") or "博士", font(24, True), 300), font=font(24, True), fill=TEXT)
            draw.text((px + 94, py + 34), f"UID {player.get('uid', '')} · {player.get('channel', '官服')}", font=font(15), fill=DIM)
            draw.text((px + 94, py + 56), cut(draw, self.band_label(record.get("bandId")), font(17), 330), font=font(17), fill=GOLD_HI)
            # 结果和奖杯
            draw.text((px + 470, py + 2), "模拟成功" if ok else "模拟中断", font=font(22, True), fill=GREEN if ok else RED)
            draw.text(
                (px + 470, py + 36),
                f"奖杯 {record.get('trophyBefore')} → {record.get('trophyAfter')}（{'+' if delta >= 0 else ''}{delta}）",
                font=font(17),
                fill=GOLD if delta >= 0 else RED,
            )
            # 数据
            stats = [
                ("击杀", str(record.get("killCnt") or 0)),
                ("同步", str(record.get("syncCnt") or 0)),
                ("金币", str(record.get("coinCost") or 0)),
                ("回合", str(record.get("passRound") or 0)),
                ("时长", fmt_dur(duration_sec(record))),
            ]
            sx = px + 470
            sy = py + 76
            for label, value in stats:
                draw.text((sx, sy), label, font=font(13), fill=DIM)
                draw.text((sx, sy + 20), value, font=font(17, True), fill=TEXT)
                sx += 120

            # 阵容（紧凑头像行）
            members = self.squad_members(record)
            mx, my = 64, top + 146
            draw.text((mx, top + 118), f"阵容 {len(members)}", font=font(14), fill=DIM)
            for i, m in enumerate(members[:10]):
                cell = mx + i * 128
                avi = self._avatar(m["char_id"], 48)
                if avi is not None:
                    img.paste(avi, (cell, my), avi)
                    draw.ellipse((cell, my, cell + 48, my + 48), outline=GOLD, width=1)
                else:
                    draw.ellipse((cell, my, cell + 48, my + 48), fill=(34, 42, 39), outline=GOLD, width=1)
                draw.text((cell, my + 54), cut(draw, m["name"], font(12), 100), font=font(12), fill=TEXT)
                lv = f"Lv{m['level']}" if m.get("level") is not None else ""
                if int(m.get("merge_level") or 0) >= 1:
                    lv += " 合体"
                draw.text((cell, my + 70), lv, font=font(11), fill=GOLD_HI)

            # 盟约
            bonds = self.active_bonds(record)
            bx, by = 64, top + 244
            draw.text((bx, by), f"盟约 {len(bonds)}", font=font(14), fill=DIM)
            by += 26
            for bid, name, layer in bonds[:8]:
                txt = f"{name} {layer}" if layer is not None else name
                w = text_w(draw, txt, font(15)) + 56
                if bx + w > width - 64:
                    break
                draw.rounded_rectangle((bx, by, bx + w, by + 34), 10, fill=PANEL2, outline=BORDER_HI, width=1)
                icon = self._bond_icon(bid, 24)
                if icon is not None:
                    img.paste(icon, (bx + 8, by + 5), icon)
                draw.text((bx + 38, by + 8), txt, font=font(15), fill=GOLD_HI)
                bx += w + 10

        self._footer(draw, img, height - 62)
        img.save(out_path)
        return out_path

    # ---------- 战绩列表卡 ----------
    async def render_list_card(self, player, records, out_path, limit=10, resource=None, indices=None, scope="全部") -> str:
        if resource is not None:
            self.set_resource(resource)
        await self.prepare_assets(records[:limit])
        await self.prepare_player_avatar()
        rows = records[:limit]
        passed = sum(1 for r in rows if self.is_success(r))
        failed = len(rows) - passed
        pass_rate = passed / len(rows) * 100 if rows else 0
        width = 1200
        height = 172 + 100 + 76 + 40 + (len(rows) + 1) * 54 + 70
        img = Image.new("RGB", (width, height), BG)
        draw = ImageDraw.Draw(img)
        self._draw = draw
        self._header(
            img,
            draw,
            f"{scope} · 共 {len(records)} 局 · 显示最近 {len(rows)} 局",
            self.season_label(),
        )
        y = 172 + 14
        self._player_line(img, draw, y, player)
        y += 100
        # 通过次数汇总
        draw.rounded_rectangle((40, y, width - 40, y + 62), 12, fill=PANEL, outline=BORDER, width=1)
        draw.rectangle((40, y, 46, y + 62), fill=GOLD)
        draw.text((64, y + 10), f"最近 {len(rows)} 局", font=font(16), fill=DIM)
        draw.text((64, y + 32), f"通过 {passed} 次", font=font(22, True), fill=GREEN)
        draw.text((220, y + 34), f"失败 {failed} 次", font=font(18), fill=RED)
        draw.text((360, y + 34), f"通过率 {pass_rate:.0f}%", font=font(18), fill=GOLD_HI)
        draw.text(
            (560, y + 34),
            f"总击杀 {sum(int(r.get('killCnt') or 0) for r in rows)}",
            font=font(18),
            fill=DIM,
        )
        y += 76
        draw.rounded_rectangle((40, y, width - 40, y + (len(rows) + 1) * 54 + 8), 12, fill=PANEL, outline=BORDER, width=1)
        heads = ["序号", "时间", "模式", "难度", "结果", "分队", "击杀", "奖杯", "时长"]
        xs = [58, 120, 254, 370, 520, 620, 830, 930, 1030]
        for i, h in enumerate(heads):
            draw.text((xs[i], y + 16), h, font=font(16, True), fill=DIM)
        for i, r in enumerate(rows):
            ry = y + 54 * (i + 1)
            draw.line((58, ry - 4, width - 58, ry - 4), fill=(32, 40, 36), width=1)
            ok = self.is_success(r)
            delta = self.trophy_delta(r)
            vals = [
                str(indices[i] if indices is not None else i + 1),
                fmt_date(r.get("gameEndTs")),
                self.mode_label(r),
                self.diff_label(r),
                "成功" if ok else "中断",
                self.band_label(r.get("bandId")),
                str(r.get("killCnt") or 0),
                ("+" if delta >= 0 else "") + str(delta),
                fmt_dur(duration_sec(r)),
            ]
            for j, v in enumerate(vals):
                color = TEXT
                if j == 4:
                    color = GREEN if ok else RED
                elif j == 7:
                    color = GREEN if delta >= 0 else RED
                elif j == 0:
                    color = DIM
                draw.text((xs[j], ry + 10), cut(draw, v, font(16), (xs[j+1] if j+1 < len(xs) else width-40) - xs[j] - 8), font=font(16), fill=color)
        self._footer(draw, img, height - 62)
        img.save(out_path)
        return out_path


def render_trend(name, scope, samples, out_path):
    """samples: (season index, measured layers or None), oldest first."""
    img = Image.new("RGB", (1000, 500), BG)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 1000, 5), fill=TEAL)
    d.text((46, 28), "盟约叠层  /  " + cut(d, name, font(34, True), 480), font=font(34, True), fill=TEXT)
    values = [v for _, v in samples if v is not None]
    avg = sum(values) / len(values) if values else None
    summary = f"均值 {avg:.1f}   ·   最高 {max(values)}" if values else "暂无可用层数"
    d.text((48, 88), f"{scope}   /   最近 {len(samples)} 场   /   有层数 {len(values)} 场   /   {summary}", font=font(20), fill=DIM)
    left, right, top, bottom = 85, 926, 171, 360
    peak = max(values, default=1)
    def at(v):
        return bottom - v / peak * (bottom-top)
    for frac in (0, .5, 1):
        y = at(peak*frac)
        d.line((left,y,right,y), fill=BORDER)
        d.text((left-10,y), f"{peak*frac:g}", anchor="rm", font=font(14), fill=DIM)
    if avg is not None:
        for x in range(left,right,16):
            d.line((x,at(avg),x+8,at(avg)), fill=GOLD)
    previous = None
    for i, (idx, v) in enumerate(samples):
        x = left + (right-left)*(i/(len(samples)-1) if len(samples)>1 else .5)
        d.text((x, bottom+18), f"#{idx}", anchor="mt", font=font(17), fill=DIM)
        if v is None:
            d.text((x,bottom-8), "—", anchor="ms", font=font(19), fill=DIM)
            previous = None
            continue
        y = at(v)
        if previous:
            d.line((*previous,x,y), fill=TEAL, width=4)
        d.ellipse((x-5,y-5,x+5,y+5), fill=TEAL)
        d.text((x,y-12), str(v), anchor="mb", font=font(18,True), fill=TEXT)
        previous = (x,y)
    d.text((48,435), "旧 → 新   ·   # 为赛季对局序号   ·   虚线为均值   ·   — 未激活或无层数，不计入均值", font=font(18), fill=DIM)
    img.save(out_path)
