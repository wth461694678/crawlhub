"""Template files for ``crawlhub platform new``.

The ``*.tpl`` files in this directory are package data, read at runtime via
``importlib.resources``. They are NOT importable Python — the ``.tpl``
suffix keeps them away from ruff / mypy / pytest collection.

Do NOT add real ``.py`` modules here; this ``__init__.py`` exists solely so
``importlib.resources.files('crawlhub.scaffolding')`` can resolve the
``templates`` subdirectory across both source checkouts and installed wheels.
"""
