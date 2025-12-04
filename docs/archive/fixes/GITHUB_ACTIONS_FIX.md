# GitHub Actions Deploy Workflow Fix

**Issue**: Deploy workflow fails when trying to checkout `petrosa_k8s` repository  
**Error**: `Not Found - https://docs.github.com/rest/repos/repos#get-a-repository`  
**Status**: ⚠️ Manual fix required (GitHub settings/permissions)

---

## Problem

The Deploy workflow in `.github/workflows/deploy.yml` attempts to:
1. ✅ Build and push Docker image - **WORKS**
2. ✅ Create release - **WORKS**
3. ❌ Checkout `PetroSa2/petrosa_k8s` repository - **FAILS**
4. ❌ Update manifests in `petrosa_k8s` - **SKIPPED**
5. ❌ Deploy to Kubernetes - **SKIPPED**

**Root Cause:**  
The default `GITHUB_TOKEN` doesn't have permission to access other repositories in the organization.

---

## Solution Options

### Option 1: Personal Access Token (Recommended)

**Steps:**

1. **Create a Personal Access Token (PAT)**
   ```bash
   # Go to: https://github.com/settings/tokens
   # Click: Generate new token (classic)
   # Name: "Petrosa K8s Deployment Access"
   # Scopes: Select "repo" (Full control of private repositories)
   # Expiration: 90 days or No expiration
   # Generate token and COPY IT
   ```

2. **Add Token to Repository Secrets**
   ```bash
   # Go to: https://github.com/PetroSa2/petrosa-data-manager/settings/secrets/actions
   # Click: New repository secret
   # Name: PETROSA_K8S_ACCESS_TOKEN
   # Value: [paste your PAT]
   # Add secret
   ```

3. **Update Deploy Workflow**
   
   Edit `.github/workflows/deploy.yml` around line 64:
   
   ```yaml
   # BEFORE:
   - name: Checkout petrosa_k8s repository
     uses: actions/checkout@v4
     with:
       repository: PetroSa2/petrosa_k8s
       path: petrosa_k8s
   
   # AFTER:
   - name: Checkout petrosa_k8s repository
     uses: actions/checkout@v4
     with:
       repository: PetroSa2/petrosa_k8s
       path: petrosa_k8s
       token: ${{ secrets.PETROSA_K8S_ACCESS_TOKEN }}  # Add this line
   ```

4. **Commit and Push**
   ```bash
   git add .github/workflows/deploy.yml
   git commit -m "fix: add PAT for petrosa_k8s repository access in deploy workflow"
   git push origin main
   ```

### Option 2: GitHub App (More secure, more complex)

Create a GitHub App with repository access permissions (recommended for production long-term).

### Option 3: Make Repository Public (Not recommended)

Make `petrosa_k8s` public (security risk if it contains sensitive configs).

---

## Verification

After implementing Option 1:

```bash
# Make a small change and push
git commit --allow-empty -m "test: verify deploy workflow"
git push origin main

# Watch the deploy workflow
gh run watch

# Should see:
# ✓ Create Release
# ✓ Build & Push
# ✓ Deploy to Kubernetes ← Should now succeed
# ✓ cleanup
# ✓ notify
```

---

## Current Workaround (What We're Doing)

**Manual Deployment Process:**
1. ✅ Code changes committed to Git
2. ✅ CI Pipeline runs and passes (lint, test, security)
3. ✅ Docker images built and pushed
4. ✅ Manual deployment: `kubectl set image deployment/...`
5. ✅ Verify deployment: Check pods and logs

**This works but:**
- Requires manual intervention
- Bypasses automated deployment verification
- No automatic rollback on failure

---

## What's Working vs What Needs Fix

### ✅ Working (No action needed)
- CI Pipeline (lint, test, security)
- Docker image building
- Docker image pushing
- Release creation
- Manual Kubernetes deployment

### ❌ Needs Manual Fix
- Automated Kubernetes deployment via GitHub Actions
- Requires: PAT or GitHub App setup

---

## Impact

**Current State:**
- ✅ Production is running correctly (v1.2.1)
- ✅ All functionality working
- ✅ CI ensures code quality
- ⚠️ Deployment requires manual kubectl commands

**After Fix:**
- ✅ Full CI/CD automation
- ✅ Push to main → auto-deploy
- ✅ Rollback capability
- ✅ Deployment verification

---

## Recommended Action

**Priority: Medium**  
**Effort: 5 minutes**  
**Impact: Improved workflow**

Implement **Option 1 (PAT)** for quick fix, then consider GitHub App for long-term security.

---

## Files to Modify

1. **GitHub Repository Secrets** (via web UI)
   - Add: `PETROSA_K8S_ACCESS_TOKEN`

2. **`.github/workflows/deploy.yml`** (line ~64)
   ```yaml
   token: ${{ secrets.PETROSA_K8S_ACCESS_TOKEN }}
   ```

That's it! Two small changes for full CI/CD automation.

---

## Current CI/CD Status

```
✅ CI Pipeline: PASSING
✅ Build & Push: PASSING  
✅ Security Scan: PASSING
✅ Code Quality: PASSING
❌ Auto-Deploy: FAILING (needs PAT)

Workaround: Manual deployment (working fine)
```

---

## Notes

- The `GITHUB_TOKEN` provided by GitHub Actions has limited permissions
- It can access the current repository but not other repos in the organization
- This is a security feature to prevent unauthorized access
- PAT or GitHub App is the standard solution for cross-repo access

