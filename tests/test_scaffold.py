import importlib
from pathlib import Path


def test_module_imports_without_hermes_or_aiohttp():
    adapter = importlib.import_module("adapter")
    assert callable(adapter.register)


def test_package_exports_register():
    text = Path("__init__.py").read_text()
    assert "register" in text
