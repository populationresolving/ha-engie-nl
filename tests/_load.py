"""Load the HA-free modules (api, parse) without running the package __init__ (needs Home Assistant)."""

import importlib
import sys
import types
from pathlib import Path

PKG_DIR = Path(__file__).resolve().parent.parent / "custom_components" / "engie_nl"


def load(name: str):
    if "engie_nl" not in sys.modules:
        pkg = types.ModuleType("engie_nl")
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules["engie_nl"] = pkg
    return importlib.import_module(f"engie_nl.{name}")
