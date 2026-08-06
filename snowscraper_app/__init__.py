"""Internal modules that implement the Snow Scraper touchscreen application.

The executable entry point intentionally remains :mod:`snowgui`.  This package
contains cohesive implementation details that were previously embedded in the
single ``snowgui.py`` file.  Keeping the entry point stable means existing
systemd services and operator commands continue to work unchanged while the
implementation becomes easier to navigate and maintain.
"""
