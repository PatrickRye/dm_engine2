import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from github import Auth, Github


@tool
def apply_triage_assessment(labels: list[str], comment: str = "") -> str:
    """Applies a list of labels to the current GitHub issue, and optionally posts a comment."""
    try:
        token = os.environ.get("GITHUB_PAT")
        repo_name = os.environ.get("GITHUB_REPO")
        issue_number = int(os.environ.get("ISSUE_NUMBER"))

        auth = Auth.Token(token)
        g = Github(auth=auth)
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)

        issue.add_to_labels(*labels)
        if comment:
            issue.create_comment(comment)
        return f"Successfully applied labels: {labels} and added comment."
    except Exception as e:
        return f"Error applying triage assessment: {e}"


TRIAGER_PROMPT = """
Role: You are the Triager Agent for the D&D AI system. 
Your job is to evaluate newly reported bugs/issues, assess their scale, assign priority, categorize them, and route them appropriately.

Allowed Actions:
1. Use `apply_triage_assessment` to add exact text labels and an optional comment to the issue.

Execution Rules:
- Analyze the issue title and body.
- Assess priority. Choose ONE: 'priority: critical', 'priority: high', 'priority: medium', 'priority: low'.
- Assess category. Choose ONE: 'category: story-compliance', 'category: software', 'category: rules', 'category: narrative-agency'.
- Assess scale. Determine if this task is appropriate for a single Implementer agent to complete in one pass. Choose ONE:
  * 'scale: small' (quick fix)
  * 'scale: medium' (standard feature or bug fix)
  * 'scale: large' (complex logic across multiple files)
  * 'scale: epic' (too large, requires architectural design and sub-dividing into smaller tasks)
- Route the Issue (Status):
  * If scale is small, medium, or large, add the label 'status: backlog'.
  * If scale is epic, add the label 'status: needs_architect' AND provide a detailed `comment` explaining how the task should be sub-divided or architected.
- Call `apply_triage_assessment` with your chosen list of labels and your comment (if applicable).
"""


def main():
    issue_title = os.environ.get("ISSUE_TITLE", "Unknown Title")
    issue_body = os.environ.get("ISSUE_BODY", "No body provided.")

    print(f"Triaging Issue: {issue_title}")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.2)
    agent = create_react_agent(llm, [apply_triage_assessment])
    state = {
        "messages": [
            SystemMessage(content=TRIAGER_PROMPT),
            HumanMessage(content=f"Please triage this new issue.\n\nTITLE: {issue_title}\n\nBODY:\n{issue_body}"),
        ]
    }
    agent.invoke(state)


if __name__ == "__main__":
    main()
