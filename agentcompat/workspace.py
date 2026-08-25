"""Disposable Git workspaces for isolated agent execution."""

from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from types import TracebackType

from git import Repo
from git.exc import BadName, InvalidGitRepositoryError, NoSuchPathError

WORKSPACE_MARKER_FILENAME = "agentcompat-workspace"


def is_disposable_workspace(workspace: Path) -> bool:
    """Return whether a path was created by the AgentCompat workspace manager."""
    resolved_workspace = workspace.resolve()
    try:
        repository = Repo(resolved_workspace, search_parent_directories=False)
    except (InvalidGitRepositoryError, NoSuchPathError):
        return False

    try:
        working_tree = repository.working_tree_dir
        if repository.bare or working_tree is None:
            return False
        if Path(working_tree).resolve() != resolved_workspace:
            return False
        marker = Path(repository.git_dir) / WORKSPACE_MARKER_FILENAME
        return marker.is_file()
    finally:
        repository.close()


@dataclass(slots=True)
class WorkspaceDiff:
    """Repository changes made inside a disposable workspace."""

    changed_files: list[str]
    added_files: list[str]
    deleted_files: list[str]
    ignored_files: list[str] = field(default_factory=list)


class DisposableWorkspace:
    """An isolated, temporary clone for one agent run."""

    def __init__(
        self,
        workspace_path: Path,
        original_commit_sha: str,
        repository: Repo,
        temporary_directory: TemporaryDirectory[str],
    ) -> None:
        self.workspace_path = workspace_path
        self.original_commit_sha = original_commit_sha
        self._repository = repository
        self._temporary_directory = temporary_directory
        self._cleaned_up = False

    def __enter__(self) -> "DisposableWorkspace":
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.cleanup()

    def _active_repository(self) -> Repo:
        if self._cleaned_up:
            raise RuntimeError("workspace has already been cleaned up")
        return self._repository

    def get_diff(self) -> WorkspaceDiff:
        """Return changes relative to the workspace's immutable starting commit."""
        repository = self._active_repository()
        changed_files: set[str] = set()
        added_files: set[str] = set(repository.untracked_files)
        deleted_files: set[str] = set()

        ignored_output = repository.git.ls_files(
            "--others",
            "--ignored",
            "--exclude-standard",
            "-z",
        )
        ignored_files = {path for path in ignored_output.split("\0") if path}
        added_files.difference_update(ignored_files)

        original_commit = repository.commit(self.original_commit_sha)
        for item in original_commit.diff(None):
            if item.renamed_file:
                if item.a_path is not None:
                    deleted_files.add(item.a_path)
                if item.b_path is not None:
                    added_files.add(item.b_path)
            elif item.new_file:
                path = item.b_path or item.a_path
                if path is not None:
                    added_files.add(path)
            elif item.deleted_file:
                path = item.a_path or item.b_path
                if path is not None:
                    deleted_files.add(path)
            else:
                path = item.b_path or item.a_path
                if path is not None:
                    changed_files.add(path)

        changed_files.difference_update(added_files | deleted_files)
        return WorkspaceDiff(
            changed_files=sorted(changed_files),
            added_files=sorted(added_files),
            deleted_files=sorted(deleted_files),
            ignored_files=sorted(ignored_files),
        )

    @property
    def changed_files(self) -> list[str]:
        """Return modified tracked paths."""
        return self.get_diff().changed_files

    @property
    def added_files(self) -> list[str]:
        """Return new tracked or untracked paths."""
        return self.get_diff().added_files

    @property
    def deleted_files(self) -> list[str]:
        """Return deleted tracked paths."""
        return self.get_diff().deleted_files

    @property
    def ignored_files(self) -> list[str]:
        """Return untracked paths hidden by Git ignore rules."""
        return self.get_diff().ignored_files

    def cleanup(self) -> None:
        """Close Git resources and remove the temporary workspace."""
        if self._cleaned_up:
            return
        self._repository.close()
        self._temporary_directory.cleanup()
        self._cleaned_up = True


class WorkspaceManager:
    """Create independent workspaces pinned to one source commit."""

    def __init__(self, source_repository: Path) -> None:
        self.source_repository = source_repository.resolve()
        self.original_commit_sha = self._read_original_commit()

    def _read_original_commit(self) -> str:
        try:
            repository = Repo(self.source_repository, search_parent_directories=False)
        except (InvalidGitRepositoryError, NoSuchPathError) as exc:
            raise ValueError(
                f"source repository is not a Git repository: {self.source_repository}"
            ) from exc

        try:
            if repository.bare:
                raise ValueError("source repository must have a working tree")
            try:
                return repository.head.commit.hexsha
            except (BadName, ValueError) as exc:
                raise ValueError(
                    "source repository must contain at least one commit"
                ) from exc
        finally:
            repository.close()

    def create_workspace(self) -> DisposableWorkspace:
        """Create a disposable clone checked out at the captured source commit."""
        temporary_directory = TemporaryDirectory(prefix="agentcompat-")
        workspace_path = Path(temporary_directory.name) / "repository"
        repository: Repo | None = None

        try:
            repository = Repo.clone_from(
                self.source_repository,
                workspace_path,
                no_hardlinks=True,
            )
            repository.git.checkout("--detach", self.original_commit_sha)
            for remote in list(repository.remotes):
                repository.delete_remote(remote)
            marker = Path(repository.git_dir) / WORKSPACE_MARKER_FILENAME
            marker.write_text(
                f"version=1\ncommit={self.original_commit_sha}\n",
                encoding="utf-8",
            )
        except Exception:
            if repository is not None:
                repository.close()
            temporary_directory.cleanup()
            raise

        return DisposableWorkspace(
            workspace_path=workspace_path,
            original_commit_sha=self.original_commit_sha,
            repository=repository,
            temporary_directory=temporary_directory,
        )

    def create(self) -> DisposableWorkspace:
        """Create a workspace using the manager's concise factory alias."""
        return self.create_workspace()
