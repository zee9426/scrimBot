import discord
from discord.ext import commands, tasks
import datetime
import os
import json
import random
import aiohttp
from dotenv import load_dotenv
from zoneinfo import ZoneInfo  # Requires Python 3.9+

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
RIOT_API_KEY = os.getenv("RIOT_API_KEY")
# Default to Oceania region ("oc1")
RIOT_REGION = os.getenv("RIOT_REGION", "oc1")

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
# Player role selections: { user_id: {"team": 1 or 2, "position": <position>} }
player_roles = {}
# Tournament codes (list)
tournament_codes = []

STATE_FILE = "state.json"
signup_message = None    # Global embed message
ready_notification_sent = False  # For duplicate notifications

# -----------------------------------------------------------------------------
# Utility functions: Tip, Announcements, and Riot Tournament Code API integration
# -----------------------------------------------------------------------------
def get_tip_of_the_day() -> str:
    tips = [
        "Communicate with your team at all times.",
        "Practice your mechanics to improve your gameplay.",
        "Maintain a positive attitude even when behind.",
        "Review your replays to learn from mistakes."
    ]
    day = datetime.datetime.now().timetuple().tm_yday
    return tips[day % len(tips)]

def get_announcements() -> str:
    return "Upcoming tournament next weekend! Prepare your strategies."

async def generate_tournament_code(tournament_id: str, count: int, pick_type: str, spectator_type: str, map_type: str) -> list:
    url = f"https://{RIOT_REGION}.api.riotgames.com/lol/tournament/v4/codes"
    params = {
        "tournamentId": tournament_id,
        "count": count,
        "pickType": pick_type,
        "spectatorType": spectator_type,
        "mapType": map_type
    }
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                codes = await resp.json()
                return codes
            else:
                print("Error generating tournament code:", resp.status, await resp.text())
                return None

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
        "player_roles": player_roles,
        "tournament_codes": tournament_codes
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print("State saved.")

async def load_state(guild: discord.Guild):
    global signups, reserves, available_times, player_roles, tournament_codes
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
    
    player_roles.update(state.get("player_roles", {}))
    tournament_codes.clear()
    tournament_codes.extend(state.get("tournament_codes", []))
    
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
def get_team_roles(team: int) -> dict:
    roles = {"Top": None, "JG": None, "Mid": None, "Bot": None, "Support": None}
    fills = []
    for uid, data in player_roles.items():
        if data["team"] == team:
            pos = data["position"]
            if pos == "Fill":
                fills.append(uid)
            elif roles[pos] is None:
                roles[pos] = uid
            else:
                fills.append(uid)
    for pos in roles:
        if roles[pos] is None and fills:
            roles[pos] = fills.pop(0)
    return roles

def format_team_roles(team: int) -> str:
    roles = get_team_roles(team)
    lines = []
    for pos in ["Top", "JG", "Mid", "Bot", "Support"]:
        if roles.get(pos):
            line = f"**{pos}:** <@{roles[pos]}>"
        else:
            line = f"**{pos}:** Open"
        lines.append(line)
    return "\n".join(lines)

# -----------------------------------------------------------------------------
# Embed creation and update functions
# -----------------------------------------------------------------------------
async def get_live_data_field() -> str:
    # Dummy live League API data; replace with a real API call as needed.
    data = {"in_custom_game": random.choice([True, False]), "game_time": random.randint(1, 30)}
    if data["in_custom_game"]:
        return f"In Custom Game | Game Time: {data['game_time']} min"
    else:
        return "No live game data."

