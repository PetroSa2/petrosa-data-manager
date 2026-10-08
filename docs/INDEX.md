# Petrosa Data Manager Documentation Index

## 🚀 Getting Started
- [**README.md**](../README.md) - Project overview, API endpoints, and quick start.
- [**AGENTS.md**](../AGENTS.md) - Repository commands and contributor policy.

## 🏗️ Architecture & Operations
- [**DEPLOYMENT_GUIDE.md**](DEPLOYMENT_GUIDE.md) - Production deployment and leader election.
- [**AUDITOR.md**](AUDITOR.md) - Data integrity validation and health scoring.
- [**klines-retention.md**](klines-retention.md) - MySQL-tier `klines_*` retention job.
- [**candle-consumer-retention-contract.md**](candle-consumer-retention-contract.md) - Execution-path candle consumer inventory and Mongo `candles_*` retention/warm-up window contract.
- [**candle-warmup-continuous-backfill.md**](candle-warmup-continuous-backfill.md) - Continuous warm-up backfill scheduler: design rationale, metrics, alert rules, operator runbook (#319).

## 🔧 CI/CD & Development
- [**Makefile**](../Makefile) - Build, test, lint, and operational commands.

## 📊 Integrations & Distribution
- [**NATS_TRACE_PROPAGATION.md**](NATS_TRACE_PROPAGATION.md) - OpenTelemetry trace context propagation details.
