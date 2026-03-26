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
def apply_triage_assessment(labels: list[str], comment: str = "") -> str:
    """Applies a list of labels to the current issue, and optionally posts a comment."""
    try:
        client = get_repo_client()
        issue_number = int(os.environ["ISSUE_NUMBER"])
        client.add_labels(issue_number, labels)
        if comment:
            client.post_comment(issue_number, comment)
        return f"Successfully applied labels: {labels}" + (" and added comment." if comment else ".")
    except Exception as e:
        return f"Error applying triage assessment: {e}"


# ----------------------------------------------------------------------
# Deterministic pre-filter: bypass LLM for obvious pattern-matched issues
# ----------------------------------------------------------------------
# (priority, category, scale, status, needs_architect_comment)
_PATTERNS = [
    # Crash/exception → software, high priority
    (re.compile(r"\b(crash|exception|traceback|stack trace|fatal error|critical error)\b", re.I), ["priority: high", "category: software", "scale: medium", "status: backlog"], False),
    # Memory/performance issues → software, high
    (re.compile(r"\b(memory leak|memory growth|performance|slow|latency|timeout|out of memory)\b", re.I), ["priority: high", "category: software", "scale: medium", "status: backlog"], False),
    # Bug/error with no rules/narrative → software
    (re.compile(r"\b(bug|error|wrong|incorrect|broken|fails? to|doesn.?t work)\b", re.I), ["priority: medium", "category: software", "scale: small", "status: backlog"], False),
    # Rules violations
    (re.compile(r"\b(rule|wrong|ruling|mechanic|damage|spell|ability|roll)\b", re.I), ["priority: medium", "category: rules", "scale: small", "status: backlog"], False),
    # Narrative/player agency issues
    (re.compile(r"\b(narrative|story|player agency|dialogue|prose|description|dm voice)\b", re.I), ["priority: low", "category: narrative-agency", "scale: small", "status: backlog"], False),
    # Epic-scale keywords
    (re.compile(r"\b(epic|large|refactor|architect|design overhaul|multi-file|redesign)\b", re.I), ["priority: medium", "category: software", "scale: epic", "status: needs_architect"], True),
]


def _deterministic_triage(issue_title: str, issue_body: str) -> dict | None:
    """
    Returns a dict with triage result if the issue matches a known pattern,
    or None if it should escalate to the LLM.
    """
    text = f"{issue_title} {issue_body}"
    for pattern, labels, needs_comment in _PATTERNS:
        if pattern.search(text):
            result = {
                "labels": labels,
                "needs_architect_comment": needs_comment,
                "escalate_to_llm": False,
            }
            # Check for epic scale in body (not just title)
            if "epic" in pattern.pattern.lower() and len(text) > 2000:
                result["escalate_to_llm"] = True  # Too complex, needs LLM to subdivide
            return result
    return None  # Novel issue — escalate to LLM


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
    if not os.environ.get("ISSUE_NUMBER"):
        print("No ISSUE_NUMBER provided. Exiting.")
        sys.exit(1)

    issue_title = os.environ.get("ISSUE_TITLE", "Unknown Title")
    issue_body = os.environ.get("ISSUE_BODY", "No body provided.")

    print(f"Triaging Issue: {issue_title}")

    # Try deterministic pre-filter first
    deterministic = _deterministic_triage(issue_title, issue_body)

    if deterministic and not deterministic["escalate_to_llm"]:
        labels = deterministic["labels"]
        needs_architect = deterministic["needs_architect_comment"]

        comment = ""
        if needs_architect:
            comment = (
                "This issue is too large for a single pass. "
                "Please sub-divide it into smaller, focused tasks before implementation."
            )

        print(f"[Deterministic triage] Labels: {labels}")
        result = apply_triage_assessment.invoke({
            "labels": labels,
            "comment": comment,
        })
        print(f"Result: {result}")
        return

    # Fall back to LLM for novel/mixed/unclear issues
    print("[LLM triage] No deterministic match — using LLM.")
    llm = ChatGoogleGenerativeAI(model="gemini-2.5-pro", temperature=0.2)
    agent = create_react_agent(llm, [apply_triage_assessment])
    state = {
        "messages": [
            SystemMessage(content=TRIAGER_PROMPT),
            HumanMessage(content=f"Please triage this new issue.\n\nTITLE: {issue_title}\n\nBODY:\n{issue_body}"),
        ]
    }
    agent.invoke(state, {"recursion_limit": 10})


if __name__ == "__main__":
    main()
