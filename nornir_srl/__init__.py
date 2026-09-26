try:
    from ._version import __version__
except ImportError:
    try:
        from importlib.metadata import version
        __version__ = version("nornir-srl")
    except Exception:
        __version__ = "0.0.0.dev0+unknown"
