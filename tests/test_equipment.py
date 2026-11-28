import asyncio
from io import BytesIO

import httpx
from PIL import Image, ImageDraw
from astrbot_plugin_weizhu.renderer import GOLD_HI, TEAL, WeizhuRenderer


def test_equipment_card(tmp_path, monkeypatch):
    r = WeizhuRenderer(str(tmp_path))
    texts = []
    draw_text = ImageDraw.ImageDraw.text
    def capture(draw, xy, text, *args, **kwargs):
        texts.append(text)
        return draw_text(draw, xy, text, *args, **kwargs)
    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    rec = {"chessSquad":[{"equips":[], "chessInfo":{"equipIds":["1","1"]}},
                         {"equips":[{"equipId":"2","equipName":"测试盾"}]},
                         {"equips":[]}, {}]}
    asyncio.run(r.render_match_card({}, rec, tmp_path/"match.png",
                                    {"items":[{"itemId":1,"name":"测试剑"}]}))
    assert texts.count("装备 · 测试剑") == 2
    assert "装备 · 测试盾" in texts
    assert "装备列表为空" in texts and "无可读装备数据" in texts


def test_equipment_card_distinguishes_normal_and_elite(tmp_path, monkeypatch):
    renderer = WeizhuRenderer(str(tmp_path))
    labels = {}
    positions = {}
    draw_text = ImageDraw.ImageDraw.text

    def capture(draw, xy, text, *args, **kwargs):
        labels[text] = kwargs.get("fill")
        positions[text] = xy
        return draw_text(draw, xy, text, *args, **kwargs)

    monkeypatch.setattr(ImageDraw.ImageDraw, "text", capture)
    gray, gold = (100, 100, 100), (195, 139, 42)
    urls = []

    def serve_icon(request):
        urls.append(request.url.path)
        buffer = BytesIO()
        color = gold if request.url.path.endswith("_2.png") else gray
        Image.new("RGB", (128, 128), color).save(buffer, format="PNG")
        return httpx.Response(200, content=buffer.getvalue())

    monkeypatch.setattr(renderer, "_http_client", lambda: httpx.AsyncClient(transport=httpx.MockTransport(serve_icon)))
    record = {
        "chessSquad": [
            {"equips": [{"equipId": "chess_item_1_01_e_a"}, {"equipId": "chess_item_1_01_e_b"}]},
            {"chessInfo": {"equipIds": ["chess_item_1_02_e_b", "chess_item_6_12_e_a"]}},
        ],
    }
    resource = {
        "items": [
            {"itemId": "chess_item_1_01_e_a", "name": "维式重锤",
             "iconUrl": "https://web.hycdn.cn/arknights/game/assets/char_skill/sktok_acarm041_1.png"},
            {"itemId": "chess_item_1_02_e_a", "name": "测试基础名"},
            {"itemId": "chess_item_1_02_e_b", "name": "坚守盾牌"},
            {"itemId": "chess_item_6_12_e_a", "name": "天马之耀"},
        ],
    }
    asyncio.run(renderer.render_match_card({}, record, tmp_path / "match.png", resource))
    assert labels["普通 · 维式重锤"] == TEAL
    assert labels["精锐 · 维式重锤"] == GOLD_HI
    assert labels["精锐 · 坚守盾牌"] == GOLD_HI
    assert labels["精锐 · 天马之耀"] == GOLD_HI
    assert [url.rsplit("/", 1)[-1] for url in urls] == ["sktok_acarm041_1.png", "sktok_acarm041_2.png"]
    with Image.open(tmp_path / "match.png") as card:
        for label, color in (("普通 · 维式重锤", gray), ("精锐 · 维式重锤", gold)):
            x, y = positions[label]
            assert card.getpixel((x - 22, y + 8)) == color
