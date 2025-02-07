import discord
from discord.ext import commands, tasks
import datetime
import os
from dotenv import load_dotenv
from zoneinfo import ZoneInfo  # Requires Python 3.9+; install tzdata via "pip install tzdata" if needed

# Load environment variables from the .env file.
load_dotenv()

# Retrieve configuration values.
TOKEN = os.getenv("DISCORD_BOT_TOKEN")
GUILD_ID = int(os.getenv("GUILD_ID", "757815438271840367"))
SIGNUP_CHANNEL_ID = int(os.getenv("SIGNUP_CHANNEL_ID", "1336930586341937195"))
COMMAND_PREFIX = os.getenv("COMMAND_PREFIX", "!")
MAX_ACTIVE_PLAYERS = int(os.getenv("MAX_ACTIVE_PLAYERS", "10"))
ADMIN_ID = int(os.getenv("ADMIN_ID", "0"))

# Set up intents and the bot.
intents = discord.Intents.all()
bot = commands.Bot(command_prefix=COMMAND_PREFIX, intents=intents)

# Global state for sign-ups and ready times.
signups = []            # List of active players (discord.Member objects)
reserves = []           # List of reserve players (discord.Member objects)
available_times = {}    # Mapping from user id -> ready datetime (in NZT)

signup_message = None   # Reference to the signup message.
ready_notification_sent = False  # To prevent repeated notifications.

def create_embed() -> discord.Embed:
    """
    Creates an embed showing active players (with ready times if set) and reserve players.
    If a player's ready time has lapsed, display "Ready now" instead.
    """
    # Get the current NZT time once.
    now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
    
    embed = discord.Embed(
        title="Custom Game Sign‐Up",
        description=("Click the buttons below to sign up, cancel your signup, declare your last game, "
                     "or set your ready time."),
        color=0x00ff00
    )
    
    if signups:
        active_list = "\n".join(
            f"{i+1}. {member.mention}" +
            (
                " (Ready: " +
                (
                    "Ready now" if now_nzt >= available_times[member.id]
                    else available_times[member.id].strftime("%H:%M %Z")
                ) + ")"
                if member.id in available_times else ""
            )
            for i, member in enumerate(signups)
        )
    else:
        active_list = "None"
    
    reserve_list = "\n".join(f"{i+1}. {member.mention}" for i, member in enumerate(reserves)) if reserves else "None"
    
    embed.add_field(name="Active Players", value=active_list, inline=False)
    embed.add_field(name="Reserves", value=reserve_list, inline=False)
    embed.set_footer(text=f"Note: Maximum active players = {MAX_ACTIVE_PLAYERS}. Reserve players are promoted as spots open up.")
    return embed

async def update_embed_message():
    """
    Updates the signup message's embed.
    """
    global signup_message
    if signup_message is not None:
        new_embed = create_embed()
        try:
            await signup_message.edit(embed=new_embed)
        except Exception as e:
            print("Error updating embed:", e)

##########################################
# SetTimeView – lets user adjust ready time via increments.
##########################################
class SetTimeView(discord.ui.View):
    def __init__(self, user: discord.User):
        super().__init__(timeout=60)  # Session lasts 60 seconds.
        self.user = user
        self.offset_minutes = 0  # Offset (in minutes) from now.
    
    def current_time_str(self) -> str:
        hrs = self.offset_minutes // 60
        mins = self.offset_minutes % 60
        return f"{hrs}h {mins}m"
    
    @discord.ui.button(label="Hour +1", style=discord.ButtonStyle.green)
    async def hour_plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This session is not for you.", ephemeral=True)
            return
        self.offset_minutes += 60
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Hour -1", style=discord.ButtonStyle.red)
    async def hour_minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This session is not for you.", ephemeral=True)
            return
        self.offset_minutes = max(0, self.offset_minutes - 60)
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Minute +10", style=discord.ButtonStyle.green)
    async def minute_plus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This session is not for you.", ephemeral=True)
            return
        self.offset_minutes += 10
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Minute -10", style=discord.ButtonStyle.red)
    async def minute_minus(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This session is not for you.", ephemeral=True)
            return
        self.offset_minutes = max(0, self.offset_minutes - 10)
        await interaction.response.edit_message(content=f"Current selection: {self.current_time_str()} from now.")
    
    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.primary)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This session is not for you.", ephemeral=True)
            return
        try:
            now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
        except Exception as e:
            await interaction.response.send_message("Error: Timezone data not found.", ephemeral=True)
            return
        ready_time = now_nzt + datetime.timedelta(minutes=self.offset_minutes)
        available_times[self.user.id] = ready_time
        formatted_time = ready_time.strftime("%H:%M %Z")
        await interaction.response.send_message(f"Your ready time has been set to {formatted_time}.", ephemeral=True)
        await update_embed_message()
        self.stop()
    
    @discord.ui.button(label="ASAP", style=discord.ButtonStyle.blurple)
    async def asap(self, interaction: discord.Interaction, button: discord.ui.Button):
        if interaction.user != self.user:
            await interaction.response.send_message("This session is not for you.", ephemeral=True)
            return
        try:
            now_nzt = datetime.datetime.now(ZoneInfo("Pacific/Auckland"))
        except Exception as e:
            await interaction.response.send_message("Error: Timezone data not found.", ephemeral=True)
            return
        available_times[self.user.id] = now_nzt
        formatted_time = now_nzt.strftime("%H:%M %Z")
        await interaction.response.send_message(f"Your ready time has been set to ASAP ({formatted_time}).", ephemeral=True)
        await update_embed_message()
        self.stop()

