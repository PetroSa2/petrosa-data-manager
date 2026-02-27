# Kubernetes Manifests

This directory contains development-time Kubernetes manifests for local testing.

**IMPORTANT**: The canonical source of truth for production manifests is in the central repository:
- `petrosa_k8s/k8s/data-manager/`

## Files

- `*.yaml` - Kubernetes manifests for local development
- `kubeconfig.yaml.example` - Example kubeconfig template (copy and customize for local use)

## Usage

For production deployments, use the manifests from `petrosa_k8s`.
