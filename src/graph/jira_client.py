import re
import httpx


def _auth(email: str, token: str) -> httpx.BasicAuth:
    return httpx.BasicAuth(email, token)


# --------------- Markdown → ADF converter ---------------

def _inline_marks(text: str) -> list[dict]:
    """Convert inline Markdown (bold, italic, bold-italic, code) to ADF text nodes."""
    nodes: list[dict] = []
    # Pattern order matters: bold-italic first, then bold, italic, inline code
    pattern = re.compile(
        r'(\*\*\*(.+?)\*\*\*)'   # ***bold italic***
        r'|(\*\*(.+?)\*\*)'      # **bold**
        r'|(\*(.+?)\*)'          # *italic*
        r'|(`(.+?)`)'            # `code`
    )
    last = 0
    for m in pattern.finditer(text):
        # Add plain text before this match
        if m.start() > last:
            plain = text[last:m.start()]
            if plain:
                nodes.append({"type": "text", "text": plain})

        if m.group(2):  # bold-italic
            nodes.append({"type": "text", "text": m.group(2), "marks": [{"type": "strong"}, {"type": "em"}]})
        elif m.group(4):  # bold
            nodes.append({"type": "text", "text": m.group(4), "marks": [{"type": "strong"}]})
        elif m.group(6):  # italic
            nodes.append({"type": "text", "text": m.group(6), "marks": [{"type": "em"}]})
        elif m.group(8):  # code
            nodes.append({"type": "text", "text": m.group(8), "marks": [{"type": "code"}]})
        last = m.end()

    # Remaining plain text
    if last < len(text):
        tail = text[last:]
        if tail:
            nodes.append({"type": "text", "text": tail})

    return nodes or [{"type": "text", "text": text}]


def _adf(text: str) -> dict:
    """Convert Markdown text to Atlassian Document Format (ADF)."""
    lines = text.split("\n")
    blocks: list[dict] = []
    i = 0

    while i < len(lines):
        line = lines[i].rstrip()

        # --- Horizontal rule ---
        if re.match(r'^-{3,}$|^\*{3,}$|^_{3,}$', line.strip()):
            blocks.append({"type": "rule"})
            i += 1
            continue

        # --- Headings (# ... ######) ---
        hm = re.match(r'^(#{1,6})\s+(.+)', line)
        if hm:
            level = len(hm.group(1))
            blocks.append({
                "type": "heading",
                "attrs": {"level": level},
                "content": _inline_marks(hm.group(2).strip()),
            })
            i += 1
            continue

        # --- Unordered list (- item, * item) ---
        if re.match(r'^[\s]*[-*]\s+', line):
            items = []
            while i < len(lines) and re.match(r'^[\s]*[-*]\s+', lines[i].rstrip()):
                item_text = re.sub(r'^[\s]*[-*]\s+', '', lines[i].rstrip())
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _inline_marks(item_text)}],
                })
                i += 1
            blocks.append({"type": "bulletList", "content": items})
            continue

        # --- Ordered list (1. item, 2. item) ---
        if re.match(r'^[\s]*\d+\.\s+', line):
            items = []
            while i < len(lines) and re.match(r'^[\s]*\d+\.\s+', lines[i].rstrip()):
                item_text = re.sub(r'^[\s]*\d+\.\s+', '', lines[i].rstrip())
                items.append({
                    "type": "listItem",
                    "content": [{"type": "paragraph", "content": _inline_marks(item_text)}],
                })
                i += 1
            blocks.append({"type": "orderedList", "content": items})
            continue

        # --- Empty line → skip (don't create empty paragraphs) ---
        if not line.strip():
            i += 1
            continue

        # --- Regular paragraph ---
        blocks.append({
            "type": "paragraph",
            "content": _inline_marks(line),
        })
        i += 1

    if not blocks:
        blocks = [{"type": "paragraph", "content": []}]

    return {"type": "doc", "version": 1, "content": blocks}


class JiraError(Exception):
    pass


async def get_project(client: httpx.AsyncClient, base_url: str, email: str, token: str, project_key: str) -> dict:
    r = await client.get(f"{base_url}/rest/api/3/project/{project_key}", auth=_auth(email, token))
    if r.status_code in (401, 403):
        raise JiraError("Jira authentication failed — check your email and API token (try `/setup`).")
    if r.status_code == 404:
        raise JiraError(f"Project '{project_key}' not found.")
    r.raise_for_status()
    return r.json()


async def get_active_sprint(client: httpx.AsyncClient, base_url: str, email: str, token: str, board_id: int) -> dict | None:
    r = await client.get(
        f"{base_url}/rest/agile/1.0/board/{board_id}/sprint",
        params={"state": "active"}, auth=_auth(email, token),
    )
    if r.status_code == 404:
        raise JiraError(f"Board {board_id} not found.")
    if r.status_code == 400:
        # Kanban boards don't support sprints — treat as "no active sprint"
        return None
    r.raise_for_status()
    values = r.json().get("values", [])
    return values[0] if values else None


async def get_sprint(client: httpx.AsyncClient, base_url: str, email: str, token: str, sprint_id: int) -> dict:
    r = await client.get(
        f"{base_url}/rest/agile/1.0/sprint/{sprint_id}", auth=_auth(email, token),
    )
    if r.status_code == 404:
        raise JiraError(f"Sprint {sprint_id} not found.")
    r.raise_for_status()
    return r.json()


async def create_issue(
    client: httpx.AsyncClient,
    base_url: str, email: str, token: str,
    project_key: str, summary: str, description: str, issue_type: str,
    idempotency_key: str,
) -> dict:
    payload = {
        "fields": {
            "project": {"key": project_key},
            "summary": summary,
            "description": _adf(description),
            "issuetype": {"name": issue_type},
        }
    }
    headers = {"X-Atlassian-Token": "no-check", "Idempotency-Key": idempotency_key}
    r = await client.post(
        f"{base_url}/rest/api/3/issue",
        json=payload, auth=_auth(email, token), headers=headers,
    )
    if r.status_code >= 400:
        raise JiraError(f"Issue create failed [{r.status_code}]: {r.text}")
    return r.json()


async def add_to_sprint(client: httpx.AsyncClient, base_url: str, email: str, token: str, sprint_id: int, issue_key: str) -> None:
    r = await client.post(
        f"{base_url}/rest/agile/1.0/sprint/{sprint_id}/issue",
        json={"issues": [issue_key]}, auth=_auth(email, token),
    )
    if r.status_code >= 400:
        raise JiraError(f"Sprint assignment failed [{r.status_code}]: {r.text}")


async def search_issues(client: httpx.AsyncClient, base_url: str, email: str, token: str, jql: str, limit: int = 5) -> list[dict]:
    """Search Jira issues using JQL."""
    payload = {
        "jql": jql,
        "maxResults": limit,
        "fields": ["summary", "status", "issuetype", "updated"]
    }
    r = await client.post(
        f"{base_url}/rest/api/3/search/jql",
        json=payload,
        auth=_auth(email, token)
    )
    r.raise_for_status()
    return r.json().get("issues", [])
