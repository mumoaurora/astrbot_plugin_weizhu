"""Load the plugin from the checkout root, regardless of its directory name."""
import sys
from pathlib import Path
from types import ModuleType

plugin = ModuleType("astrbot_plugin_weizhu")
plugin.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[plugin.__name__] = plugin
