"""Shared test configuration and fixtures for SDK tests."""

import os
import sys

# Ensure the SDK source is on the path for all tests
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
