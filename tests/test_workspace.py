"""Tests for disposable Git workspaces."""

from pathlib import Path

import pytest
from git import Actor, Repo

from agentcompat.workspace import (
    WorkspaceDiff,
    WorkspaceManager,
    is_disposable_workspace,
)


@pytest.fixture
def source_repository(tmp_path: Path) -> Path:
    """Create a committed source repository without relying on global Git config."""
    repository_path = tmp_path / "source"
    repository_path.mkdir()
    (repository_path / "modified.txt").write_text("original\n", encoding="utf-8")
    (repository_path / "deleted.txt").write_text("keep me\n", encoding="utf-8")

    repository = Repo.init(repository_path)
    repository.index.add(["modified.txt", "deleted.txt"])
    author = Actor("AgentCompat Tests", "tests@agentcompat.local")
    repository.index.commit("Initial commit", author=author, committer=author)
    repository.close()
    return repository_path


def test_manager_creates_separate_workspaces_from_same_commit(
    source_repository: Path,
) -> None:
    source = Repo(source_repository)
    source_status = source.git.status("--porcelain")
    source_sha = source.head.commit.hexsha
    source.close()

    manager = WorkspaceManager(source_repository)
    baseline = manager.create_workspace()
    candidate = manager.create()

    try:
        assert baseline.workspace_path != candidate.workspace_path
        assert baseline.workspace_path.is_dir()
        assert candidate.workspace_path.is_dir()
        assert is_disposable_workspace(baseline.workspace_path)
        assert is_disposable_workspace(candidate.workspace_path)
        assert not is_disposable_workspace(source_repository)
        baseline_repository = Repo(baseline.workspace_path)
        candidate_repository = Repo(candidate.workspace_path)
        assert list(baseline_repository.remotes) == []
        assert list(candidate_repository.remotes) == []
        baseline_repository.close()
        candidate_repository.close()
        assert baseline.original_commit_sha == source_sha
        assert candidate.original_commit_sha == source_sha
        assert manager.original_commit_sha == source_sha

        (baseline.workspace_path / "modified.txt").write_text(
            "baseline change\n",
            encoding="utf-8",
        )

        assert (candidate.workspace_path / "modified.txt").read_text(
            encoding="utf-8"
        ) == "original\n"
        assert (source_repository / "modified.txt").read_text(
            encoding="utf-8"
        ) == "original\n"

        source = Repo(source_repository)
        assert source.head.commit.hexsha == source_sha
        assert source.git.status("--porcelain") == source_status
        source.close()
    finally:
        baseline.cleanup()
        candidate.cleanup()


def test_workspace_reports_modified_added_and_deleted_files(
    source_repository: Path,
) -> None:
    manager = WorkspaceManager(source_repository)

    with manager.create_workspace() as workspace:
        (workspace.workspace_path / "modified.txt").write_text(
            "changed\n",
            encoding="utf-8",
        )
        (workspace.workspace_path / "added.txt").write_text(
            "new file\n",
            encoding="utf-8",
        )
        (workspace.workspace_path / "staged-added.txt").write_text(
            "staged file\n",
            encoding="utf-8",
        )
        (workspace.workspace_path / "deleted.txt").unlink()

        repository = Repo(workspace.workspace_path)
        repository.index.add(["modified.txt", "staged-added.txt"])
        repository.close()

        diff = workspace.get_diff()

        assert diff == WorkspaceDiff(
            changed_files=["modified.txt"],
            added_files=["added.txt", "staged-added.txt"],
            deleted_files=["deleted.txt"],
        )
        assert workspace.changed_files == ["modified.txt"]
        assert workspace.added_files == ["added.txt", "staged-added.txt"]
        assert workspace.deleted_files == ["deleted.txt"]


def test_cleanup_is_idempotent_and_removes_workspace(
    source_repository: Path,
) -> None:
    workspace = WorkspaceManager(source_repository).create_workspace()
    workspace_path = workspace.workspace_path

    workspace.cleanup()
    workspace.cleanup()

    assert not workspace_path.exists()
    with pytest.raises(RuntimeError, match="already been cleaned up"):
        workspace.get_diff()


def test_context_manager_cleans_up_workspace(source_repository: Path) -> None:
    with WorkspaceManager(source_repository).create_workspace() as workspace:
        workspace_path = workspace.workspace_path
        assert workspace_path.exists()

    assert not workspace_path.exists()


def test_manager_rejects_non_repository(tmp_path: Path) -> None:
    non_repository = tmp_path / "not-a-repository"
    non_repository.mkdir()

    with pytest.raises(ValueError, match="not a Git repository"):
        WorkspaceManager(non_repository)


def test_manager_rejects_repository_without_commits(tmp_path: Path) -> None:
    empty_repository = tmp_path / "empty-repository"
    Repo.init(empty_repository).close()

    with pytest.raises(ValueError, match="at least one commit"):
        WorkspaceManager(empty_repository)
