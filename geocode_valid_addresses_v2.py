#!/usr/bin/env python3
"""
Root entrypoint and backward-compatibility interface for the Credility Geocoding Pipeline.

All modular implementation resides in src/geocoding/.
This file provides 100% backward-compatible CLI execution and module imports.
"""
from __future__ import annotations

import os
import sys

# Ensure src/ is on sys.path
_HERE = os.path.dirname(os.path.abspath(__file__))
_SRC = os.path.join(_HERE, "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)

# Re-export entire geocoding namespace
from geocoding import *
from geocoding import __all__ as _GEO_ALL
import geocoding as _geo

# Re-export all standard library dependencies that were previously accessible in v2
import argparse
import csv
import difflib
import functools
import json
import math
import random
import re
import signal
import sqlite3
import threading
import time
import traceback
import unicodedata
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
import pandas as pd
import requests

__all__ = _GEO_ALL

if __name__ == "__main__":
    sys.exit(main())
