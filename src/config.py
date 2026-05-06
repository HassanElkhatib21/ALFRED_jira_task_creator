import os
from dotenv import load_dotenv

load_dotenv()

DISCORD_BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

GEMINI_API_KEY = os.environ["GEMINI_API_KEY"]
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-flash-lite")

CHECKPOINT_DB = os.getenv("CHECKPOINT_DB", "./checkpoints.sqlite")
USER_DB = os.getenv("USER_DB", "./users.sqlite")

JIRA_BASE_URL = os.getenv("JIRA_BASE_URL", "").rstrip("/")
JIRA_API_TOKEN = os.getenv("JIRA_API_TOKEN", "")

ALLOWED_ISSUE_TYPES = ["Task", "Bug", "Story", "Epic"]