def create_embed() -> discord.Embed:
    now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
    reset_time = now_nzt.replace(hour=9, minute=0, second=0, microsecond=0)
    if now_nzt >= reset_time:
        reset_time += datetime.timedelta(days=1)
    remaining = reset_time - now_nzt
    total_seconds = int(remaining.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    time_until_reset_str = f"{hours:02d}:{minutes:02d}"
    
    today_str = now_nzt.strftime("%A, %B %d, %Y")
    tip = get_tip_of_the_day()
    announcements = get_announcements()
    
    guild = bot.get_guild(GUILD_ID)
    embed = discord.Embed(
        title="Custom Game Sign‐Up",
        description=f"Today is {today_str} and 5pm onwards.\n\nTip of the Day: *{tip}*\nAnnouncements: {announcements}",
        color=0x00ff00
    )
    # Visuals: update these URLs as needed (or use GitHub raw URLs)
    embed.set_image(url="https://raw.githubusercontent.com/your_username/your_repo/main/images/banner.png")
    embed.set_thumbnail(url="https://raw.githubusercontent.com/your_username/your_repo/main/images/thumbnail.png")
    embed.set_author(name="Custom SignUp", icon_url="https://raw.githubusercontent.com/your_username/your_repo/main/images/icon.png")
    
    team1 = format_team_roles(1)
    team2 = format_team_roles(2)
    
    embed.add_field(name="Team 1", value=team1, inline=True)
    embed.add_field(name="Team 2", value=team2, inline=True)
    
    # Live game data field (to be updated)
    embed.add_field(name="Live Game Data", value="Fetching...", inline=False)
    
    # Tournament code field (if any)
    if tournament_codes:
        embed.add_field(name="Tournament Lobby", value=", ".join(tournament_codes), inline=False)
    
    embed.set_footer(text=f"Max active players: {MAX_ACTIVE_PLAYERS}. Lobby resets in: {time_until_reset_str}")
    return embed

async def update_embed_message():
    global signup_message
    if signup_message:
        embed = create_embed()
        live_data = await get_live_data_field()
        if len(embed.fields) >= 2:
            # Assume the live data field is the second-to-last field.
            embed.set_field_at(len(embed.fields)-2, name="Live Game Data", value=live_data, inline=False)
        try:
            await signup_message.edit(embed=embed)
        except Exception as e:
            print("Error updating embed:", e)
    save_state()

# -----------------------------------------------------------------------------
# Update role selection functions
# -----------------------------------------------------------------------------
def update_role(user_id: int, team: int, position: str):
    global player_roles
    player_roles[user_id] = {"team": team, "position": position}

def get_available_positions_for_team(team: int) -> list:
    taken = set()
    for uid, data in player_roles.items():
        if data["team"] == team and data["position"] != "Fill":
            taken.add(data["position"])
    positions = ["Top", "JG", "Mid", "Bot", "Support", "Fill"]
    return [pos for pos in positions if pos == "Fill" or pos not in taken]

# -----------------------------------------------------------------------------
# Global Controls View
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
            player_roles.pop(user.id, None)
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
# RoleSelectView: Two-step selection for team then position with restrictions.
# -----------------------------------------------------------------------------
class RoleSelectView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.user = user
        self.selected_team = None
        self.add_item(TeamSelect())

    async def on_timeout(self):
        self.stop()

class TeamSelect(discord.ui.Select):
    def __init__(self):
        options = [
            discord.SelectOption(label="Team 1", value="1"),
            discord.SelectOption(label="Team 2", value="2")
        ]
        super().__init__(placeholder="Select your team", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: RoleSelectView = self.view
        view.selected_team = int(self.values[0])
        view.clear_items()
        available = get_available_positions_for_team(view.selected_team)
        view.add_item(PositionSelect(available))
        await interaction.response.edit_message(content="Select your position:", view=view)

class PositionSelect(discord.ui.Select):
    def __init__(self, available_positions: list):
        options = [discord.SelectOption(label=pos, value=pos) for pos in available_positions]
        super().__init__(placeholder="Select your position", min_values=1, max_values=1, options=options)

    async def callback(self, interaction: discord.Interaction):
        view: RoleSelectView = self.view
        selected_position = self.values[0]
        if view.selected_team is None:
            await interaction.response.send_message("Team not selected.", ephemeral=True)
            return
        update_role(interaction.user.id, view.selected_team, selected_position)
        await interaction.response.edit_message(content=f"Your role has been set to Team {view.selected_team} - {selected_position}.", view=None)
        await update_embed_message()
        view.stop()

# -----------------------------------------------------------------------------
# SetTimeView: Allows user to adjust their ready time.
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
        view.add_item(GenerateTournamentCodeButton())
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
# Tournament Code Generation
# -----------------------------------------------------------------------------
tournament_codes = []

async def generate_tournament_code(tournament_id: str, count: int, pick_type: str, spectator_type: str, map_type: str) -> list:
    url = f"https://{RIOT_REGION}.api.riotgames.com/lol/tournament/v4/codes"
    params = {
        "tournamentId": tournament_id,
        "count": count,
        "pickType": pick_type,
        "spectatorType": spectator_type,
        "mapType": map_type
    }
    headers = {"X-Riot-Token": RIOT_API_KEY}
    async with aiohttp.ClientSession() as session:
        async with session.post(url, headers=headers, params=params) as resp:
            if resp.status == 200:
                codes = await resp.json()
                return codes
            else:
                print("Error generating tournament code:", resp.status, await resp.text())
                return None

class GenerateTournamentCodeButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Generate Tournament Code", style=discord.ButtonStyle.primary, custom_id="generate_tournament_code")
    
    async def callback(self, interaction: discord.Interaction):
        view = GenerateTournamentModal()
        await interaction.response.send_modal(view)

class GenerateTournamentModal(discord.ui.Modal, title="Generate Tournament Code"):
    tournament_id_input = discord.ui.TextInput(label="Tournament ID", placeholder="Enter Tournament ID")
    count_input = discord.ui.TextInput(label="Count", placeholder="Number of codes", default="1")
    pick_type_input = discord.ui.TextInput(label="Pick Type", placeholder="e.g., DRAFT_MODE", default="DRAFT_MODE")
    spectator_type_input = discord.ui.TextInput(label="Spectator Type", placeholder="e.g., ALL", default="ALL")
    map_type_input = discord.ui.TextInput(label="Map Type", placeholder="e.g., SUMMONERS_RIFT", default="SUMMONERS_RIFT")
    
    async def callback(self, interaction: discord.Interaction):
        global tournament_codes
        tournament_id = self.tournament_id_input.value
        try:
            count = int(self.count_input.value)
        except ValueError:
            count = 1
        pick_type = self.pick_type_input.value
        spectator_type = self.spectator_type_input.value
        map_type = self.map_type_input.value
        
        codes = await generate_tournament_code(tournament_id, count, pick_type, spectator_type, map_type)
        if codes:
            tournament_codes.clear()
            tournament_codes.extend(codes)
            await interaction.response.send_message(f"Tournament code(s) generated: {', '.join(codes)}", ephemeral=True)
        else:
            await interaction.response.send_message("Failed to generate tournament code.", ephemeral=True)
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
    await update_embed_message()

@tasks.loop(time=datetime.time(hour=9, minute=0))
async def reset_signups():
    global signups, reserves, available_times, player_roles, tournament_codes
    signups.clear()
    reserves.clear()
    available_times.clear()
    player_roles.clear()
    tournament_codes.clear()
    channel = bot.get_channel(SIGNUP_CHANNEL_ID)
    if channel:
        await channel.send("Daily reset: Sign-ups are now open!")
        await update_embed_message()
    save_state()

# -----------------------------------------------------------------------------
# Bot Startup: Initialize global embed and load state.
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
