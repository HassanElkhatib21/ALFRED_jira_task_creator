import logging
import re
import uuid
import discord

import httpx
from src import config, user_store
from src.graph.workflow import get_workflow
from src.graph import nodes

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("bot")


intents = discord.Intents.default()
intents.message_content = True
intents.dm_messages = True
client = discord.Client(intents=intents)
tree = discord.app_commands.CommandTree(client)


# user_id -> active LangGraph thread_id (so follow-up DMs continue the same conversation)
_active_threads: dict[int, str] = {}

# user_id -> index into SETUP_STEPS while collecting profile fields
_setup_pos: dict[int, int] = {}


# ---------- setup ----------

SETUP_STEPS: list[tuple[str, str, callable]] = []


def _v_url(v: str):
    if not re.match(r"^https?://[^\s]+\.atlassian\.net/?$", v.strip()):
        return None, "That doesn't look like a Jira URL. Try `https://yourco.atlassian.net`."
    return v.strip().rstrip("/"), None


def _v_email(v: str):
    v = v.strip()
    if "@" not in v or " " in v:
        return None, "That doesn't look like an email. Try again."
    return v, None


def _v_token(v: str):
    v = v.strip()
    if len(v) < 16:
        return None, "That token looks too short. Paste the full API token."
    return v, None


def _v_project(v: str):
    v = v.strip()
    if v.lower() == "skip":
        return None, None
    if not v.isalnum():
        return None, "Project keys are alphanumeric, like `ABC` or `PROJ123`. Try again or reply `skip`."
    return v.upper(), None


def _v_board(v: str):
    v = v.strip()
    if v.lower() == "skip":
        return None, None
    if not v.isdigit():
        return None, "Board id must be a number. Try again or reply `skip`."
    return int(v), None


SETUP_STEPS = [
    ("base_url", "**Step 1/5** — What's your Jira workspace URL? (e.g. `https://yourco.atlassian.net`)", _v_url),
    ("email", "**Step 2/5** — Your Jira email address?", _v_email),
    ("api_token", "**Step 3/5** — Your Jira API token?\n*To get one, go to: https://id.atlassian.com/manage-profile/security/api-tokens > Click 'Create API token' > Copy the token and paste it here.*", _v_token),
    ("default_project", "**Step 4/5** — Default project key (e.g. `ABC`)? Reply `skip` to specify per task.", _v_project),
    ("default_board", "**Step 5/5** — Default board id (numeric)? Reply `skip` to specify per task.", _v_board),
]


async def _start_setup(channel: discord.abc.Messageable, user_id: int, *, intro: str | None = None):
    log.info("Starting setup for user %s", user_id)
    _setup_pos[user_id] = 0
    if intro:
        await channel.send(intro)
    await channel.send(SETUP_STEPS[0][1])


async def _handle_setup_reply(channel: discord.abc.Messageable, user_id: int, content: str) -> bool:
    """Returns True if the message was consumed by setup."""
    if user_id not in _setup_pos:
        return False

    idx = _setup_pos[user_id]
    field, _, validator = SETUP_STEPS[idx]
    value, err = validator(content)
    if err:
        await channel.send(f"⚠️ {err}")
        return True

    log.info("Setup step %d/%d (%s) completed for user %s", idx + 1, len(SETUP_STEPS), field, user_id)
    user_store.save_field(str(user_id), field, value)

    idx += 1
    if idx >= len(SETUP_STEPS):
        del _setup_pos[user_id]
        await channel.send(
            "✅ All set! You can now describe a task in plain English and I'll draft a Jira issue for you.\n"
            "Tip: send `/reset` anytime to redo this setup."
        )
        return True

    _setup_pos[user_id] = idx
    await channel.send(SETUP_STEPS[idx][1])
    return True


# ---------- preview / approval ----------

def _preview_embed(preview: dict) -> discord.Embed:
    embed = discord.Embed(
        title=preview["title"],
        description=preview["description"][:4000],
        color=discord.Color.blurple(),
    )
    for f in preview["fields"]:
        embed.add_field(name=f["name"], value=f["value"], inline=f.get("inline", False))
    embed.set_footer(text=preview["footer"])
    return embed


