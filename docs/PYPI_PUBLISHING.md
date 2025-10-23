# PyPI Publishing Integration - Complete

## Overview

The `petrosa-data-manager-client` package is now automatically published to PyPI on every release, with version numbers synchronized to Git tags.

## Package Information

- **Package Name**: `petrosa-data-manager-client`
- **PyPI URL**: https://pypi.org/project/petrosa-data-manager-client/
- **Current Version**: 1.0.23 (auto-incremented)
- **Repository**: https://github.com/PetroSa2/petrosa-data-manager

## Installation

```bash
# Install latest version
pip install petrosa-data-manager-client

# Install specific version
pip install petrosa-data-manager-client==1.0.23

# Upgrade to latest
pip install --upgrade petrosa-data-manager-client
```

## Services Using This Package

### Currently Using
- **petrosa-binance-data-extractor**: `petrosa-data-manager-client>=1.0.0`
- **petrosa-realtime-strategies**: `petrosa-data-manager-client>=1.0.0`

### Commented Out (Using Local Imports)
- **petrosa-tradeengine**: Has dependency commented out

### Not Using
- **petrosa-bot-ta-analysis**: Uses local HTTP client
- **petrosa-socket-client**: Doesn't need data-manager access

## How It Works

### Automated Publishing Workflow

The package is automatically published whenever code is merged to the `main` branch:

1. **Version Generation**: Semantic version auto-incremented (v1.0.0 → v1.0.1 → v1.0.2...)
2. **Git Tag Creation**: Version tag pushed to repository
3. **Package Build**: Client library built with version from Git tag
4. **PyPI Publishing**: Package published to PyPI automatically
5. **K8s Deployment**: Docker image deployed to Kubernetes

### Version Synchronization

```
Git Tag: v1.0.23
   ↓
Environment Variable: RELEASE_VERSION=1.0.23
   ↓
setup.py: version=os.getenv('RELEASE_VERSION', '1.0.0')
   ↓
PyPI Package: petrosa-data-manager-client==1.0.23
```

### GitHub Actions Workflow

The publishing is handled by `.github/workflows/deploy.yml`:

```yaml
publish-to-pypi:
  name: Publish to PyPI
  needs: create-release
  runs-on: ubuntu-latest
  steps:
    - Checkout code
    - Setup Python 3.11
    - Extract version (strip 'v' prefix)
    - Install build tools
    - Build package with version sync
    - Publish to PyPI using PYPI_TOKEN
```

## Package Contents

### Main Module: `client/`

```python
from client import DataManagerClient

# Initialize client
client = DataManagerClient(
    base_url="http://petrosa-data-manager:8000",
    timeout=30,
    max_retries=3
)

# Query data
result = await client.query(
    database="mongodb",
    collection="candles_BTCUSDT_1m",
    filter={"symbol": "BTCUSDT"},
    limit=100
)

# Insert data
result = await client.insert(
    database="mongodb",
    collection="candles_BTCUSDT_1m",
    data=[{...}],
    validate=True
)
```

### Dependencies

- `httpx>=0.24.0`: HTTP client for API requests
- `pydantic>=1.10.0`: Data validation and models
- `tenacity>=8.0.0`: Retry logic and error handling

## Configuration

### GitHub Secrets Required

- **PYPI_TOKEN**: Organization-level secret for PyPI authentication
  - Configured at: GitHub Organization Settings → Secrets → Actions
  - Scope: All public repositories
  - Status: ✅ Active

### setup.py Configuration

The client library is configured in `/setup.py`:

```python
setup(
    name="petrosa-data-manager-client",
    version=os.getenv('RELEASE_VERSION', '1.0.0'),  # Dynamic versioning
    author="Petrosa Systems",
    description="Client library for Petrosa Data Manager API",
    packages=find_packages(),  # Discovers 'client' package
    install_requires=[
        "httpx>=0.24.0",
        "pydantic>=1.10.0",
        "tenacity>=8.0.0",
    ],
)
```

## Deployment History

### Successful Deployments

| Version | Date | Git Tag | Docker Image | PyPI Package | K8s Status |
|---------|------|---------|--------------|--------------|------------|
| 1.0.23 | 2025-10-23 | v1.0.23 | ✅ | ✅ | ✅ Running |
| 1.0.22 | 2025-10-23 | v1.0.22 | ✅ | ❌ | ✅ Running |
| 1.0.21 | 2025-10-23 | v1.0.21 | ✅ | ❌ | ✅ Running |

