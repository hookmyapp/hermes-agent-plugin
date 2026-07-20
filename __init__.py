try:
    from .adapter import register
except ImportError:  # loaded with repo root on sys.path (tests)
    from adapter import register

__all__ = ["register"]
