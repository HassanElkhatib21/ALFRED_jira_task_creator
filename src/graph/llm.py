from typing import Literal, Optional
from pydantic import BaseModel, Field
from langchain_google_genai import ChatGoogleGenerativeAI
from langchain_core.messages import SystemMessage, HumanMessage

from src import config


class TaskDraft(BaseModel):
    title: Optional[str] = Field(None, description="Concise issue summary (max 120 chars).")
    issue_type: Optional[Literal["Task", "Bug", "Story", "Epic"]] = Field(
        None, description="Best-fit issue type. Default 'Task' if unclear."
    )
    project_key: Optional[str] = Field(None, description="Jira project key (uppercase letters/digits).")
    board_id: Optional[int] = Field(None, description="Numeric Jira board id.")
    sprint: Optional[str] = Field(
        None, description="'current', 'none', or numeric sprint id. Default 'current'."
    )
    description: Optional[str] = Field(None, description="Raw description text from user.")


class IntakeResult(BaseModel):
    ready: bool
    next_question: Optional[str] = None
    alfred_message: Optional[str] = Field(None, description="A custom, conversational message from Alfred when the draft is ready for review.")
    draft: TaskDraft


_INTAKE_SYSTEM = """You are Alfred Pennyworth, the loyal, highly capable, and extremely polite butler from Batman. You are integrated into a Discord bot to assist with Jira task intake.
Your job: read the user's natural-language request and extract a structured task draft.

The user's Discord name is provided below. You MUST infer their gender from their name. If male, address them as "Master [Name]" or "Sir". If female, address them as "Miss [Name]" or "Madam". If uncertain, default to "Master [Name]" or "Sir/Madam".

REQUIRED fields: title, project_key, board_id, description.
- project_key looks like "ABC", "ENG", "PROJ123" — alphanumeric, uppercase.
- board_id is a positive integer.
- issue_type defaults to "Task" unless clearly a bug/story/epic.
- sprint defaults to "current" unless the user specifies otherwise.

If the user has provided defaults below, USE them whenever the current request does not override them.

If a REQUIRED field is still missing, set ready=false and write next_question asking ONLY for what's missing. **CRITICAL:** Your next_question MUST be spoken in the voice of Alfred Pennyworth. Be exceptionally polite, professional, and dryly British. (e.g., "I beg your pardon, Master Hassan, but it appears we are missing the project key. Would you be so kind as to provide it?")

If everything is present, set ready=true, next_question=null, fill the draft completely, AND provide an `alfred_message`.
**CRITICAL:** The `alfred_message` MUST be spoken in the voice of Alfred Pennyworth. It should politely inform the user that the draft has been prepared and is ready for their review (e.g., "Right away, Master Hassan. I have prepared a draft regarding the login issue for your perusal. Shall we proceed?").
Title should be a clear imperative summary (max 120 chars). Keep description as the user's raw text.
"""


_FORMAT_SYSTEM = """You are a senior engineer writing a Jira issue description.
Rewrite the user's raw description into clean, well-structured Markdown.

Use ## headings for each section. Include only sections that have content — never invent details.

## Context
1-3 sentences of background.

## Details
- Bullet points expanding on what was said.

## Steps to Reproduce
(Only for bugs) Numbered list.

## Expected vs Actual
(Only for bugs)

## Acceptance Criteria
- Checkable outcome 1
- Checkable outcome 2
(Infer reasonable acceptance criteria from context if the user gave none.)

## Notes
Anything else relevant.

Rules:
- Preserve every concrete fact the user gave. Do not add numbers, names, dates, or links the user did not state.
- Do not include the title — only the body.
- Use proper Markdown: ## for headings, - for bullet lists, 1. for numbered lists, **bold** for emphasis.
- No code fences around the whole output.
- Always include at least Context and Acceptance Criteria sections.
"""


def _llm():
    return ChatGoogleGenerativeAI(
        model=config.GEMINI_MODEL,
        google_api_key=config.GEMINI_API_KEY,
        temperature=0.2,
    )


async def intake(
    user_request: str,
    discord_user: str,
    default_project: Optional[str] = None,
    default_board: Optional[int] = None,
) -> IntakeResult:
    llm = _llm().with_structured_output(IntakeResult)
    defaults = f"Defaults -> Project: {default_project}, Board: {default_board}"
    sys_msg = SystemMessage(content=_INTAKE_SYSTEM + f"\n\n{defaults}\n\nUser's Discord Name: {discord_user}")
    res = await llm.ainvoke([sys_msg, HumanMessage(content=user_request)])
    return res


async def format_description(title: str, raw_description: str, issue_type: str) -> str:
    msg = (
        f"Issue type: {issue_type}\n"
        f"Title: {title}\n"
        f"Raw description from user:\n---\n{raw_description}\n---"
    )
    resp = await _llm().ainvoke([
        SystemMessage(content=_FORMAT_SYSTEM),
        HumanMessage(content=msg),
    ])
    text = resp.content
    if isinstance(text, list):
        text = "\n".join(part if isinstance(part, str) else part.get("text", "") for part in text)
    return text.strip()
