import discord
from discord.ext import commands, tasks
import datetime
import os
import json
from dotenv import load_dotenv
from zoneinfo import ZoneInfo  # Python 3.9+; ensure tzdata is installed

# -----------------------------------------------------------------------------
# Load configuration from .env
# -----------------------------------------------------------------------------
load_dotenv()

TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "757815438271840367"))
SIGNUP_CHANNEL_ID = int(os.getenv("SIGNUP_CHANNEL_ID", "1336930586341937195"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
MAX_ACTIVE_PLAYERS = int(os.getenv("MAX_ACTIVE_PLAYERS", "10"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))
ADMIN_ROLE = os.getenv("ADMIN_ROLE", "Admin")

# -----------------------------------------------------------------------------
# Set up the bot
# -----------------------------------------------------------------------------
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# -----------------------------------------------------------------------------
# Global state
# -----------------------------------------------------------------------------
signups = []             # Active players (discord.Member objects)
reserves = []            # Reserve players (discord.Member objects)
available_times = {}     # Mapping: user_id -> ready datetime
# New: Store each player's team and position selection.
# Format: { user_id: { "team": 1 or 2, "position": "Top"/"JG"/"Mid"/"Bot"/"Support"/"Fill" } }
player_roles = {}

STATE_FILE = "state.json"
signup_message = None    # Global embed message in the signup channel
ready_notification_sent = False  # For duplicate notifications

# -----------------------------------------------------------------------------
# State persistence functions
# -----------------------------------------------------------------------------
def save_state():
    global signup_message
    state = {
        "signup_message_id": signup_message.id if signup_message else None,
        "signups": [member.id for member in signups],
        "reserves": [member.id for member in reserves],
        "available_times": {str(uid): dt.isoformat() for uid, dt in available_times.items()},
        "player_roles": player_roles
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print("State saved.")

async def load_state(guild: discord.Guild):
    global signups, reserves, available_times, player_roles
    if not os.path.exists(STATE_FILE):
        print("No saved state found.")
        return
    with open(STATE_FILE, "r") as f:
        state = json.load(f)
    signups_ids = [int(x) for x in state.get("signups", [])]
    reserves_ids = [int(x) for x in state.get("reserves", [])]
    avail_times = {}
    for uid, iso in state.get("available_times", {}).items():
        try:
            dt_obj = datetime.datetime.fromisoformat(iso)
            avail_times[int(uid)] = dt_obj
        except Exception as e:
            print(f"Error parsing time for user {uid}: {e}")
    available_times.clear()
    available_times.update(avail_times)
    
    player_roles = state.get("player_roles", {})

    new_signups = []
    for uid in signups_ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception as e:
                print(f"Could not fetch member {uid}: {e}")
                continue
        if member:
            new_signups.append(member)
    signups.clear()
    signups.extend(new_signups)
    
    new_reserves = []
    for uid in reserves_ids:
        member = guild.get_member(uid)
        if member is None:
            try:
                member = await guild.fetch_member(uid)
            except Exception as e:
                print(f"Could not fetch member {uid}: {e}")
                continue
        if member:
            new_reserves.append(member)
    reserves.clear()
    reserves.extend(new_reserves)
    
    print("State loaded.")

# -----------------------------------------------------------------------------
# Helper: Format New Zealand time (returns HH:MM with NZDT/NZST)
# -----------------------------------------------------------------------------
def format_nz_time(dt: datetime.datetime) -> str:
    offset = dt.utcoffset()
    if offset is not None:
        total_hours = offset.total_seconds() / 3600
        if total_hours == 13:
            abbrev = "NZDT"
        elif total_hours == 12:
            abbrev = "NZST"
        else:
            abbrev = dt.tzname() or ""
    else:
        abbrev = ""
    return dt.strftime("%H:%M ") + abbrev

# -----------------------------------------------------------------------------
# Team Composition Helpers
# -----------------------------------------------------------------------------
def get_team_composition(team_number: int) -> dict:
    # Initialize roles for a team.
    comp = {"Top": None, "JG": None, "Mid": None, "Bot": None, "Support": None, "Fill": []}
    # Iterate over player_roles.
    for uid, data in player_roles.items():
        if data["team"] == team_number:
            pos = data["position"]
            if pos != "Fill":
                # If the role is not already assigned, assign it.
                if comp[pos] is None:
                    comp[pos] = f"<@{uid}>"
                else:
                    # If already taken, add to fill list.
                    comp["Fill"].append(f"<@{uid}>")
            else:
                comp["Fill"].append(f"<@{uid}>")
    # Auto-assign fill players to open roles.
    for role in ["Top", "JG", "Mid", "Bot", "Support"]:
        if comp[role] is None and comp["Fill"]:
            comp[role] = comp["Fill"].pop(0)
    return comp

def format_team(comp: dict) -> str:
    lines = []
    for role in ["Top", "JG", "Mid", "Bot", "Support"]:
        if comp.get(role):
            lines.append(f"**{role}:** {comp[role]}")
        else:
            lines.append(f"**{role}:** Open")
    return "\n".join(lines)

# -----------------------------------------------------------------------------
# Embed creation and update functions (with team compositions and lobby reset clock)
# -----------------------------------------------------------------------------
def create_embed() -> discord.Embed:
    now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
    # Calculate lobby reset time (reset at 9:00 AM NZT)
    reset_time = now_nzt.replace(hour=9, minute=0, second=0, microsecond=0)
    if now_nzt >= reset_time:
        reset_time += datetime.timedelta(days=1)
    remaining = reset_time - now_nzt
    total_seconds = int(remaining.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    time_until_reset_str = f"{hours:02d}:{minutes:02d}"
    
    today_str = now_nzt.strftime("%A, %B %d, %Y")
    
    guild = bot.get_guild(GUILD_ID)
    embed = discord.Embed(
        title="Custom Game Sign‐Up",
        description=f"Today is {today_str} and 5pm onwards. Click **I'm in!** to sign up and select your team & position.",
        color=0x00ff00
    )
    # Add visual embellishments (update URLs as needed)
    embed.set_image(url="https://raw.githubusercontent.com/your_username/your_repo/main/images/banner.png")
    embed.set_thumbnail(url="https://raw.githubusercontent.com/your_username/your_repo/main/images/thumbnail.png")
    embed.set_author(name="Custom SignUp", icon_url="https://raw.githubusercontent.com/your_username/your_repo/main/images/icon.png")
    
    # Build team compositions from player_roles
    team1 = get_team_composition(1)
    team2 = get_team_composition(2)
    
    embed.add_field(name="Team 1", value=format_team(team1), inline=True)
    embed.add_field(name="Team 2", value=format_team(team2), inline=True)
    embed.set_footer(text=f"Max active players: {MAX_ACTIVE_PLAYERS}. Lobby resets in: {time_until_reset_str}")
    return embed

async def update_embed_message():
    global signup_message
    if signup_message:
        new_embed = create_embed()
        try:
            await signup_message.edit(embed=new_embed)
        except Exception as e:
            print("Error updating embed:", e)
    save_state()

# -----------------------------------------------------------------------------
# New: Update player's role selection
# -----------------------------------------------------------------------------
def update_role(user_id: int, team: int, position: str):
    global player_roles
    player_roles[user_id] = {"team": team, "position": position}

# -----------------------------------------------------------------------------
# Global Controls View: A single public "I'm in!" button.
# -----------------------------------------------------------------------------
class GlobalControlsView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="I'm in!", style=discord.ButtonStyle.green, custom_id="global_im_in")
    async def im_in_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global signups, reserves
        user = interaction.user
        if user not in signups and user not in reserves:
            if len(signups) < MAX_ACTIVE_PLAYERS:
                signups.append(user)
            else:
                reserves.append(user)
            await update_embed_message()
        view = UserControlView(user)
        await interaction.response.send_message("", view=view, ephemeral=True)

# -----------------------------------------------------------------------------
# Personalized User Control View (Ephemeral)
# -----------------------------------------------------------------------------
class UserControlView(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=0)
        self.user = user
        self.add_item(ToggleOutButton())
        self.add_item(SetTimeButton())
        self.add_item(SelectRoleButton())
        if any(role.name == ADMIN_ROLE for role in user.roles):
            self.add_item(AdminControlsButton())

# -----------------------------------------------------------------------------
# User Control Buttons
# -----------------------------------------------------------------------------
class ToggleOutButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="I'm out!", style=discord.ButtonStyle.red, custom_id="user_toggle_out")
    
    async def callback(self, interaction: discord.Interaction):
        global signups, reserves, available_times, player_roles
        user = interaction.user
        if user in signups:
            signups.remove(user)
            available_times.pop(user.id, None)
            player_roles.pop(user.id, None)  # Remove role selection when leaving
            if reserves:
                promoted = reserves.pop(0)
                signups.append(promoted)
        await update_embed_message()
        await interaction.response.edit_message(content="You've been removed.", view=None)

class SetTimeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Set Time", style=discord.ButtonStyle.gray, custom_id="user_set_time")
    
    async def callback(self, interaction: discord.Interaction):
        view = SetTimeView(interaction.user)
        await interaction.response.edit_message(content="Adjust your ready time:", view=view)

class SelectRoleButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Select Role", style=discord.ButtonStyle.primary, custom_id="select_role")
    
    async def callback(self, interaction: discord.Interaction):
        view = RoleSelectView(interaction.user)
        await interaction.response.edit_message(content="Select your team and position:", view=view)

# -----------------------------------------------------------------------------
# RoleSelectView: Presents two select menus for team and position.
# -----------------------------------------------------------------------------
class RoleSelectView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.user = user
        self.selected_team = None
        self.selected_position = None

    @discord.ui.select(
        placeholder="Select your team",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Team 1", value="1"),
            discord.SelectOption(label="Team 2", value="2")
        ]
    )
    async def team_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_team = int(select.values[0])
        if self.selected_position is not None:
            update_role(interaction.user.id, self.selected_team, self.selected_position)
            await interaction.response.edit_message(content=f"Your role has been set to Team {self.selected_team} - {self.selected_position}.", view=None)
            await update_embed_message()
            self.stop()
        else:
            await interaction.response.defer()

    @discord.ui.select(
        placeholder="Select your position",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="Top", value="Top"),
            discord.SelectOption(label="JG", value="JG"),
            discord.SelectOption(label="Mid", value="Mid"),
            discord.SelectOption(label="Bot", value="Bot"),
            discord.SelectOption(label="Support", value="Support"),
            discord.SelectOption(label="Fill", value="Fill")
        ]
    )
    async def position_select(self, interaction: discord.Interaction, select: discord.ui.Select):
        self.selected_position = select.values[0]
        if self.selected_team is not None:
            update_role(interaction.user.id, self.selected_team, self.selected_position)
            await interaction.response.edit_message(content=f"Your role has been set to Team {self.selected_team} - {self.selected_position}.", view=None)
            await update_embed_message()
            self.stop()
        else:
            await interaction.response.defer()

