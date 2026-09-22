"""Regression tests for the post-#1110 GitOps deployment checkout."""

from pathlib import Path

WORKFLOW_FILES = (
    Path(__file__).parents[1] / ".github" / "workflows" / "deploy.yml",
    Path(__file__).parents[1] / ".github" / "workflows" / "manual-deploy.yml",
)


def test_gitops_workflows_link_umbrella_scripts_before_rebase():
    for workflow_path in WORKFLOW_FILES:
        workflow = workflow_path.read_text()
        checkout = workflow.index("repository: PetroSa2/petrosa\n")
        rebase = workflow.index("run: ./scripts/gitops-rebase-main.sh")

        assert checkout < rebase
        assert "path: .agent-tooling" in workflow[checkout:rebase]
        assert "clean: false" in workflow[checkout:rebase]
        assert (
            "ln -sfn ../.agent-tooling/scripts petrosa_k8s/scripts"
            in workflow[checkout:rebase]
        )
        assert (
            "test -f petrosa_k8s/scripts/gitops-rebase-main.sh"
            in workflow[checkout:rebase]
        )
