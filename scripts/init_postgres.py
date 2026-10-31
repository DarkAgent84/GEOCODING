#!/usr/bin/env python3
"""
Initialize and manage PostgreSQL tables for the GEOCODING project.
"""
import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.abspath(os.path.join(_HERE, ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import init_postgres

if __name__ == "__main__":
    init_postgres.main()