# -----------------------------------------------------------------------------
# SetTimeView: Allows the user to adjust their ready time via increments.
# -----------------------------------------------------------------------------
class SetTimeView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.user = user
        self.offset_minutes = 0
    
    def current_time_str(self) -> str:
        hrs = self.offset_minutes // 60
        mins = self.offset_minutes % 60
        return f"{hrs}h {mins}m"
    
    @discord.ui.button(label="Hour +1", style=discord.ButtonStyle.green)
    async def hour_plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.defer(ephemeral=True)
            return
        self.offset_minutes += 60
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Hour -1", style=discord.ButtonStyle.red)
    async def hour_minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.defer(ephemeral=True)
            return
        self.offset_minutes = max(0, self.offset_minutes - 60)
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Minute +10", style=discord.ButtonStyle.green)
    async def minute_plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.defer(ephemeral=True)
            return
        self.offset_minutes += 10
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Minute -10", style=discord.ButtonStyle.red)
    async def minute_minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.defer(ephemeral=True)
            return
        self.offset_minutes = max(0, self.offset_minutes - 10)
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.defer(ephemeral=True)
            return
        try:
            now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
        except Exception as e:
            await interaction.response.defer(ephemeral=True)
            return
        ready_time = now_nzt + datetime.timedelta(minutes=self.offset_minutes)
        available_times[self.user.id] = ready_time
        formatted_time = ready_time.strftime("%H:%M %Z")
        await interaction.response.edit_message(content=f"Your ready time has been set to {formatted_time}.", view=None)
        await update_embed_message()
        self.stop()
    
    @discord.ui.button(label="ASAP", style=discord.ButtonStyle.blurple)
    async def asap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.defer(ephemeral=True)
            return
        try:
            now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
        except Exception as e:
            await interaction.response.defer(ephemeral=True)
            return
        available_times[self.user.id] = now_nzt
        formatted_time = now_nzt.strftime("%H:%M %Z")
        await interaction.response.edit_message(content=f"Your ready time has been set to ASAP ({formatted_time}).", view=None)
        await update_embed_message()
        self.stop()