class ApprovalView(discord.ui.View):
    def __init__(self, thread_id: str, requester_id: int, state: dict):
        super().__init__(timeout=3600)
        self.thread_id = thread_id
        self.requester_id = requester_id
        self.state = state

    async def _resume(self, interaction: discord.Interaction, decision: str) -> None:
        if interaction.user.id != self.requester_id:
            await interaction.response.send_message(
                "Only the original requester can decide.", ephemeral=True
            )
            return

        await interaction.response.defer()

        for child in self.children:
            child.disabled = True
        await interaction.message.edit(view=self)

        _active_threads.pop(self.requester_id, None)

        if decision == "reject":
            await interaction.followup.send("❌ Cancelled — no Jira issue created.")
            return

        # Call create_jira_issue directly with the stored state
        log.info("User %s approved — creating Jira issue", self.requester_id)
        try:
            result = await nodes.create_jira_issue(self.state)
        except Exception as e:
            log.exception("Jira creation failed for user %s", self.requester_id)
            await interaction.followup.send(f"⚠️ Jira error: {e}")
            return

        if result.get("failure"):
            await interaction.followup.send(f"⚠️ Jira error: {result['failure']}")
            return
        if result.get("issue_key"):
            await interaction.followup.send(
                f"✅ Created **{result['issue_key']}** — {result['issue_url']}"
            )
        else:
            await interaction.followup.send("⚠️ Workflow ended without creating an issue.")

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._resume(interaction, "approve")

    @discord.ui.button(label="Reject", style=discord.ButtonStyle.danger, emoji="❌")
    async def reject(self, interaction: discord.Interaction, _: discord.ui.Button):
        await self._resume(interaction, "reject")


# ---------- slash commands ----------

@tree.command(name="setup", description="Start the setup wizard for your Jira profile")
async def cmd_setup(interaction: discord.Interaction):
    user_id = interaction.user.id
    user_store.delete(str(user_id))
    _active_threads.pop(user_id, None)
    await interaction.response.send_message("Starting setup! Check your DMs.", ephemeral=True)
    try:
        await _start_setup(interaction.user, user_id, intro="🔄 Setting up your profile.")
    except discord.Forbidden:
        pass

@tree.command(name="reset", description="Reset your Jira profile")
async def cmd_reset(interaction: discord.Interaction):
    user_id = interaction.user.id
    user_store.delete(str(user_id))
    _active_threads.pop(user_id, None)
    await interaction.response.send_message("Profile reset! You can run `/setup` to start over.", ephemeral=True)

@tree.command(name="profile", description="View your current Jira profile configuration")
async def cmd_profile(interaction: discord.Interaction):
    profile = user_store.get(str(interaction.user.id))
    if not user_store.is_complete(profile):
        await interaction.response.send_message("You don't have a complete profile. Run `/setup` to configure it.", ephemeral=True)
        return
    embed = discord.Embed(title="Jira Profile", color=discord.Color.blue())
    embed.add_field(name="Email", value=profile["email"], inline=False)
    embed.add_field(name="Workspace URL", value=profile["base_url"], inline=False)
    embed.add_field(name="Default Project", value=profile.get("default_project") or "None", inline=True)
    embed.add_field(name="Default Board ID", value=str(profile.get("default_board") or "None"), inline=True)
    await interaction.response.send_message(embed=embed, ephemeral=True)

from src.graph import jira_client

