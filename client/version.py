"""
Version management utility for Petrosa Data Manager Client.

This module provides version resolution from multiple sources:
1. RELEASE_VERSION environment variable (highest priority)
2. VERSION file in repository root
3. Default fallback version
"""

import os
from pathlib import Path


def get_version() -> str:
    """
    Get the package version from multiple sources in priority order.

    Priority order:
    1. RELEASE_VERSION environment variable (set by CI/CD)
    2. VERSION file in repository root
    3. Default fallback: "1.0.0"

    Returns:
        str: The resolved version string

    Examples:
        >>> # With env var set
        >>> os.environ["RELEASE_VERSION"] = "2.5.3"
        >>> get_version()
        '2.5.3'

        >>> # From VERSION file
        >>> # (assuming VERSION contains "1.2.3")
        >>> get_version()
        '1.2.3'

        >>> # Fallback when no sources available
        >>> get_version()
        '1.0.0'
    """
    # Priority 1: Environment variable (CI/CD sets this)
    version = os.getenv("RELEASE_VERSION")
    if version:
        return version

    # Priority 2: VERSION file
    try:
        version_file = Path(__file__).parent.parent / "VERSION"
        with open(version_file, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        pass

    # Priority 3: Default fallback
    return "1.0.0"


__version__ = get_version()
