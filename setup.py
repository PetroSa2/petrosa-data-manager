"""
Setup script for Petrosa Data Manager Client Library.
"""

import os
from setuptools import setup, find_packages

with open("README.md", "r", encoding="utf-8") as fh:
    long_description = fh.read()

setup(
    name="petrosa-data-manager-client",
    version=os.getenv('RELEASE_VERSION', '1.0.0'),
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
