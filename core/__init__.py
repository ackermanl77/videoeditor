"""Core editing engine: data models, undo/redo, media probing and export.

This package has no GUI dependencies so it can be tested and reused
independently of the PySide6 UI layer in ``ui/``.
"""

#: The single source of the application version. The About box, the
#: ``--self-test`` report, every saved ``.vproj`` (``app_version``) and the
#: packaged exe's Windows version resource (build.spec) all read it, and the
#: release workflow refuses a ``v*`` tag that doesn't match it.
__version__ = "0.2.0"
