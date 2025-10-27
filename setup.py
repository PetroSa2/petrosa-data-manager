"""
Setup script for Petrosa Data Manager Client Library.
"""

import os
from pathlib import Path

from setuptools import find_packages, setup


def get_version() -> str:
    """
    Get the package version without importing the package.

    This avoids importing dependencies before they're installed.

    Priority order:
    1. RELEASE_VERSION environment variable (set by CI/CD)
    2. VERSION file in repository root
    3. Default fallback: "1.0.0"
    """
    # Priority 1: Environment variable (CI/CD sets this)
    version = os.getenv("RELEASE_VERSION")
    if version:
        return version

    # Priority 2: VERSION file
    try:
        version_file = Path(__file__).parent / "VERSION"
        with open(version_file, encoding="utf-8") as f:
            return f.read().strip()
    except FileNotFoundError:
        pass

    # Priority 3: Default fallback
    return "1.0.0"


with open("README.md", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="petrosa_data_manager_client",
    version=get_version(),
    author="Petrosa Systems",
    author_email="team@petrosa.com",
    description="Client library for Petrosa Data Manager API",
    long_description=long_description,
    long_description_content_type="text/markdown",
    url="https://github.com/petrosa/petrosa-data-manager",
    packages=find_packages(),
    classifiers=[
        "Development Status :: 5 - Production/Stable",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Topic :: Software Development :: Libraries :: Python Modules",
        "Topic :: Database",
        "Topic :: Internet :: WWW/HTTP :: Dynamic Content",
    ],
    python_requires=">=3.8",
    install_requires=[
        "httpx>=0.24.0",
        "pydantic>=1.10.0",
        "tenacity>=8.0.0",
    ],
    extras_require={
        "dev": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "pytest-cov>=4.0.0",
            "black>=23.0.0",
            "ruff>=0.1.0",
            "mypy>=1.0.0",
        ],
        "test": [
            "pytest>=7.0.0",
            "pytest-asyncio>=0.21.0",
            "pytest-cov>=4.0.0",
            "httpx-mock>=0.1.0",
        ],
    },
    entry_points={
        "console_scripts": [
            "data-manager-client=client.cli:main",
        ],
    },
    include_package_data=True,
    zip_safe=False,
)
