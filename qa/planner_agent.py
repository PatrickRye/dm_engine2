import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from github import Auth, Github

def _get_repo():
    token = os.environ.get("GITHUB_PAT")
    repo_name = os.environ.get("GITHUB_REPO")
    if not token or not repo_name:
        raise ValueError("GITHUB_PAT or GITHUB_REPO env variables are missing.")
    auth = Auth.Token(token)
    g = Github(auth=auth)
    return g.get_repo(repo_name)

@tool
def check_open_prs_for_files(files: list[str]) -> str:
    """Checks open PRs to see if the specified files are currently being modified. Use this to prevent merge conflicts."""
    try:
        repo = _get_repo()
        open_prs = repo.get_pulls(state='open')
        conflicts = []
        for pr in open_prs:
            pr_files = [f.filename for f in pr.get_files()]
            for target_file in files:
                if target_file in pr_files:
                    conflicts.append(f"File '{target_file}' is currently locked by PR #{pr.number} ({pr.title}).")
        if conflicts:
            return "\n".join(conflicts)
        return "All requested files are free and unlocked."
    except Exception as e:
        return f"Error checking PRs: {e}"

@tool
def create_github_branch(branch_name: str) -> str:
    """Creates a new branch from the main branch."""
    try:
        repo = _get_repo()
        main_branch = repo.get_branch("main")
        repo.create_git_ref(ref=f"refs/heads/{branch_name}", sha=main_branch.commit.sha)
        return f"Successfully created branch '{branch_name}'."
    except Exception as e:
        return f"Error creating branch: {e}"

@tool
def update_issue_and_assign(issue_number: int, new_status_label: str, comment: str) -> str:
    """Updates the issue's status label and adds a comment."""
    try:
        repo = _get_repo()
        issue = repo.get_issue(number=issue_number)
        current_labels = [l.name for l in issue.labels if not l.name.startswith("status:")]
        issue.set_labels(*(current_labels + [new_status_label]))
        if comment:
            issue.create_comment(comment)
        return f"Successfully updated issue #{issue_number} to {new_status_label} and added comment."
    except Exception as e:
        return f"Error updating issue: {e}"

PLANNER_PROMPT = """
Role: You are the Planner Agent for a Python D&D engine.
Your job is to schedule development tasks, create Git branches, and prevent concurrent file modification conflicts.

Allowed Actions:
1. Use `check_open_prs_for_files` to ensure target files aren't already being modified in other PRs.
2. Use `create_github_branch` to spawn a new feature branch (e.g., `feature/ISSUE-123`).
3. Use `update_issue_and_assign` to set the label `status: selected` and notify the Implementer.

Execution Rules:
- Analyze the issue title and body to determine which Python files likely need modification.
- Check if those files are currently being modified in any open Pull Requests.
- If the files are locked (in an open PR): DO NOT create a branch. Use `update_issue_and_assign` to leave a comment explaining the block, and leave the label as `status: backlog`.
- If the files are free:
  - Create a new Git branch named `feature/ISSUE-<number>`.
  - Use `update_issue_and_assign` to add the `status: selected` label. Provide a comment instructing the Implementer to use the new branch, outlining which files they should focus on.
"""

def main():
    issue_number_str = os.environ.get("ISSUE_NUMBER")
    if not issue_number_str:
        print("No ISSUE_NUMBER provided. Exiting.")
        return
        
    issue_number = int(issue_number_str)
    issue_title = os.environ.get("ISSUE_TITLE", "Unknown Title")
    issue_body = os.environ.get("ISSUE_BODY", "No body provided.")
    
    print(f"Planning Issue #{issue_number}: {issue_title}")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.1)
    agent = create_react_agent(llm, [check_open_prs_for_files, create_github_branch, update_issue_and_assign])
    
    human_msg = f"Please plan this issue.\nIssue #{issue_number}\nTITLE: {issue_title}\n\nBODY:\n{issue_body}"
    state = {"messages": [SystemMessage(content=PLANNER_PROMPT), HumanMessage(content=human_msg)]}
    agent.invoke(state)

if __name__ == "__main__":
    main()