@tree.command(name="search", description="Search for Jira issues")
@discord.app_commands.describe(query="Keyword or exact issue key to search for")
async def cmd_search(interaction: discord.Interaction, query: str):
    profile = user_store.get(str(interaction.user.id))
    if not user_store.is_complete(profile):
        await interaction.response.send_message("You must run `/setup` first.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    # properly escape quotes in JQL
    safe_query = query.replace('"', '\\"')
    jql = f'text ~ "{safe_query}" OR issueKey = "{safe_query}" ORDER BY updated DESC'
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            issues = await jira_client.search_issues(
                client, profile["base_url"], profile["email"], profile["api_token"], jql, limit=5
            )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Search failed: {e}")
        return

    if not issues:
        await interaction.followup.send(f"No issues found matching `{query}`.")
        return

    embed = discord.Embed(title=f"Search Results for '{query}'", color=discord.Color.green())
    for iss in issues:
        key = iss["key"]
        summary = iss["fields"]["summary"]
        status = iss["fields"]["status"]["name"]
        url = f'{profile["base_url"]}/browse/{key}'
        embed.add_field(name=f"[{key}] {status}", value=f"[{summary}]({url})", inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="recent", description="View your most recently updated Jira issues")
async def cmd_recent(interaction: discord.Interaction):
    profile = user_store.get(str(interaction.user.id))
    if not user_store.is_complete(profile):
        await interaction.response.send_message("You must run `/setup` first.", ephemeral=True)
        return
    if not profile.get("default_project"):
        await interaction.response.send_message("You need a default project set up to use this command.", ephemeral=True)
        return

    await interaction.response.defer(ephemeral=False)
    jql = f'project = "{profile["default_project"]}" ORDER BY updated DESC'
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            issues = await jira_client.search_issues(
                client, profile["base_url"], profile["email"], profile["api_token"], jql, limit=5
            )
    except Exception as e:
        await interaction.followup.send(f"⚠️ Fetch failed: {e}")
        return

    if not issues:
        await interaction.followup.send(f"No recent issues found in project {profile['default_project']}.")
        return

    embed = discord.Embed(title=f"Recent Issues in {profile['default_project']}", color=discord.Color.green())
    for iss in issues:
        key = iss["key"]
        summary = iss["fields"]["summary"]
        status = iss["fields"]["status"]["name"]
        url = f'{profile["base_url"]}/browse/{key}'
        embed.add_field(name=f"[{key}] {status}", value=f"[{summary}]({url})", inline=False)

    await interaction.followup.send(embed=embed)

@tree.command(name="clear", description="Clear recent bot messages from the chat")
async def cmd_clear(interaction: discord.Interaction, amount: int = 50):
    await interaction.response.defer(ephemeral=True)
    deleted = 0
    try:
        # If in a server and bot has manage_messages, purge all messages up to 'amount'
        if interaction.guild and interaction.channel.permissions_for(interaction.guild.me).manage_messages:
            deleted_msgs = await interaction.channel.purge(limit=amount)
            deleted = len(deleted_msgs)
        else:
            # In DMs (or without permissions), the bot can only delete its own messages
            async for msg in interaction.channel.history(limit=amount):
                if msg.author.id == client.user.id:
                    await msg.delete()
                    deleted += 1
        
        await interaction.followup.send(f"✅ Cleared {deleted} messages.", ephemeral=True)
    except Exception as e:
        await interaction.followup.send(f"⚠️ Failed to clear messages: {e}", ephemeral=True)


# ---------- main DM handler ----------

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or message.author.id == client.user.id:
        return
    if not isinstance(message.channel, discord.DMChannel):
        return

    log.info("DM from %s (id=%s): %s", message.author, message.author.id, message.content[:80])

    content = message.content.strip()
    if not content:
        return

    user_id = message.author.id
    channel = message.channel
    lowered = content.lower()

    # In the middle of setup
    if await _handle_setup_reply(channel, user_id, content):
        return

    # No profile yet → start setup
    profile = user_store.get(str(user_id))
    if not user_store.is_complete(profile):
        await _start_setup(
            channel, user_id,
            intro=(
                f"👋 Hey {message.author.display_name}! I can turn what you describe into Jira tasks. "
                "First, I need a few one-time details."
            ),
        )
        return

    # Normal task flow
    log.info("Running task request for user %s", user_id)
    async with channel.typing():
        try:
            await _run_request(message.author, channel, content, profile)
        except Exception as e:
            log.exception("Error handling request from user %s", user_id)
            await channel.send(f"⚠️ Internal error: {e}")


async def _run_request(user: discord.abc.User, channel: discord.abc.Messageable, request_text: str, profile: dict):
    user_id = user.id
    thread_id = _active_threads.get(user_id) or f"user-{user_id}-{uuid.uuid4().hex[:10]}"
    _active_threads[user_id] = thread_id

    cfg = {"configurable": {"thread_id": thread_id}}
    initial: dict = {
        "discord_user": user.display_name,
        "discord_user_id": str(user_id),
        "request": request_text,
        "jira_base_url": profile["base_url"],
        "jira_email": profile["email"],
        "jira_token": profile["api_token"],
        "default_project": profile.get("default_project"),
        "default_board": profile.get("default_board"),
    }

    wf = await get_workflow()
    log.info("Invoking workflow thread=%s for user=%s", thread_id, user_id)
    state = await wf.ainvoke(initial, config=cfg)
    log.info("Workflow returned — ready=%s, errors=%s, preview=%s",
             state.get('ready'), bool(state.get('errors')), bool(state.get('preview')))

    if not state.get("ready"):
        question = state.get("next_question") or "Could you give me more details?"
        await channel.send(f"🤖 {question}")
        return

    if state.get("errors"):
        bullets = "\n".join(f"• {e}" for e in state["errors"])
        _active_threads.pop(user_id, None)
        await channel.send(f"**Couldn't proceed:**\n{bullets}")
        return

    preview = state.get("preview")
    if not preview:
        _active_threads.pop(user_id, None)
        await channel.send("⚠️ Workflow produced no preview.")
        return

    view = ApprovalView(thread_id=thread_id, requester_id=user_id, state=state)
    await channel.send(
        content="**Preview** — review and confirm:",
        embed=_preview_embed(preview),
        view=view,
    )


@client.event
async def on_ready():
    user_store.init()
    log.info("✅ Logged in as %s (id=%s)", client.user, client.user.id)
    try:
        await tree.sync()
        log.info("✅ Slash commands synced globally.")
    except Exception as e:
        log.error("Failed to sync slash commands: %s", e)
    log.info("Bot is ready — DM to start.")


def main():
    log.info("Initializing bot...")
    user_store.init()
    log.info("User store initialized.")
    token = config.DISCORD_BOT_TOKEN
    if not token:
        log.error("DISCORD_BOT_TOKEN is empty or missing!")
        return
    log.info("Token loaded (length=%d). Connecting to Discord...", len(token))
    client.run(token)


if __name__ == "__main__":
    main()