# -----------------------------------------------------------------------------
# Admin Controls
# -----------------------------------------------------------------------------
class AdminControlsButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Admin Controls", style=discord.ButtonStyle.primary, custom_id="admin_controls")
    
    async def callback(self, interaction: discord.Interaction):
        view = AdminControlView(interaction.user)
        await interaction.response.send_message("Admin Controls:", view=view, ephemeral=True)

class AdminControlView(discord.ui.View):
    def __init__(self, admin: discord.Member):
        super().__init__(timeout=60)
        self.admin = admin
        self.add_item(RemovePlayerButton())

class RemovePlayerButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Remove Player", style=discord.ButtonStyle.danger, custom_id="admin_remove_player")
    
    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RemovePlayerModal())

class RemovePlayerModal(discord.ui.Modal, title="Remove Player"):
    user_id_input = discord.ui.TextInput(label="User ID to remove", placeholder="Enter the user ID")
    
    async def callback(self, interaction: discord.Interaction):
        global signups, reserves, available_times, player_roles
        try:
            uid = int(self.user_id_input.value)
        except ValueError:
            await interaction.response.send_message("", view=None)
            return
        if any(member.id == uid for member in signups):
            signups[:] = [m for m in signups if m.id != uid]
        if any(member.id == uid for member in reserves):
            reserves[:] = [m for m in reserves if m.id != uid]
        available_times.pop(uid, None)
        player_roles.pop(uid, None)
        await interaction.response.send_message("", view=None)
        await update_embed_message()

