import os
import asyncio
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from github import Auth, Github

# MCP Imports
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from langchain_mcp_adapters.tools import load_mcp_tools

from dotenv import dotenv_values

# Load non-default env variables
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
if os.path.exists(env_path):
    defaults = {"your_gemini_api_key_here", "github_pat_your_token_here", "owner/repo_name"}
    for k, v in dotenv_values(env_path).items():
        if v and v not in defaults:
            os.environ[k] = v


def _get_repo():
    token = os.environ.get("GITHUB_PAT")
    # GitHub Actions automatically injects GITHUB_REPOSITORY
    repo_name = os.environ.get("GITHUB_REPO", os.environ.get("GITHUB_REPOSITORY"))
    if not token or not repo_name:
        raise ValueError("GITHUB_PAT or GITHUB_REPO/GITHUB_REPOSITORY env variables are missing.")
    return Github(auth=Auth.Token(token)).get_repo(repo_name)


@tool
def get_pr_by_branch(branch_name: str) -> str:
    """Finds the open Pull Request associated with a specific branch."""
    try:
        repo = _get_repo()
        # Assumes branches are pushed to the same repository
        prs = repo.get_pulls(state="open", head=f"{repo.owner.login}:{branch_name}")
        if prs.totalCount == 0:
            return f"No open PR found for branch {branch_name}."
        pr = prs[0]

        issue_num = "Unknown"
        if "ISSUE-" in branch_name:
            issue_num = branch_name.split("ISSUE-")[1]

        return f"PR Number: {pr.number}\nTitle: {pr.title}\nBody: {pr.body}\nLinked Issue: #{issue_num}"
    except Exception as e:
        return f"Error finding PR: {e}"


@tool
def update_issue_status(issue_number: int, new_label: str, comment: str = "") -> str:
    """Updates the status label on the original GitHub issue and adds an optional comment."""
    try:
        repo = _get_repo()
        issue = repo.get_issue(number=issue_number)
        current_labels = [l.name for l in issue.labels if not l.name.startswith("status:")]
        issue.set_labels(*(current_labels + [new_label]))
        if comment:
            issue.create_comment(comment)
        return f"Issue #{issue_number} updated to '{new_label}'."
    except Exception as e:
        return f"Error updating issue: {e}"


REVIEWER_PROMPT = """
Role: You are the Reviewer Agent, acting as a strict Senior Software Engineer. Your job is to audit GitHub Pull Requests, validate CI test results, and merge compliant code into the main branch. 
You trust the Triager and Planner for scoping and categorizing; your focus is purely on execution completeness, consistency, and code quality.

Allowed Actions:
1. Use `get_pr_by_branch` to find the PR number and linked issue for the tested branch.
2. Read PR diffs and resolve conflicts using GitHub MCP tools (`get_pull_request`, `get_file_contents`, `push_files`).
3. Merge or reject PRs via the MCP `merge_pull_request` tool.
4. Update the original issue using `update_issue_status`.

Execution Rules:
- You are reviewing the branch `{TARGET_BRANCH}`. The automated test suite result is: `{TEST_CONCLUSION}`.
- If `TEST_CONCLUSION` is 'failure': 
  * DO NOT merge. 
  * Use `update_issue_status` to add a comment detailing the test failure, and change the label to `status: backlog`.
- If `TEST_CONCLUSION` is 'success':
  * Use `get_pull_request` via MCP to read the diff.
  * Verify the task requirements (as defined in the linked issue) were completely and consistently executed.
  * Verify the code strictly aligns with the requirements without scope creep.
  * Enforce clean code, good software architecture, and extensible design patterns.
  * Verify that the test suite was updated consistently with the new requirements/logic.
  * If approved, use `merge_pull_request` via MCP. Then use `update_issue_status` to change the issue label to `status: archived` (which signals it's complete).
  * If unapproved, reject the merge and use `update_issue_status` to send it back to `status: backlog` with detailed rejection feedback explaining the missing tests, poor architecture, or incomplete execution.
"""


async def main():
    target_branch = os.environ.get("TARGET_BRANCH", "")
    test_conclusion = os.environ.get("TEST_CONCLUSION", "unknown")
    if not target_branch:
        print("No TARGET_BRANCH provided. Exiting.")
        return

    print(f"Reviewing Branch: {target_branch} (Tests: {test_conclusion})")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.0)
    local_tools = [get_pr_by_branch, update_issue_status]
    prompt = REVIEWER_PROMPT.replace("{TARGET_BRANCH}", target_branch).replace("{TEST_CONCLUSION}", test_conclusion)

    server_params = StdioServerParameters(
        command="npx",
        args=["-y", "@modelcontextprotocol/server-github"],
        env={**os.environ, "GITHUB_PERSONAL_ACCESS_TOKEN": os.environ.get("GITHUB_PAT", "")},
    )
    async with stdio_client(server_params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            mcp_tools = await load_mcp_tools(session)
            agent = create_react_agent(llm, local_tools + mcp_tools)
            await agent.ainvoke(
                {"messages": [SystemMessage(content=prompt), HumanMessage(content="Begin your review process.")]}
            )


if __name__ == "__main__":
    asyncio.run(main())