##########################################
# Main SignupView – includes a "Set Time" button.
##########################################
class SignupView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)
    
    @discord.ui.button(label="Sign Up", style=discord.ButtonStyle.green, custom_id="signup_button")
    async def signup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global signups, reserves
        user = interaction.user
        if user in signups or user in reserves:
            await interaction.response.send_message("You're already signed up!", ephemeral=True)
            return
        if len(signups) < MAX_ACTIVE_PLAYERS:
            signups.append(user)
            await interaction.response.send_message("You've been added as an **active player**!", ephemeral=True)
        else:
            reserves.append(user)
            await interaction.response.send_message("Active spots are full. You've been added to the **reserve list**!", ephemeral=True)
        await update_embed_message()
    
    @discord.ui.button(label="Cancel Signup", style=discord.ButtonStyle.red, custom_id="cancel_signup_button")
    async def cancel_signup_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global signups, reserves
        user = interaction.user
        if user in signups:
            signups.remove(user)
            if reserves:
                promoted = reserves.pop(0)
                signups.append(promoted)
                await interaction.response.send_message(f"You've been removed. {promoted.mention} has been promoted from reserves!", ephemeral=True)
            else:
                await interaction.response.send_message("You've been removed from the active players list.", ephemeral=True)
        elif user in reserves:
            reserves.remove(user)
            await interaction.response.send_message("You've been removed from the reserve list.", ephemeral=True)
        else:
            await interaction.response.send_message("You're not signed up!", ephemeral=True)
        await update_embed_message()
    
    @discord.ui.button(label="Last Game", style=discord.ButtonStyle.blurple, custom_id="last_game_button")
    async def last_game_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        global signups, reserves
        user = interaction.user
        if user not in signups:
            await interaction.response.send_message("You are not an active player!", ephemeral=True)
            return
        signups.remove(user)
        if reserves:
            promoted = reserves.pop(0)
            signups.append(promoted)
            await interaction.response.send_message(f"You declared your last game. {promoted.mention} has been promoted from reserves!", ephemeral=True)
        else:
            await interaction.response.send_message("You declared your last game. No reserves are available at this time.", ephemeral=True)
        await update_embed_message()
    
    @discord.ui.button(label="Set Time", style=discord.ButtonStyle.gray, custom_id="set_time_button")
    async def set_time_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        view = SetTimeView(interaction.user)
        await interaction.response.send_message("Set your ready time using these buttons:", view=view, ephemeral=True)

##########################################
# Background task to check for ready players.
##########################################
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
        if member.id in available_times:
            if now_nzt >= available_times[member.id]:
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

##########################################
# Daily reset task.
##########################################
@tasks.loop(time=datetime.time(hour=12, minute=0))
async def reset_signups():
    global signups, reserves, available_times
    signups.clear()
    reserves.clear()
    available_times.clear()
    channel = bot.get_channel(SIGNUP_CHANNEL_ID)
    if channel:
        await channel.send("Daily reset: Sign-ups are now open!")
        await update_embed_message()

##########################################
# Bot startup: send the signup message.
##########################################
@bot.event
async def on_ready():
    global signup_message
    print(f"{bot.user} is online!")
    for guild in bot.guilds:
        print(f"Bot is in guild: {guild.name} (ID: {guild.id})")
    view = SignupView()
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
    reset_signups.start()
    check_ready_players.start()

bot.run(TOKEN)
