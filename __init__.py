try:
    from .main import WeizhuPlugin
except ModuleNotFoundError as exc:  # noqa: N818
    if exc.name != "astrbot":
        raise
    WeizhuPlugin = None

Main = WeizhuPlugin
__all__ = ["WeizhuPlugin", "Main"]
