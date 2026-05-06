from typing import TypedDict, Optional


class GraphState(TypedDict, total=False):
    # Inputs from Discord
    discord_user: str
    discord_user_id: str
    request: str

    # Per-user Jira credentials
    jira_base_url: str
    jira_email: str
    jira_token: str
    default_project: Optional[str]
    default_board: Optional[int]

    # Filled by extract
    project_key: str
    board_id: int
    issue_type: str
    sprint: str
    title: str
    description: str
    ready: bool
    next_question: Optional[str]

    # Filled by enrich
    sprint_id: Optional[int]
    sprint_name: Optional[str]

    # Filled by format_description
    formatted_description: Optional[str]

    # Workflow state
    errors: list[str]
    preview: Optional[dict]
    decision: Optional[str]
    issue_key: Optional[str]
    issue_url: Optional[str]
    failure: Optional[str]
