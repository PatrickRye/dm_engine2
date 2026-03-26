import os
import sys
import re

from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent

from dotenv import dotenv_values

# Load non-default env variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(env_path):
    defaults = {"your_gemini_api_key_here", "github_pat_your_token_here", "owner/repo_name"}
    for k, v in dotenv_values(env_path).items():
        if v and v not in defaults:
            os.environ[k] = v

from repo_client import get_repo_client


@tool
def check_open_prs_for_files(files: list[str]) -> str:
    """Checks open PRs/MRs to see if the specified files are currently being modified. Use this to prevent merge conflicts."""
    try:
        client = get_repo_client()
        open_mrs = client.list_open_mrs()
        conflicts = []
        for mr in open_mrs:
            mr_files = client.get_mr_diff_files(mr["number"])
            for target_file in files:
                if target_file in mr_files:
                    conflicts.append(f"File '{target_file}' is currently locked by PR #{mr['number']} ({mr['title']}).")
        return "\n".join(conflicts) if conflicts else "All requested files are free and unlocked."
    except Exception as e:
        return f"Error checking PRs: {e}"


@tool
def create_branch(branch_name: str) -> str:
    """Creates a new branch from the default branch."""
    try:
        get_repo_client().create_branch(branch_name)
        return f"Successfully created branch '{branch_name}'."
    except Exception as e:
        return f"Error creating branch: {e}"


@tool
def update_issue_and_assign(issue_number: int, new_status_label: str, comment: str, new_body: str = None) -> str:
    """Updates the issue's status label, optionally updates the body requirements, and adds a comment."""
    try:
        client = get_repo_client()
        client.set_status_label(issue_number, new_status_label)
        if new_body:
            client.update_issue(issue_number, body=new_body)
        if comment:
            client.post_comment(issue_number, comment)
        return f"Successfully updated issue #{issue_number} to {new_status_label} and added comment."
    except Exception as e:
        return f"Error updating issue: {e}"


PLANNER_PROMPT = """
Role: You are the Lead Architect and Planner Agent.
Your job is to schedule development tasks, refine requirements, create Git branches, and prevent concurrent file modification conflicts.

Project Architecture & Context:
{PROJECT_DESIGN}

Allowed Actions:
1. Use `check_open_prs_for_files` to ensure target files aren't already being modified in other PRs.
2. Use `create_branch` to spawn a new feature branch (e.g., `feature/ISSUE-123`).
3. Use `update_issue_and_assign` to set the label `status: selected`, optionally rewrite the issue body to refine requirements and add `## Implementer Instructions`, and notify the Implementer via comment.

Execution Rules:
- Analyze the issue title and body. Cross-reference it with the Project Architecture.
- If the requirements are vague, flawed, or missing test criteria, rewrite the issue body to include strict, actionable requirements.
- Append a `## Implementer Instructions` section to the issue body. In this section, act as a Senior Engineering Manager: define the exact design patterns, files to touch, and testing expectations for the Implementer based on the domain.
- Check if those files are currently being modified in any open Pull Requests.
- If the files are locked (in an open PR): DO NOT create a branch. Use `update_issue_and_assign` to leave a comment explaining the block, and leave the label as `status: backlog`.
- If the files are free:
  - Create a new Git branch named `feature/ISSUE-<number>`.
  - Use `update_issue_and_assign` to update the body (with refined requirements and instructions), add the `status: selected` label, and leave a comment instructing the Implementer to begin.
"""


def main():
    issue_number_str = os.environ.get("ISSUE_NUMBER")
    if not issue_number_str:
        print("No ISSUE_NUMBER provided. Exiting.")
        sys.exit(1)

    issue_number = int(issue_number_str)
    issue_title = os.environ.get("ISSUE_TITLE", "Unknown Title")
    issue_body = os.environ.get("ISSUE_BODY", "No body provided.")

    design_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "project_design.md")
    project_design_content = "Project design document not found."
    if os.path.exists(design_path):
        with open(design_path, "r", encoding="utf-8") as f:
            project_design_content = f.read()

    print(f"Planning Issue #{issue_number}: {issue_title}")

    # ------------------------------------------------------------------
    # Deterministic escape hatch: if implementer instructions already
    # exist, skip LLM and execute the plan directly.
    # ------------------------------------------------------------------
    if "## Implementer Instructions" in issue_body or "## Implementer Instructions" in issue_title:
        print("[Deterministic plan] Issue already has Implementer Instructions — skipping LLM.")

        # Extract branch name
        branch_name = f"feature/ISSUE-{issue_number}"
        print(f"[Deterministic plan] Creating branch: {branch_name}")
        create_branch.invoke({"branch_name": branch_name})

        # Extract any file mentions to check for conflicts
        file_patterns = re.findall(r'`([^`]+)`', issue_body)
        if file_patterns:
            conflict_result = check_open_prs_for_files.invoke({"files": file_patterns})
            print(f"[Deterministic plan] Conflict check: {conflict_result}")
            if "free" not in conflict_result.lower():
                # Files are locked — just update status and exit
                update_issue_and_assign.invoke({
                    "issue_number": issue_number,
                    "new_status_label": "status: backlog",
                    "comment": f"Cannot create branch — files are locked in an open PR: {conflict_result}",
                })
                print("[Deterministic plan] Files locked. Status set to backlog.")
                return

        # Update issue: set to selected, add comment
        comment = (
            f"Branch `{branch_name}` created. "
            "Issue already contains ## Implementer Instructions — implementation may begin."
        )
        update_issue_and_assign.invoke({
            "issue_number": issue_number,
            "new_status_label": "status: selected",
            "comment": comment,
        })
        print(f"[Deterministic plan] Branch created. Issue #{issue_number} set to status: selected.")
        return

    # Fall back to LLM for issues needing requirements refinement
    print("[LLM plan] No Implementer Instructions — using LLM to refine and plan.")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.1)
    agent = create_react_agent(llm, [check_open_prs_for_files, create_branch, update_issue_and_assign])

    human_msg = f"Please plan this issue.\nIssue #{issue_number}\nTITLE: {issue_title}\n\nBODY:\n{issue_body}"
    state = {
        "messages": [
            SystemMessage(content=PLANNER_PROMPT.replace("{PROJECT_DESIGN}", project_design_content)),
            HumanMessage(content=human_msg),
        ]
    }
    agent.invoke(state, {"recursion_limit": 15})


if __name__ == "__main__":
    main()
