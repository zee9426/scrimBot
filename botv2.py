import discord
from discord.ext import commands, tasks
import datetime
import os
import json
from dotenv import load_dotenv
from zoneinfo import ZoneInfo  # Python 3.9+; ensure tzdata is installed (pip install tzdata)

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
# Global state (store user IDs and ISO-formatted datetimes when saving)
# -----------------------------------------------------------------------------
signups = []             # List of discord.Member objects (active players)
reserves = []            # List of discord.Member objects (reserve players)
available_times = {}     # Mapping: user_id (int) -> ready datetime (datetime object)
game_choices = {}        # Mapping: user_id (int) -> chosen game (string)

STATE_FILE = "state.json"
signup_message = None    # Global signup message in the signup channel
ready_notification_sent = False  # Flag to avoid duplicate notifications

# -----------------------------------------------------------------------------
# State persistence functions
# -----------------------------------------------------------------------------
def save_state():
    state = {
        "signups": [member.id for member in signups],
        "reserves": [member.id for member in reserves],
        "available_times": {str(uid): dt.isoformat() for uid, dt in available_times.items()},
        "game_choices": game_choices
    }
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)
    print("State saved.")

async def load_state(guild: discord.Guild):
    global signups, reserves, available_times, game_choices
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
    
    game_choices = state.get("game_choices", {})
    
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
# Helper: Format New Zealand time (displays NZDT or NZST)
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
# Embed creation and update functions
# -----------------------------------------------------------------------------
def create_embed() -> discord.Embed:
    now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
    embed = discord.Embed(
        title="Custom Game Sign‐Up",
        description="Click **I'm in!** to sign up, manage your ready time, and select a game.",
        color=0x00ff00
    )
    if signups:
        active_list = "\n".join(
            f"{i+1}. {member.mention}" +
            (f" (Ready: {('🟢 ' if now_nzt >= available_times[member.id] else '🟠 ') + format_nz_time(available_times[member.id])})"
             if member.id in available_times else "")
            for i, member in enumerate(signups)
        )
    else:
        active_list = "None"
    reserve_list = "\n".join(f"{i+1}. {member.mention}" for i, member in enumerate(reserves)) if reserves else "None"
    
    embed.add_field(name="Active Players", value=active_list, inline=False)
    embed.add_field(name="Reserves", value=reserve_list, inline=False)
    embed.set_footer(text=f"Max active players: {MAX_ACTIVE_PLAYERS}. Reserve players are promoted as spots open up.")
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
        # Send an ephemeral control view for the user.
        view = UserControlView(user)
        await interaction.response.send_message("", view=view, ephemeral=True)

# -----------------------------------------------------------------------------
# Personalized User Control View (Ephemeral)
# -----------------------------------------------------------------------------
class UserControlView(discord.ui.View):
    def __init__(self, user: discord.Member):
        super().__init__(timeout=0)
        self.user = user
        # Since clicking "I'm in!" immediately signs the user up, show controls for signed-up users.
        self.add_item(ToggleOutButton())
        self.add_item(SetTimeButton())
        self.add_item(LastGameButton())
        self.add_item(SelectGamesButton())
        if any(role.name == ADMIN_ROLE for role in user.roles):
            self.add_item(AdminControlsButton())

# -----------------------------------------------------------------------------
# User Control Buttons
# -----------------------------------------------------------------------------
class ToggleOutButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="I'm out!", style=discord.ButtonStyle.red, custom_id="user_toggle_out")
    
    async def callback(self, interaction: discord.Interaction):
        global signups, reserves, available_times
        user = interaction.user
        if user in signups:
            signups.remove(user)
            available_times.pop(user.id, None)
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

class LastGameButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Last Game", style=discord.ButtonStyle.blurple, custom_id="user_last_game")
    
    async def callback(self, interaction: discord.Interaction):
        global signups, reserves, available_times
        user = interaction.user
        if user not in signups:
            await interaction.response.edit_message(content="You are not an active player.", view=None)
            return
        signups.remove(user)
        available_times.pop(user.id, None)
        if reserves:
            promoted = reserves.pop(0)
            signups.append(promoted)
        await update_embed_message()
        await interaction.response.edit_message(content="You've been removed.", view=None)

# -----------------------------------------------------------------------------
# New: SelectGamesButton and GameSelectView
# -----------------------------------------------------------------------------
class SelectGamesButton(discord.ui.Button):
    def __init__(self):
        super().__init__(label="Select Games", style=discord.ButtonStyle.primary, custom_id="select_games")
    
    async def callback(self, interaction: discord.Interaction):
        view = GameSelectView(interaction.user)
        await interaction.response.edit_message(content="Select your game:", view=view)

class GameSelectView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)
        self.user = user

    @discord.ui.select(
        placeholder="Choose your game...",
        min_values=1,
        max_values=1,
        options=[
            discord.SelectOption(label="League", description="Play League of Legends", emoji="🏆"),
            discord.SelectOption(label="CS2", description="Play Counter-Strike 2", emoji="🔫")
        ]
    )
    async def select_callback(self, select: discord.ui.Select, interaction: discord.Interaction):
        global game_choices
        choice = select.values[0]
        game_choices[interaction.user.id] = choice
        await interaction.response.edit_message(content=f"Your game choice has been set to **{choice}**.", view=None)
        # Optionally update the embed or state if desired.

# Initialize the global game_choices dictionary
game_choices = {}

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
        global signups, reserves, available_times
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

@tasks.loop(time=datetime.time(hour=9, minute=0))
async def reset_signups():
    global signups, reserves, available_times
    signups.clear()
    reserves.clear()
    available_times.clear()
    channel = bot.get_channel(SIGNUP_CHANNEL_ID)
    if channel:
        await channel.send("Daily reset: Sign-ups are now open!")
        await update_embed_message()
    save_state()

# -----------------------------------------------------------------------------
# Bot Startup: Send the global signup message and load persisted state.
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
    embed = create_embed()
    signup_message = await channel.send(embed=embed, view=view)
    await load_state(channel.guild)
    await update_embed_message()
    reset_signups.start()
    check_ready_players.start()

bot.run(TOKEN)
