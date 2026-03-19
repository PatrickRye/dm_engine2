import os
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage
from langchain_core.tools import tool
from langgraph.prebuilt import create_react_agent
from github import Auth, Github

@tool
def apply_github_labels(labels: list[str]) -> str:
    """Applies a list of labels to the current GitHub issue."""
    try:
        token = os.environ.get("GITHUB_PAT")
        repo_name = os.environ.get("GITHUB_REPO")
        issue_number = int(os.environ.get("ISSUE_NUMBER"))
        
        auth = Auth.Token(token)
        g = Github(auth=auth)
        repo = g.get_repo(repo_name)
        issue = repo.get_issue(number=issue_number)
        
        issue.add_to_labels(*labels)
        return f"Successfully applied labels: {labels}"
    except Exception as e:
        return f"Error applying labels: {e}"

TRIAGER_PROMPT = """
Role: You are the Triager Agent for the D&D AI system. 
Your job is to evaluate newly reported bugs/issues, assign priority, categorize them, and move them to the backlog.

Allowed Actions:
1. Use `apply_github_labels` to add exact text labels to the issue.

Execution Rules:
- Analyze the issue title and body.
- Assess priority. Choose ONE: 'priority: critical', 'priority: high', 'priority: medium', 'priority: low'.
- Categorize the issue. Choose ONE: 'category: narrative', 'category: rules', 'category: code', 'category: other'.
- ALWAYS add the label 'status: backlog' so the Planner knows to pick it up.
- Call `apply_github_labels` with your chosen list of labels.
"""

def main():
    issue_title = os.environ.get("ISSUE_TITLE", "Unknown Title")
    issue_body = os.environ.get("ISSUE_BODY", "No body provided.")
    
    print(f"Triaging Issue: {issue_title}")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-flash", temperature=0.2)
    agent = create_react_agent(llm, [apply_github_labels])
    state = {"messages": [SystemMessage(content=TRIAGER_PROMPT), HumanMessage(content=f"Please triage this new issue.\n\nTITLE: {issue_title}\n\nBODY:\n{issue_body}")]}
    agent.invoke(state)

if __name__ == "__main__":
    main()