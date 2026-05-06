import httpx
from langgraph.types import interrupt

from src import config
from src.graph import jira_client, llm
from src.graph.state import GraphState


async def extract(state: GraphState) -> dict:
    result = await llm.intake(
        user_request=state["request"],
        discord_user=state["discord_user"],
        default_project=state.get("default_project"),
        default_board=state.get("default_board"),
    )
    draft = result.draft

    out: dict = {
        "ready": result.ready,
        "next_question": result.next_question,
        "alfred_message": getattr(result, "alfred_message", None),
    }
    if draft.title:
        out["title"] = draft.title.strip()
    if draft.description:
        out["description"] = draft.description.strip()
    out["issue_type"] = draft.issue_type or "Task"
    if draft.project_key:
        out["project_key"] = draft.project_key.strip().upper()
    elif state.get("default_project"):
        out["project_key"] = state["default_project"]
    if draft.board_id is not None:
        out["board_id"] = int(draft.board_id)
    elif state.get("default_board") is not None:
        out["board_id"] = int(state["default_board"])
    out["sprint"] = (draft.sprint or "current").strip().lower()
    return out


def validate(state: GraphState) -> dict:
    errors: list[str] = []

    if not state.get("title"):
        errors.append("Title missing.")
    elif len(state["title"]) > 255:
        errors.append("Title must be <= 255 characters.")

    if not state.get("description"):
        errors.append("Description missing.")

    if state.get("issue_type") not in config.ALLOWED_ISSUE_TYPES:
        errors.append(f"Issue type must be one of: {', '.join(config.ALLOWED_ISSUE_TYPES)}.")

    pk = state.get("project_key", "")
    if not pk or not pk.isalnum():
        errors.append("Project key missing or invalid.")

    if not isinstance(state.get("board_id"), int) or state["board_id"] <= 0:
        errors.append("Board id missing or invalid.")

    sprint = state.get("sprint") or "current"
    if sprint not in ("current", "none") and not sprint.isdigit():
        errors.append("Sprint must be 'current', 'none', or a numeric id.")

    return {"errors": errors}


async def enrich(state: GraphState) -> dict:
    errors = list(state.get("errors") or [])
    sprint_id: int | None = None
    sprint_name: str | None = None
    base_url = state["jira_base_url"]
    email = state["jira_email"]
    token = state["jira_token"]

    async with httpx.AsyncClient(timeout=20.0) as client:
        try:
            await jira_client.get_project(client, base_url, email, token, state["project_key"])
        except jira_client.JiraError as e:
            errors.append(str(e))

        sprint = state["sprint"]
        if sprint == "current":
            try:
                active = await jira_client.get_active_sprint(client, base_url, email, token, state["board_id"])
                if active is None:
                    pass  # Kanban board or no active sprint — continue without sprint
                else:
                    sprint_id = active["id"]
                    sprint_name = active["name"]
            except jira_client.JiraError as e:
                errors.append(str(e))
        elif sprint.isdigit():
            try:
                s = await jira_client.get_sprint(client, base_url, email, token, int(sprint))
                sprint_id = s["id"]
                sprint_name = s["name"]
            except jira_client.JiraError as e:
                errors.append(str(e))

    return {"errors": errors, "sprint_id": sprint_id, "sprint_name": sprint_name}


async def format_description(state: GraphState) -> dict:
    formatted = await llm.format_description(
        title=state["title"],
        raw_description=state["description"],
        issue_type=state["issue_type"],
    )
    return {"formatted_description": formatted}


def build_preview(state: GraphState) -> dict:
    sprint_label = state.get("sprint_name") or "— (no sprint)"
    preview = {
        "title": f"[{state['project_key']}] {state['title']}",
        "fields": [
            {"name": "Project", "value": state["project_key"], "inline": True},
            {"name": "Type", "value": state["issue_type"], "inline": True},
            {"name": "Sprint", "value": sprint_label, "inline": True},
            {"name": "Board", "value": str(state["board_id"]), "inline": True},
            {"name": "Reporter", "value": state["discord_user"], "inline": True},
        ],
        "description": state.get("formatted_description") or state["description"],
        "footer": "AI-organized preview. Approve to create in Jira.",
        "alfred_message": state.get("alfred_message"),
    }
    return {"preview": preview}


async def await_approval(state: GraphState) -> dict:
    decision = interrupt({"preview": state["preview"]})
    return {"decision": decision}


async def create_jira_issue(state: GraphState) -> dict:
    body = state.get("formatted_description") or state["description"]
    body += f"\n\n---\n*Reported via Discord by {state['discord_user']}*"
    idem_key = f"discord-{state['discord_user_id']}-{state['title'][:40]}"
    base_url = state["jira_base_url"]
    email = state["jira_email"]
    token = state["jira_token"]
    async with httpx.AsyncClient(timeout=30.0) as client:
        try:
            issue = await jira_client.create_issue(
                client, base_url, email, token,
                project_key=state["project_key"],
                summary=state["title"],
                description=body,
                issue_type=state["issue_type"],
                idempotency_key=idem_key,
            )
            issue_key = issue["key"]
            issue_url = f"{base_url}/browse/{issue_key}"
            if state.get("sprint_id"):
                await jira_client.add_to_sprint(client, base_url, email, token, state["sprint_id"], issue_key)
            return {"issue_key": issue_key, "issue_url": issue_url}
        except jira_client.JiraError as e:
            return {"failure": str(e)}


def extract_branch(state: GraphState) -> str:
    return "ready" if state.get("ready") else "ask"


def has_errors(state: GraphState) -> str:
    return "fail" if state.get("errors") else "ok"


def decision_branch(state: GraphState) -> str:
    return "approve" if state.get("decision") == "approve" else "reject"
