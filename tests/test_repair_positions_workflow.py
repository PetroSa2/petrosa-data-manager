import re
from pathlib import Path

WORKFLOW = Path(__file__).parents[1] / ".github/workflows/repair-positions-dry-run.yml"


def test_workflow_runs_only_the_dry_run_and_uses_the_existing_mysql_secret():
    source = WORKFLOW.read_text()

    assert "--dry-run" in source
    assert "--apply" not in source
    assert "name: petrosa-sensitive-credentials" in source
    assert "key: MYSQL_URI" in source


def test_workflow_rejects_unpinned_image_input():
    image_pattern = re.compile(r"^[A-Za-z0-9._/-]+:(?!latest$)[A-Za-z0-9._-]+$")

    assert image_pattern.fullmatch("yurisa2/petrosa-data-manager:1.2.20-r453")
    assert not image_pattern.fullmatch("yurisa2/petrosa-data-manager:latest")
    assert not image_pattern.fullmatch("yurisa2/petrosa-data-manager")


def test_workflow_cleans_up_after_job_completion_or_failure():
    source = WORKFLOW.read_text()

    assert "trap cleanup EXIT" in source
    assert 'kubectl -n "$NAMESPACE" delete job "$JOB_NAME"' in source
    assert 'kubectl -n "$NAMESPACE" wait \\' in source
    assert "--for=condition=complete" in source
    assert 'kubectl -n "$NAMESPACE" logs "job/$JOB_NAME"' in source
