# Petrosa Data Manager Documentation Index

**Last Updated**: February 23, 2026
**Service Status**: ✅ ACTIVE (Production)

---

## 🚀 Getting Started
- [**README.md**](../README.md) - Project overview, API endpoints, and quick start.
- [**QUICK_REFERENCE.md**](QUICK_REFERENCE.md) - Essential commands and common workflows.

## 🏗️ Architecture & Operations
- [**DEPLOYMENT_GUIDE.md**](DEPLOYMENT_GUIDE.md) - Production deployment and leader election.
- [**MANUAL_DEPLOYMENT_GUIDE.md**](MANUAL_DEPLOYMENT_GUIDE.md) - Procedures for manual overrides.
- [**AUDITOR.md**](AUDITOR.md) - Data integrity validation and health scoring.
- [**klines-retention.md**](klines-retention.md) - MySQL-tier `klines_*` retention job.
- [**candle-consumer-retention-contract.md**](candle-consumer-retention-contract.md) - Execution-path candle consumer inventory and Mongo `candles_*` retention/warm-up window contract.

## 🔧 CI/CD & Development
- [**CI_CD_PIPELINE.md**](CI_CD_PIPELINE.md) - GitHub Actions workflow details.
- [**TESTING.md**](TESTING.md) - Test suite structure and coverage requirements.
- [**MAKEFILE.md**](MAKEFILE.md) - Standardized command reference.

## 📊 Integrations & Distribution
- [**NATS_TRACE_PROPAGATION.md**](NATS_TRACE_PROPAGATION.md) - OpenTelemetry trace context propagation details.
- [**PYPI_PUBLISHING.md**](PYPI_PUBLISHING.md) - Guide for publishing the client library.

## 📜 Historical
- [**Archive Folder**](archive/) - Outdated or issue-specific documentation.
