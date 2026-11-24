import asyncio
from PIL import ImageDraw
from astrbot_plugin_weizhu.renderer import WeizhuRenderer


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
