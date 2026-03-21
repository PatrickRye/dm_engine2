"""
Platform-agnostic repository client.

Set REPO_PLATFORM=github (default) or REPO_PLATFORM=gitlab to select the backend.

GitHub auth:  GITHUB_PAT + GITHUB_REPO (or GITHUB_REPOSITORY, auto-injected by GitHub Actions)
GitLab auth:  GITLAB_TOKEN + CI_PROJECT_PATH (auto-injected by GitLab CI, or set GITLAB_PROJECT manually)
              GITLAB_URL defaults to https://gitlab.com

All methods use a unified dict schema so agents work identically on both platforms.
"""
import os
from typing import Optional

PLATFORM = os.environ.get("REPO_PLATFORM", "github").lower()
_DEFAULT_BRANCH_ENV = os.environ.get("CI_DEFAULT_BRANCH", os.environ.get("DEFAULT_BRANCH", "main"))


class RepoClient:
    """Common interface for GitHub and GitLab repo operations."""

    # --- Issues ---
    def get_issue(self, number: int) -> dict:
        raise NotImplementedError

    def update_issue(self, number: int, body: str = None, title: str = None):
        raise NotImplementedError

    def add_labels(self, issue_number: int, labels: list[str]):
        """Appends labels to an issue without removing existing ones."""
        raise NotImplementedError

    def set_status_label(self, issue_number: int, new_status: str):
        """Replaces all status:* labels with new_status, preserving every other label."""
        raise NotImplementedError

    def post_comment(self, issue_number: int, body: str):
        raise NotImplementedError

    def list_issues(self, state: str = "open", labels: list[str] = None) -> list[dict]:
        raise NotImplementedError

    # --- Branches / MRs / PRs ---
    def create_branch(self, name: str, from_ref: str = None):
        raise NotImplementedError

    def list_open_mrs(self, source_branch: str = None) -> list[dict]:
        """Returns open pull/merge requests, optionally filtered by source branch."""
        raise NotImplementedError

    def get_mr_diff_files(self, mr_number: int) -> list[str]:
        """Returns a list of file paths modified by a PR/MR."""
        raise NotImplementedError

    def get_mr(self, mr_number: int) -> dict:
        """Returns PR/MR metadata and unified diff text (capped at 20 files)."""
        raise NotImplementedError

    def merge_mr(self, mr_number: int, message: str = "") -> bool:
        raise NotImplementedError

    def get_file_contents(self, filepath: str, branch: str = None) -> str:
        """Reads a file from the repository at the specified branch (defaults to default branch)."""
        raise NotImplementedError

    def create_mr(self, title: str, body: str, head: str, base: str = None) -> dict:
        """Creates a Pull Request / Merge Request. Returns {number, url}."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# GitHub implementation
# ---------------------------------------------------------------------------

class GitHubRepoClient(RepoClient):
    def __init__(self):
        from github import Auth, Github
        token = os.environ.get("GITHUB_PAT")
        repo_name = os.environ.get("GITHUB_REPO", os.environ.get("GITHUB_REPOSITORY"))
        if not token or not repo_name:
            raise ValueError("GITHUB_PAT and GITHUB_REPO/GITHUB_REPOSITORY env vars are required.")
        self._repo = Github(auth=Auth.Token(token)).get_repo(repo_name)

    def get_issue(self, number: int) -> dict:
        issue = self._repo.get_issue(number=number)
        return {
            "number": issue.number,
            "title": issue.title,
            "body": issue.body or "",
            "labels": [l.name for l in issue.labels],
        }

    def update_issue(self, number: int, body: str = None, title: str = None):
        issue = self._repo.get_issue(number=number)
        kwargs = {}
        if body is not None:
            kwargs["body"] = body
        if title is not None:
            kwargs["title"] = title
        if kwargs:
            issue.edit(**kwargs)

    def add_labels(self, issue_number: int, labels: list[str]):
        self._repo.get_issue(number=issue_number).add_to_labels(*labels)

    def set_status_label(self, issue_number: int, new_status: str):
        issue = self._repo.get_issue(number=issue_number)
        current = [l.name for l in issue.labels if not l.name.startswith("status:")]
        issue.set_labels(*(current + [new_status]))

    def post_comment(self, issue_number: int, body: str):
        self._repo.get_issue(number=issue_number).create_comment(body)

    def list_issues(self, state: str = "open", labels: list[str] = None) -> list[dict]:
        kwargs = {"state": state}
        if labels:
            kwargs["labels"] = labels
        return [
            {"number": i.number, "title": i.title, "body": i.body or ""}
            for i in self._repo.get_issues(**kwargs)[:20]
        ]

    def create_branch(self, name: str, from_ref: str = None):
        base = from_ref or _DEFAULT_BRANCH_ENV
        sha = self._repo.get_branch(base).commit.sha
        self._repo.create_git_ref(ref=f"refs/heads/{name}", sha=sha)

    def list_open_mrs(self, source_branch: str = None) -> list[dict]:
        kwargs = {"state": "open"}
        if source_branch:
            kwargs["head"] = f"{self._repo.owner.login}:{source_branch}"
        return [
            {"number": pr.number, "title": pr.title, "body": pr.body or "", "head": pr.head.ref}
            for pr in self._repo.get_pulls(**kwargs)
        ]

    def get_mr_diff_files(self, mr_number: int) -> list[str]:
        return [f.filename for f in self._repo.get_pull(mr_number).get_files()]

    def get_mr(self, mr_number: int) -> dict:
        pr = self._repo.get_pull(mr_number)
        diff_lines = []
        for f in pr.get_files():
            diff_lines.append(f"--- {f.filename} (+{f.additions} -{f.deletions})")
            if f.patch:
                diff_lines.append(f.patch[:2000])  # cap per-file patch to prevent token explosion
        return {
            "number": pr.number,
            "title": pr.title,
            "body": pr.body or "",
            "head": pr.head.ref,
            "diff": "\n".join(diff_lines),
            "mergeable": pr.mergeable,
        }

    def merge_mr(self, mr_number: int, message: str = "") -> bool:
        pr = self._repo.get_pull(mr_number)
        result = pr.merge(commit_message=message or f"Merge PR #{mr_number} via AI Reviewer")
        return result.merged

    def get_file_contents(self, filepath: str, branch: str = None) -> str:
        ref = branch or _DEFAULT_BRANCH_ENV
        try:
            content = self._repo.get_contents(filepath, ref=ref)
            return content.decoded_content.decode("utf-8")
        except Exception as e:
            return f"Error reading {filepath}@{ref}: {e}"

    def create_mr(self, title: str, body: str, head: str, base: str = None) -> dict:
        base_branch = base or _DEFAULT_BRANCH_ENV
        pr = self._repo.create_pull(title=title, body=body, head=head, base=base_branch)
        return {"number": pr.number, "url": pr.html_url}


# ---------------------------------------------------------------------------
# GitLab implementation
# ---------------------------------------------------------------------------

class GitLabRepoClient(RepoClient):
    def __init__(self):
        import gitlab
        token = os.environ.get("GITLAB_TOKEN")
        project_path = os.environ.get("CI_PROJECT_PATH", os.environ.get("GITLAB_PROJECT"))
        gitlab_url = os.environ.get("GITLAB_URL", "https://gitlab.com")
        if not token or not project_path:
            raise ValueError("GITLAB_TOKEN and CI_PROJECT_PATH/GITLAB_PROJECT env vars are required.")
        gl = gitlab.Gitlab(gitlab_url, private_token=token)
        gl.auth()
        # GitLab get_project accepts both numeric id and "namespace/project" path
        self._project = gl.projects.get(project_path)

    def get_issue(self, number: int) -> dict:
        issue = self._project.issues.get(number)
        return {
            "number": issue.iid,
            "title": issue.title,
            "body": issue.description or "",
            "labels": issue.labels,
        }

    def update_issue(self, number: int, body: str = None, title: str = None):
        issue = self._project.issues.get(number)
        if body is not None:
            issue.description = body
        if title is not None:
            issue.title = title
        issue.save()

    def add_labels(self, issue_number: int, labels: list[str]):
        issue = self._project.issues.get(issue_number)
        issue.labels = list(set(issue.labels + labels))
        issue.save()

    def set_status_label(self, issue_number: int, new_status: str):
        issue = self._project.issues.get(issue_number)
        current = [l for l in issue.labels if not l.startswith("status:")]
        issue.labels = current + [new_status]
        issue.save()

    def post_comment(self, issue_number: int, body: str):
        self._project.issues.get(issue_number).notes.create({"body": body})

    def list_issues(self, state: str = "open", labels: list[str] = None) -> list[dict]:
        kwargs = {"state": "opened" if state == "open" else state, "per_page": 20}
        if labels:
            kwargs["labels"] = labels
        return [
            {"number": i.iid, "title": i.title, "body": i.description or ""}
            for i in self._project.issues.list(**kwargs)
        ]

    def create_branch(self, name: str, from_ref: str = None):
        base = from_ref or _DEFAULT_BRANCH_ENV
        self._project.branches.create({"branch": name, "ref": base})

    def list_open_mrs(self, source_branch: str = None) -> list[dict]:
        kwargs = {"state": "opened"}
        if source_branch:
            kwargs["source_branch"] = source_branch
        return [
            {"number": mr.iid, "title": mr.title, "body": mr.description or "", "head": mr.source_branch}
            for mr in self._project.mergerequests.list(**kwargs)
        ]

    def get_mr_diff_files(self, mr_number: int) -> list[str]:
        mr = self._project.mergerequests.get(mr_number)
        files = set()
        for change in mr.changes().get("changes", []):
            files.add(change.get("new_path") or change.get("old_path", ""))
        return list(files)

    def get_mr(self, mr_number: int) -> dict:
        mr = self._project.mergerequests.get(mr_number)
        changes = mr.changes().get("changes", [])
        diff_lines = []
        for c in changes[:20]:  # cap at 20 files
            diff_lines.append(f"--- {c.get('new_path', c.get('old_path', '?'))} (+{c.get('a_mode', '?')})")
            diff_lines.append(c.get("diff", "")[:2000])
        return {
            "number": mr.iid,
            "title": mr.title,
            "body": mr.description or "",
            "head": mr.source_branch,
            "diff": "\n".join(diff_lines),
            "mergeable": getattr(mr, "merge_status", "unknown"),
        }

    def merge_mr(self, mr_number: int, message: str = "") -> bool:
        mr = self._project.mergerequests.get(mr_number)
        mr.merge(merge_commit_message=message or f"Merge MR !{mr_number} via AI Reviewer")
        return True

    def get_file_contents(self, filepath: str, branch: str = None) -> str:
        ref = branch or _DEFAULT_BRANCH_ENV
        try:
            f = self._project.files.get(file_path=filepath, ref=ref)
            return f.decode().decode("utf-8")
        except Exception as e:
            return f"Error reading {filepath}@{ref}: {e}"

    def create_mr(self, title: str, body: str, head: str, base: str = None) -> dict:
        base_branch = base or _DEFAULT_BRANCH_ENV
        mr = self._project.mergerequests.create({
            "title": title,
            "description": body,
            "source_branch": head,
            "target_branch": base_branch,
        })
        return {"number": mr.iid, "url": mr.web_url}


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------

def get_repo_client() -> RepoClient:
    """Returns the platform-appropriate RepoClient (controlled by REPO_PLATFORM env var)."""
    if PLATFORM == "gitlab":
        return GitLabRepoClient()
    return GitHubRepoClient()