### Implementation Timeline

1. **PR #12**: Initial PyPI publishing integration
   - Added `publish-to-pypi` job to workflow
   - Implemented dynamic version reading in setup.py
   - ✅ Merged successfully

2. **PR #13**: Fixed package naming
   - Corrected build to use client library (not main service)
   - Temporarily renamed pyproject.toml during build
   - ⚠️ Build failed (setup.py command deprecated)

3. **PR #14**: Fixed build process
   - Installed setuptools and wheel explicitly
   - Used `--universal` flag for compatibility
   - ✅ Successfully published to PyPI

## Verification

### Check PyPI Status

```bash
# Check available versions
pip index versions petrosa-data-manager-client

# Check package metadata
curl -s https://pypi.org/pypi/petrosa-data-manager-client/json | jq '.info.version'
```

### Test Installation

```bash
# Create test environment
python -m venv test_env
source test_env/bin/activate

# Install package
pip install petrosa-data-manager-client

# Verify import
python -c "from client import DataManagerClient; print('✅ Import successful')"
```

### Verify in Services

Update service dependencies to use the latest version:

```bash
# petrosa-binance-data-extractor/requirements.txt
petrosa-data-manager-client>=1.0.23

# petrosa-realtime-strategies/requirements.txt  
petrosa-data-manager-client>=1.0.23
```

## Kubernetes Deployment Status

### Current Deployment

```bash
# Check deployment
kubectl --kubeconfig=k8s/kubeconfig.yaml get deployment petrosa-data-manager -n petrosa-apps

# Verify image version
kubectl --kubeconfig=k8s/kubeconfig.yaml get deployment petrosa-data-manager -n petrosa-apps \
  -o jsonpath='{.spec.template.spec.containers[0].image}'
# Output: yurisa2/petrosa-data-manager:v1.0.23

# Check pod status
kubectl --kubeconfig=k8s/kubeconfig.yaml get pods -n petrosa-apps -l app=data-manager
# Output: 3 pods Running
```

## Troubleshooting

### PyPI Publishing Fails

1. **Check PYPI_TOKEN secret**:
   ```bash
   gh secret list --org PetroSa2 | grep PYPI_TOKEN
   ```

2. **Verify build output**:
   ```bash
   gh run view <run-id> --log | grep "Build Package"
   ```

3. **Check dist/ contents**:
   - Should contain: `petrosa-data-manager-client-{version}.tar.gz`
   - Should contain: `petrosa-data-manager-client-{version}-py2.py3-none-any.whl`

### Version Mismatch

If PyPI version doesn't match Git tag:
1. Check RELEASE_VERSION environment variable in workflow
2. Verify setup.py reads from environment
3. Ensure version stripping works (`v1.0.23` → `1.0.23`)

### Import Errors After Installation

```bash
# Verify package structure
pip show petrosa-data-manager-client

# Check installed files
pip show -f petrosa-data-manager-client | grep "^  client/"

# Reinstall if needed
pip install --force-reinstall petrosa-data-manager-client
```

## Future Improvements

### Planned Enhancements

1. **Trusted Publishers**: Configure PyPI Trusted Publishers for GitHub Actions
   - Eliminates need for PYPI_TOKEN
   - More secure authentication
   - Link: https://pypi.org/manage/project/petrosa-data-manager-client/settings/publishing/

2. **Separate Client Repository**: Consider moving client to separate repo
   - Cleaner versioning
   - Independent release cycles
   - Simplified build process

3. **Documentation Website**: Auto-generate API docs
   - Sphinx or MkDocs
   - Published to GitHub Pages
   - Version-specific documentation

4. **Testing in CI**: Add client library tests
   - Unit tests for client methods
   - Integration tests with mock server
   - Coverage reporting

## References

- **PyPI Package**: https://pypi.org/project/petrosa-data-manager-client/
- **GitHub Repository**: https://github.com/PetroSa2/petrosa-data-manager
- **CI/CD Workflow**: `.github/workflows/deploy.yml`
- **Package Configuration**: `setup.py`
- **Organization Secrets**: https://github.com/organizations/PetroSa2/settings/secrets/actions

---

**Status**: ✅ **FULLY OPERATIONAL**

The PyPI publishing integration is complete and working. The `petrosa-data-manager-client` package is automatically published on every release with synchronized versioning.

