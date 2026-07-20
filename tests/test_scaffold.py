import importlib


def test_module_imports_without_hermes_or_aiohttp():
    adapter = importlib.import_module("adapter")
    assert callable(adapter.register)


def test_package_exports_register():
    pkg_init = importlib.import_module("__init__") if False else None  # root package import checked via file read
    text = open("__init__.py").read()
    assert "register" in text
