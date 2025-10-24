# Kubernetes Setup Guide - Data Manager

## Overview

The Data Manager service connects to the **Petrosa MicroK8s cluster** using the centralized kubeconfig at `~/.kube/config`.

**Context name:** `petrosa`

## Quick Commands

```bash
# Check connection
kubectl --context=petrosa get nodes

# View Data Manager deployment
kubectl --context=petrosa get pods -l app=data-manager -n petrosa-apps

# View logs
kubectl --context=petrosa logs -f deployment/data-manager -n petrosa-apps

# Set as default context (optional)
kubectl config use-context petrosa

# Deploy this service
kubectl --context=petrosa apply -f k8s/
```

## Initial Setup

The MicroK8s cluster credentials should already be merged into your `~/.kube/config`. If not:

```bash
# 1. Generate fresh credentials
microk8s config > /tmp/microk8s-config.yaml

# 2. Merge with existing config
KUBECONFIG=~/.kube/config:/tmp/microk8s-config.yaml kubectl config view --flatten > ~/.kube/config.new
mv ~/.kube/config.new ~/.kube/config

# 3. Test connection
kubectl --context=petrosa get nodes
```

## Service-Specific Operations

### Deployment

```bash
# Deploy Data Manager
kubectl --context=petrosa apply -f k8s/

# Check deployment status
kubectl --context=petrosa get deployment data-manager -n petrosa-apps

# Check pods
kubectl --context=petrosa get pods -l app=data-manager -n petrosa-apps
```

### Monitoring

```bash
# View logs
kubectl --context=petrosa logs -f deployment/data-manager -n petrosa-apps

# View recent events
kubectl --context=petrosa get events -n petrosa-apps --sort-by=.metadata.creationTimestamp

# Check resource usage
kubectl --context=petrosa top pods -l app=data-manager -n petrosa-apps
```

### Debugging

```bash
# Describe deployment
kubectl --context=petrosa describe deployment data-manager -n petrosa-apps

# Shell into pod
kubectl --context=petrosa exec -it deployment/data-manager -n petrosa-apps -- /bin/bash

# Port forward for local testing
kubectl --context=petrosa port-forward deployment/data-manager 8000:8000 -n petrosa-apps
```

## Multiple Clusters

If you work with multiple clusters (dev, prod, petrosa), use context switching:

```bash
# List all contexts
kubectl config get-contexts

# Switch to petrosa
kubectl config use-context petrosa

# Or use --context flag
kubectl --context=petrosa get pods
```

## Security Notes

- **NEVER commit** `k8s/kubeconfig.yaml` to version control
- `k8s/kubeconfig.yaml` is in .gitignore to prevent credential exposure
- Use `k8s/kubeconfig.yaml.example` as reference only
- All cluster access should use `~/.kube/config` with context switching

## Troubleshooting

### Connection Issues

```bash
# Check if MicroK8s is running
microk8s status

# Start if needed
microk8s start

# Verify context exists
kubectl config get-contexts | grep petrosa

# Test connection
kubectl --context=petrosa cluster-info
```

### Certificate Issues

```bash
# Use insecure flag (temporary)
kubectl --context=petrosa --insecure-skip-tls-verify get nodes
```

## Additional Resources

- See `../k8s/` for Kubernetes manifests
- Check `.cursorrules` for repository-specific conventions
- Refer to `petrosa_k8s/docs/KUBERNETES_SETUP.md` for detailed cluster management



