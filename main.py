#!/usr/bin/env python3
"""Entry point for the Wire Diagram desktop application.

Launches the two-mode tkinter GUI:
  * Mode 1 - Sales Order (.xlsx) + customer-supplied devices -> Power
             Distribution CSV
  * Mode 2 - Power Distribution CSV -> full-fidelity .drawio wiring diagram
"""
import sys
import os

# Allow running both as a module and as a frozen / script executable.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app.gui import main  # noqa: E402

if __name__ == "__main__":
    main()
