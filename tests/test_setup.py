"""
Tests for setup.py version management.

This module tests the version resolution logic used by setup.py
for dynamic version management from multiple sources.
"""

import os
import tempfile
from pathlib import Path
from unittest.mock import mock_open, patch

import pytest


class TestVersionManagement:
    """Test suite for version resolution logic."""

    def test_get_version_from_env_var(self, monkeypatch):
        """Test that get_version() returns version from RELEASE_VERSION env var."""
        monkeypatch.setenv("RELEASE_VERSION", "2.5.3")

        # Reimport to get fresh version
        import importlib

        import client.version

        importlib.reload(client.version)

        from client.version import get_version

        version = get_version()
        assert version == "2.5.3"

    def test_get_version_from_version_file(self, monkeypatch, tmp_path):
        """Test that get_version() reads from VERSION file when env var not set."""
        # Unset RELEASE_VERSION env var
        monkeypatch.delenv("RELEASE_VERSION", raising=False)

        # Create temporary VERSION file content
        version_content = "1.2.3\n"

        # Mock the file read
        with patch("builtins.open", mock_open(read_data=version_content)):
            with patch("pathlib.Path.exists", return_value=True):
                import importlib

                import client.version

                importlib.reload(client.version)

                from client.version import get_version

                version = get_version()
                assert version == "1.2.3"

    def test_get_version_fallback_when_file_not_found(self, monkeypatch):
        """Test that get_version() returns default '1.0.0' when VERSION file not found."""
        # Unset RELEASE_VERSION env var
        monkeypatch.delenv("RELEASE_VERSION", raising=False)

        # Mock open to raise FileNotFoundError
        def mock_open_error(*args, **kwargs):
            raise FileNotFoundError("VERSION file not found")

        with patch("builtins.open", side_effect=mock_open_error):
            import importlib

            import client.version

            importlib.reload(client.version)

            from client.version import get_version

            version = get_version()
            assert version == "1.0.0"

    def test_get_version_strips_whitespace(self, monkeypatch):
        """Test that get_version() strips whitespace from VERSION file content."""
        # Unset RELEASE_VERSION env var
        monkeypatch.delenv("RELEASE_VERSION", raising=False)

        # Create VERSION file with whitespace
        version_content = "  3.4.5  \n\n"

        with patch("builtins.open", mock_open(read_data=version_content)):
            with patch("pathlib.Path.exists", return_value=True):
                import importlib

                import client.version

                importlib.reload(client.version)

                from client.version import get_version

                version = get_version()
                assert version == "3.4.5"

    def test_get_version_env_var_takes_precedence(self, monkeypatch):
        """Test that RELEASE_VERSION env var takes precedence over VERSION file."""
        # Set env var with specific version
        monkeypatch.setenv("RELEASE_VERSION", "5.0.0")

        # Mock VERSION file with different version
        version_content = "1.0.0"

        with patch("builtins.open", mock_open(read_data=version_content)):
            with patch("pathlib.Path.exists", return_value=True):
                import importlib

                import client.version

                importlib.reload(client.version)

                from client.version import get_version

                version = get_version()
                # Env var should take precedence
                assert version == "5.0.0"

    def test_version_file_exists_and_readable(self):
        """Integration test: Verify VERSION file exists in repository root."""
        version_file = Path(__file__).parent.parent / "VERSION"
        assert version_file.exists(), "VERSION file should exist in repository root"

        # Read and verify content
        with open(version_file, encoding="utf-8") as f:
            content = f.read().strip()
            assert content, "VERSION file should not be empty"
            # Should follow semantic versioning pattern (basic check)
            parts = content.split(".")
            assert len(parts) >= 2, "VERSION should have at least major.minor format"

    def test_version_module_has_version_attribute(self):
        """Test that version module exports __version__ attribute."""
        import client.version

        assert hasattr(client.version, "__version__")
        assert isinstance(client.version.__version__, str)
        assert len(client.version.__version__) > 0

    def test_setup_py_imports_version_correctly(self):
        """Test that setup.py can import get_version from client.version."""
        # This tests the import path is correct
        from client.version import get_version

        assert callable(get_version)

        # Verify it returns a valid version string
        version = get_version()
        assert isinstance(version, str)
        assert len(version) > 0
        # Should be a valid version format (basic check)
        assert "." in version or version.isdigit()


class TestVersionFormat:
    """Test suite for version string format validation."""

    def test_version_follows_semver_pattern(self):
        """Test that version string follows semantic versioning pattern."""
        from client.version import get_version

        version = get_version()

        # Basic semver check: should have at least major.minor
        parts = version.split(".")
        assert len(parts) >= 2, (
            f"Version '{version}' should have at least major.minor format"
        )

        # First two parts should be numeric
        assert parts[0].isdigit(), f"Major version '{parts[0]}' should be numeric"
        assert parts[1].split("-")[0].isdigit(), (
            f"Minor version '{parts[1]}' should start with numeric"
        )

    def test_version_is_not_empty(self):
        """Test that version string is never empty."""
        from client.version import get_version

        version = get_version()
        assert version, "Version should never be empty"
        assert version.strip() == version, (
            "Version should not have leading/trailing whitespace"
        )


class TestSetupPy:
    """Test suite for setup.py file itself."""

    def test_setup_py_syntax_valid(self):
        """Test that setup.py has valid Python syntax."""
        import types

        setup_path = Path(__file__).parent.parent / "setup.py"

        with open(setup_path, encoding="utf-8") as f:
            code = f.read()

        # This will raise SyntaxError if invalid
        try:
            compiled_code = compile(code, str(setup_path), "exec")
            # Assert compilation succeeded and returned a code object
            assert isinstance(compiled_code, types.CodeType), (
                "Compiled code should be a CodeType object"
            )
        except SyntaxError as e:
            pytest.fail(f"setup.py has syntax error: {e}")

    def test_setup_py_imports_are_valid(self):
        """Test that all imports in setup.py are valid."""
        # Test the imports work
        try:
            from setuptools import find_packages, setup

            from client.version import get_version

            # Assert imports succeeded by checking they are callable/exist
            assert callable(setup), "setup should be callable"
            assert callable(find_packages), "find_packages should be callable"
            assert callable(get_version), "get_version should be callable"
        except ImportError as e:
            pytest.fail(f"setup.py has invalid imports: {e}")

    def test_version_function_returns_string(self):
        """Test that get_version() returns a string."""
        from client.version import get_version

        version = get_version()
        assert isinstance(version, str), "get_version() must return a string"


class TestVersionPriority:
    """Test suite for version resolution priority order."""

    def test_priority_order_documented(self):
        """Test that version module documents priority order."""
        import client.version

        docstring = client.version.get_version.__doc__

        assert docstring is not None, "get_version() should have docstring"
        assert "RELEASE_VERSION" in docstring, "Should document env var source"
        assert "VERSION" in docstring, "Should document VERSION file source"
        assert "priority" in docstring.lower(), "Should document priority order"

    def test_env_var_name_is_release_version(self):
        """Test that the correct environment variable name is used."""
        import inspect

        import client.version

        source = inspect.getsource(client.version.get_version)
        assert "RELEASE_VERSION" in source, "Should check RELEASE_VERSION env var"

    def test_version_file_path_is_correct(self):
        """Test that VERSION file is looked up in repository root."""
        import inspect

        import client.version

        source = inspect.getsource(client.version.get_version)
        assert "VERSION" in source, "Should reference VERSION file"
        assert "parent" in source or ".." in source, "Should look in parent directory"
