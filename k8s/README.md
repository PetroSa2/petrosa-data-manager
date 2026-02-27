# Kubernetes Manifests

This directory contains Kubernetes manifest examples that mirror production-oriented defaults but are intended for development-time experimentation and local testing.

**IMPORTANT**: The canonical source of truth for **production** manifests is in the central repository:
- `petrosa_k8s/k8s/data-manager/`

## Files

- `*.yaml` - Reference Kubernetes manifests (with production-like defaults such as `ENVIRONMENT=production`, HPA/replica settings) for local testing and experimentation. **Do not use these as-is for real production clusters.**
- `kubeconfig.yaml.example` - Optional example kubeconfig template for local testing. The preferred approach is to use your standard `~/.kube/config` with context switching; if you do copy this file to `kubeconfig.yaml`, treat it as a local-only file (gitignored) and do not commit it.

## Usage

These manifests are intended for:
- Local smoke-testing (e.g., kind/minikube)
- Development-time iteration and debugging
- As a reference for how the service is typically wired in Kubernetes

They are **not** hardened or guaranteed to be safe for production as-is. Some values intentionally mirror production defaults (for example, `ENVIRONMENT: "production"` in `configmap.yaml` and HorizontalPodAutoscaler/replica settings) so behavior is closer to the real environment, but any production deployment **must** use and be driven by the manifests in the central `petrosa_k8s` repository:

- `petrosa_k8s/k8s/data-manager/`

For production deployments (including staging/pre-prod clusters), always use and modify the manifests from `petrosa_k8s` rather than these local examples.
