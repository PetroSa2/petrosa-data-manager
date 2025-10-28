# Manual Deployment Guide - Data Manager

This guide explains how to trigger manual deployments of the Data Manager service with automatic version bumping without requiring code changes.

## Overview

The manual deployment workflow allows you to:
- Deploy data-manager without code changes
- Automatically bump semantic versions (patch/minor/major)
- Build and push Docker images with new version tags
- Update Kubernetes deployment with new image
- Maintain full audit trail
- Run through complete CI/CD validation

## Prerequisites

- Access to the GitHub repository
- Permissions to trigger GitHub Actions workflows
- `KUBECONFIG` secret configured in repository settings
- `GITHUB_TOKEN` with package write permissions (automatic)

## Triggering from GitHub UI

1. **Navigate to Actions tab**
   - Go to: https://github.com/PetroSa2/petrosa-data-manager/actions
   
2. **Select the workflow**
   - Click on "Manual Deployment with Version Bump" in the left sidebar
   
3. **Click "Run workflow"**
   - Click the "Run workflow" button (top right)
   
4. **Fill in parameters**:
   - **Environment**: Select target environment
     - `staging` (default)
     - `production`
   
   - **Version bump type**: Select how to increment version
     - `patch` (default) - Bug fixes (1.2.3 → 1.2.4)
     - `minor` - New features (1.2.3 → 1.3.0)
     - `major` - Breaking changes (1.2.3 → 2.0.0)
   
   - **Reason**: Explain why deployment is needed
     - Example: "Update ConfigMap with new RSI thresholds"
     - Example: "Redeploy after infrastructure changes"
     - Example: "Apply new resource limits"

5. **Monitor deployment**
   - Watch workflow progress in Actions tab
   - Check deployment summary upon completion
   - Review logs if any issues occur

## Triggering from GitHub CLI

### Prerequisites
```bash
# Install GitHub CLI
brew install gh

# Authenticate
gh auth login
```

### Basic Usage

```bash
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=patch \
  -f reason="Update ConfigMap with new API endpoints"
```

### Examples

**Patch deployment (bug fix or config change)**:
```bash
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=patch \
  -f reason="Fix data retrieval timeout handling"
```

**Minor deployment (new feature)**:
```bash
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=minor \
  -f reason="Add support for new aggregation endpoint"
```

**Major deployment (breaking change)**:
```bash
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=major \
  -f reason="Migrate to Data Manager API v2"
```

**Staging deployment**:
```bash
gh workflow run manual-deploy.yml \
  -f environment=staging \
  -f version_bump=patch \
  -f reason="Test new caching strategy"
```

## Version Bumping Rules

Follow semantic versioning principles:

### Patch (X.Y.Z → X.Y.Z+1)
Use for:
- Bug fixes
- Configuration updates
- Security patches
- Documentation changes
- Performance improvements (no API changes)

**Examples**: v1.2.3 → v1.2.4

### Minor (X.Y.Z → X.Y+1.0)
Use for:
- New features (backward compatible)
- New API endpoints
- New strategy implementations
- Deprecations (not removals)
- Significant enhancements

**Examples**: v1.2.3 → v1.3.0

### Major (X.Y.Z → X+1.0.0)
Use for:
- Breaking API changes
- Database schema migrations
- Removal of deprecated features
- Architecture changes
- New required dependencies

**Examples**: v1.2.3 → v2.0.0

## What Happens During Deployment

1. **Checkout code**: Get latest code from repository
2. **Get current version**: Read latest git tag
3. **Bump version**: Calculate new version based on bump type
4. **Create git tag**: Tag repository with new version
5. **Update manifests**: Replace VERSION_PLACEHOLDER with new version
6. **Setup kubectl**: Configure Kubernetes CLI
7. **Deploy**: Apply manifests to cluster
8. **Restart**: Trigger rolling restart of deployment
9. **Wait**: Monitor rollout completion
10. **Record**: Log deployment details
11. **Summarize**: Create workflow summary

## Verification

After deployment completes, verify:

```bash
# Check pod status
kubectl get pods -n petrosa-apps -l app=data-manager

# Check recent logs
kubectl logs -n petrosa-apps -l app=data-manager --tail=50

# Check deployment version
kubectl describe deployment petrosa-data-manager -n petrosa-apps | grep Image
```

## Rollback

If deployment fails or causes issues:

### Via kubectl
```bash
# Rollback to previous version
kubectl rollout undo deployment/petrosa-data-manager -n petrosa-apps

# Check rollback status
kubectl rollout status deployment/petrosa-data-manager -n petrosa-apps
```

### Via GitHub Actions
Simply trigger another deployment with the previous version number or create a new patch with fixes.

## Audit Trail

All deployments are tracked:

### View deployment history
```bash
# In repository
cat deployments/history.log
```

### View GitHub Actions logs
1. Go to Actions tab
2. Click on workflow run
3. View step-by-step logs
4. Check deployment summary

### View git tags
```bash
# List all version tags
git tag -l "v*" --sort=-version:refname

# Show tag details
git show v1.2.3
```

## Common Use Cases

### 1. ConfigMap/Secret Update
```bash
# After updating ConfigMap/Secret, redeploy to pick up changes
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=patch \
  -f reason="Update API endpoints via ConfigMap"
```

### 2. Infrastructure Change
```bash
# After scaling cluster or updating node pools
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=patch \
  -f reason="Redeploy after node pool upgrade"
```

### 3. Force Pod Restart
```bash
# To clear any transient issues
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=patch \
  -f reason="Restart to clear connection pool issues"
```

### 4. Docker Base Image Security Update
```bash
# Rebuild with security patches in base image (no code changes)
gh workflow run manual-deploy.yml \
  -f environment=production \
  -f version_bump=patch \
  -f reason="Rebuild with latest Python security patches"
```

## Best Practices

1. **Always provide clear reason**: Future you will thank you
2. **Use staging first**: Test in staging before production
3. **Follow semantic versioning**: Use correct bump type
4. **Monitor after deployment**: Check logs and metrics
5. **Document major changes**: Update README if behavior changes
6. **Keep audit trail**: Don't delete deployment history

## Troubleshooting

### Workflow fails at "Create git tag"
- **Cause**: Tag already exists
- **Solution**: Either use a higher bump type or delete the existing tag

### Workflow fails at "Deploy to Kubernetes"
- **Cause**: KUBECONFIG secret not configured or invalid
- **Solution**: Verify secret in repository settings

### Workflow fails at "Wait for rollout"
- **Cause**: Pods failing to start (image pull, resource limits, etc.)
- **Solution**: Check pod logs and events:
  ```bash
  kubectl describe pod <pod-name> -n petrosa-apps
  kubectl logs <pod-name> -n petrosa-apps
  ```

### Version bump incorrect
- **Cause**: Wrong bump type selected
- **Solution**: Create new deployment with correct version

## Security Considerations

- **KUBECONFIG secret**: Stored encrypted in GitHub Secrets
- **Audit trail**: All deployments logged with actor and reason
- **Branch flexibility**: Workflow can be triggered from any branch (no branch restriction in workflow)
- **Permissions**: Only authorized users can trigger workflows
- **Recommendation**: Trigger from main/master branch for production deployments to ensure latest code

## Future Enhancements

Potential improvements:
- Slack/Discord notifications
- Automatic rollback on failure
- Deployment approval gates
- Multi-cluster support
- Blue-green deployment option
- Canary deployment option

## Support

For issues or questions:
- Check workflow logs in GitHub Actions
- Review this documentation
- Contact DevOps team
- Create GitHub issue with details

