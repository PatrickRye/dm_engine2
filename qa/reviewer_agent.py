import os
import sys

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
def get_pr_by_branch(branch_name: str) -> str:
    """Finds the open PR/MR for a branch and returns its diff, body, and linked issue number."""
    try:
        client = get_repo_client()
        mrs = client.list_open_mrs(source_branch=branch_name)
        if not mrs:
            return f"No open PR found for branch {branch_name}."
        mr_detail = client.get_mr(mrs[0]["number"])
        issue_num = branch_name.split("ISSUE-")[1] if "ISSUE-" in branch_name else "Unknown"
        return (
            f"PR Number: {mr_detail['number']}\n"
            f"Title: {mr_detail['title']}\n"
            f"Body: {mr_detail['body']}\n"
            f"Linked Issue: #{issue_num}\n"
            f"Mergeable: {mr_detail['mergeable']}\n\n"
            f"Diff:\n{mr_detail['diff']}"
        )
    except Exception as e:
        return f"Error finding PR: {e}"


@tool
def get_file_from_branch(filepath: str, branch: str) -> str:
    """Reads the contents of a file from a specific branch for deeper review context."""
    try:
        return get_repo_client().get_file_contents(filepath, branch=branch)
    except Exception as e:
        return f"Error reading file: {e}"


@tool
def merge_pull_request(mr_number: int, commit_message: str = "") -> str:
    """Merges an approved pull request into the default branch."""
    try:
        success = get_repo_client().merge_mr(mr_number, message=commit_message)
        return f"PR #{mr_number} merged successfully." if success else f"Failed to merge PR #{mr_number}."
    except Exception as e:
        return f"Error merging PR: {e}"


@tool
def update_issue_status(issue_number: int, new_label: str, comment: str = "") -> str:
    """Updates the status label on the original issue and adds an optional comment."""
    try:
        client = get_repo_client()
        client.set_status_label(issue_number, new_label)
        if comment:
            client.post_comment(issue_number, comment)
        return f"Issue #{issue_number} updated to '{new_label}'."
    except Exception as e:
        return f"Error updating issue: {e}"


REVIEWER_PROMPT = """
Role: You are the Reviewer Agent, acting as a strict Senior Software Engineer. Your job is to audit Pull Requests, validate CI test results, and merge compliant code into the main branch.
You trust the Triager and Planner for scoping and categorizing; your focus is purely on execution completeness, consistency, and code quality.

Allowed Actions:
1. Use `get_pr_by_branch` to find the PR number, linked issue, and full diff for the tested branch.
2. Use `get_file_from_branch` to read specific files from the PR branch if you need deeper context.
3. Use `merge_pull_request` to merge an approved PR.
4. Use `update_issue_status` to update the original issue label and leave feedback comments.

Execution Rules:
- You are reviewing the branch `{TARGET_BRANCH}`. The automated test suite result is: `{TEST_CONCLUSION}`.
- If `TEST_CONCLUSION` is 'failure':
  * DO NOT merge.
  * Use `update_issue_status` to add a comment detailing the test failure, and change the label to `status: backlog`.
- If `TEST_CONCLUSION` is 'success':
  * Use `get_pr_by_branch` to read the diff.
  * Verify the task requirements (as defined in the linked issue) were completely and consistently executed.
  * Verify the code strictly aligns with the requirements without scope creep.
  * Enforce clean code, good software architecture, and extensible design patterns.
  * Verify that the test suite was updated consistently with the new requirements/logic.
  * If approved, use `merge_pull_request`. Then use `update_issue_status` to change the issue label to `status: archived`.
  * If unapproved, reject the merge and use `update_issue_status` to send it back to `status: backlog` with detailed rejection feedback explaining the missing tests, poor architecture, or incomplete execution.
"""


def main():
    target_branch = os.environ.get("TARGET_BRANCH", "")
    test_conclusion = os.environ.get("TEST_CONCLUSION", "unknown")
    if not target_branch:
        print("No TARGET_BRANCH provided. Exiting.")
        sys.exit(1)

    print(f"Reviewing Branch: {target_branch} (Tests: {test_conclusion})")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.0)
    tools = [get_pr_by_branch, get_file_from_branch, merge_pull_request, update_issue_status]
    prompt = REVIEWER_PROMPT.replace("{TARGET_BRANCH}", target_branch).replace("{TEST_CONCLUSION}", test_conclusion)

    agent = create_react_agent(llm, tools)
    agent.invoke(
        {"messages": [SystemMessage(content=prompt), HumanMessage(content="Begin your review process.")]},
        {"recursion_limit": 20},
    )


if __name__ == "__main__":
    main()
