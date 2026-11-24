from astrbot_plugin_weizhu.insights import rankings, select_history
from astrbot_plugin_weizhu.renderer import render_trend
from PIL import Image
import pytest


def bond(key, n=None):
    return {"bondId": key, "activate": True, "layer": n}


def test_rank_counts_games_not_rows():
    history = [{"bondInfo": [bond("A", 3), bond("A", 5), bond("B", 90), bond("C")]},
               {"bondInfo": [bond("A", 1), bond("C", 0)]}]
    assert rankings(history) == [("A", 2, 5, 3), ("C", 2, None, None), ("B", 1, 90, 90)]


def test_filter_retains_detail_indices():
    records = [{"modeType": "SINGLE"}, {"modeType": "MULTI", "completeState": True},
               {"modeType": "MULTI", "modeDifficulty": "FAST", "completeState": False}]
    assert select_history(records, "多人", "中断") == [(3, records[2])]
    assert select_history(records, "极速") == [(3, records[2])]
    with pytest.raises(ValueError):
        select_history(records, "错字")


@pytest.mark.parametrize("points", [[(2,None)], [(1,30)], [(3,50),(2,None),(1,20)]])
def test_chart(tmp_path, points):
    path = tmp_path / "trend.png"
    render_trend("炎", "终极", points, path)
    with Image.open(path) as img:
        assert img.size == (1000, 500)
        img.verify()