# -----------------------------------------------------------------------------
# Background Tasks
# -----------------------------------------------------------------------------
@tasks.loop(seconds=60)
async def check_ready_players():
    global ready_notification_sent
    try:
        now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
    except Exception as e:
        print("Timezone error in check_ready_players:", e)
        return
    ready_count = 0
    for member in signups:
        if member.id in available_times and now_nzt >= available_times[member.id]:
            ready_count += 1
    if ready_count >= 10 and not ready_notification_sent:
        ready_notification_sent = True
        channel = bot.get_channel(SIGNUP_CHANNEL_ID)
        if channel:
            notify_message = f"@here 10 players are ready to go! (Ready count: {ready_count})"
            await channel.send(notify_message)
            if ADMIN_ID:
                admin = channel.guild.get_member(ADMIN_ID)
                if admin:
                    try:
                        await admin.send(f"10 players are ready to go in {channel.name}!")
                    except Exception as e:
                        print("Could not DM admin:", e)
    elif ready_count < 10:
        ready_notification_sent = False

@tasks.loop(minutes=1)
async def update_lobby_clock():
    # Refresh the global embed every minute so the lobby reset clock updates.
    await update_embed_message()

@tasks.loop(time=datetime.time(hour=9, minute=0))
async def reset_signups():
    global signups, reserves, available_times, player_roles
    signups.clear()
    reserves.clear()
    available_times.clear()
    player_roles.clear()
    channel = bot.get_channel(SIGNUP_CHANNEL_ID)
    if channel:
        await channel.send("Daily reset: Sign-ups are now open!")
        await update_embed_message()
    save_state()

# -----------------------------------------------------------------------------
# Bot Startup: Send (or update) the global signup message and load persisted state.
# -----------------------------------------------------------------------------
@bot.event
async def on_ready():
    global signup_message
    print(f"{bot.user} is online!")
    for guild in bot.guilds:
        print(f"Bot is in guild: {guild.name} (ID: {guild.id})")
    view = GlobalControlsView()
    bot.add_view(view)
    channel = bot.get_channel(SIGNUP_CHANNEL_ID)
    if channel is None:
        print("Channel not found in cache. Attempting to fetch it from Discord...")
        try:
            channel = await bot.fetch_channel(SIGNUP_CHANNEL_ID)
        except Exception as e:
            print("Error fetching channel:", e)
            return
    print(f"Found signup channel: {channel.name} (ID: {channel.id})")
    stored_msg_id = None
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r") as f:
            state = json.load(f)
        stored_msg_id = state.get("signup_message_id")
    if stored_msg_id:
        try:
            signup_message = await channel.fetch_message(stored_msg_id)
            print("Fetched existing signup message.")
        except Exception as e:
            print("Could not fetch stored signup message, sending new one:", e)
            signup_message = await channel.send(embed=create_embed(), view=view)
    else:
        signup_message = await channel.send(embed=create_embed(), view=view)
    await load_state(channel.guild)
    await update_embed_message()
    reset_signups.start()
    check_ready_players.start()
    update_lobby_clock.start()

bot.run(TOKEN)
