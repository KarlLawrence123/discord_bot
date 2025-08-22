import discord
from discord.ext import commands
from discord import app_commands
from typing import Optional
from discord.ui import Modal, TextInput, View, Button
from discord import TextStyle
import aiosqlite
import os
from dotenv import load_dotenv
import asyncio
from datetime import datetime, timedelta, timezone
import re
import json

intents = discord.Intents.default()
intents.message_content = False
intents.members = True

bot = commands.Bot(command_prefix='!', intents=intents)
db_path = 'projects.db'


MANAGER_ROLE_ID = int(os.getenv('MANAGER_ROLE_ID')) if os.getenv('MANAGER_ROLE_ID', '').isdigit() else None
ARCHIVE_AFTER_DAYS = int(os.getenv('ARCHIVE_AFTER_DAYS')) if os.getenv('ARCHIVE_AFTER_DAYS', '').isdigit() else None

NOTIFY_USER_ID = int(os.getenv('NOTIFY_USER_ID')) if os.getenv('NOTIFY_USER_ID', '').isdigit() else 1177981973789151383


def parse_to_iso_or_none(raw: Optional[str]) -> Optional[str]:
    """Parse common human date/time strings to ISO 8601. Supports:
    - MM/DD/YYYY
    - MM/DD/YYYY HH:MM (24h)
    - YYYY-MM-DD[ T]HH:MM
    - YYYY-MM-DD
    - HH:MM (interpreted as today)
    Returns None if empty. Raises ValueError on invalid input.
    """
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    patterns = [
        '%m/%d/%Y',            
        '%m/%d/%Y %H:%M',      
        '%Y-%m-%dT%H:%M',     
        '%Y-%m-%d %H:%M',      
        '%Y-%m-%d',            
    ]
    for fmt in patterns:
        try:
            dt = datetime.strptime(text, fmt)
            return dt.isoformat(timespec='minutes')
        except ValueError:
            continue
    
    try:
        dt_time_only = datetime.strptime(text, '%H:%M')
        today = datetime.today()
        merged = today.replace(hour=dt_time_only.hour, minute=dt_time_only.minute, second=0, microsecond=0)
        return merged.isoformat(timespec='minutes')
    except ValueError:
        pass

    
    try:
        return datetime.fromisoformat(text).isoformat(timespec='minutes')
    except Exception as exc:
        raise ValueError(f'Invalid date/time format: "{text}"') from exc


def parse_minutes_seconds_or_none(raw: Optional[str]) -> Optional[str]:
    """Parse MM:SS into a normalized zero-padded string. Returns None if empty."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    parts = text.split(":")
    if len(parts) != 2:
        raise ValueError(f'Invalid time format (use MM:SS): "{text}"')
    try:
        minutes = int(parts[0])
        seconds = int(parts[1])
    except ValueError as exc:
        raise ValueError(f'Invalid numeric values in MM:SS: "{text}"') from exc
    if seconds < 0 or seconds > 59 or minutes < 0:
        raise ValueError(f'Invalid range in MM:SS: "{text}"')
    return f"{minutes:02d}:{seconds:02d}"


def compute_deadline_from_days_or_iso(raw: Optional[str]) -> Optional[str]:
    """If input is digits, treat as days from now and return ISO string. Otherwise fall back to parse_to_iso_or_none."""
    if raw is None:
        return None
    text = str(raw).strip()
    if not text:
        return None
    if text.isdigit():
        days = int(text)
        deadline_dt = datetime.utcnow() + timedelta(days=days)
        return deadline_dt.isoformat(timespec='minutes')
    # Fallback to general date parsing
    return parse_to_iso_or_none(text)

@bot.event
async def on_ready():
    print(f'Logged in as {bot.user}')
    # Database setup: create tables if not exist
    async with aiosqlite.connect(db_path) as db:
        await db.execute('''
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                resource_links TEXT NOT NULL,
                start_time TEXT,
                end_time TEXT,
                deadline TEXT,
                notes TEXT,
                assigned_editor_id INTEGER,
                rate TEXT,
                status TEXT DEFAULT 'unassigned',
                submission_link TEXT,
                payment_status TEXT DEFAULT 'pending'
            )
        ''')
        # Best-effort migration for new columns
        try:
            await db.execute('ALTER TABLE projects ADD COLUMN thread_id INTEGER')
        except Exception:
            pass
        try:
            await db.execute('ALTER TABLE projects ADD COLUMN created_by INTEGER')
        except Exception:
            pass
        try:
            await db.execute('ALTER TABLE projects ADD COLUMN started_at TEXT')
        except Exception:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS project_messages (
                message_id INTEGER PRIMARY KEY,
                project_id INTEGER NOT NULL,
                channel_id INTEGER
            )
        ''')
        # Migrate existing project_messages to add channel_id if missing
        try:
            await db.execute('ALTER TABLE project_messages ADD COLUMN channel_id INTEGER')
        except Exception:
            pass
        await db.execute('''
            CREATE TABLE IF NOT EXISTS editors (
                user_id INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                position TEXT NOT NULL,
                gcash TEXT NOT NULL,
                email TEXT NOT NULL,
                avatar_url TEXT,
                availability_status TEXT DEFAULT 'available',
                max_concurrent_projects INTEGER DEFAULT 3,
                current_projects INTEGER DEFAULT 0,
                last_active TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        # Best-effort migration for new columns
        try:
            await db.execute('ALTER TABLE editors ADD COLUMN availability_status TEXT DEFAULT "available"')
        except Exception:
            pass
        try:
            await db.execute('ALTER TABLE editors ADD COLUMN max_concurrent_projects INTEGER DEFAULT 3')
        except Exception:
            pass
        try:
            await db.execute('ALTER TABLE editors ADD COLUMN current_projects INTEGER DEFAULT 0')
        except Exception:
            pass
        try:
            await db.execute('ALTER TABLE editors ADD COLUMN last_active TEXT DEFAULT CURRENT_TIMESTAMP')
        except Exception:
            pass
        try:
            await db.execute('ALTER TABLE editors ADD COLUMN gcash_qr_url TEXT')
        except Exception:
            pass
        await db.commit()
    # Start background reminder task
    if not hasattr(bot, 'reminder_task'):
        bot.reminder_task = bot.loop.create_task(deadline_reminder_task())
    
    # Start background project count update task
    if not hasattr(bot, 'project_count_task'):
        bot.project_count_task = bot.loop.create_task(update_editor_project_counts())

    # Sync slash commands to all guilds the bot is in for immediate availability
    try:
        for guild in bot.guilds:
            bot.tree.copy_global_to(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f'Slash commands synced to guild: {guild.name} ({guild.id})')
        # Register persistent views so buttons work after restarts
        bot.add_view(AssignmentButtons())
    except Exception as e:
        print(f'Command sync failed: {e}')

# -----------------------------
# Slash command definitions
# -----------------------------

@bot.tree.command(name="ping", description="Ping the bot")
async def slash_ping(interaction: discord.Interaction):
    await interaction.response.send_message("pong")


@bot.tree.command(name="help", description="Show all available commands")
async def slash_help(interaction: discord.Interaction):
    """Show all available commands and their purposes"""
    embed = discord.Embed(
        title="🤖 Discord Bot Commands", 
        description="Here are all the available commands:",
        color=discord.Color.blue()
    )
    
    # Project Management Commands
    embed.add_field(
        name="📋 **Project Management**", 
        value="""
`/add_project_ui` - Create and assign a new project
`/assign_ui` - Assign existing project to editor
`/assign_project_ui` - Assign with availability check
`/submit_ui` - Submit a project for review
`/approve_ui` - Approve a submitted project
`/reject_ui` - Reject a submitted project
`/mark_paid_ui` - Mark project as paid
        """, 
        inline=False
    )
    
    # Editor Management Commands
    embed.add_field(
        name="👤 **Editor Management**", 
        value="""
`/register_editor_ui` - Register as an editor
`/set_availability` - Set your availability status
`/check_availability` - Check editor availability
`/my_status` - Check your own status
        """, 
        inline=False
    )
    
    # Project Information Commands
    embed.add_field(
        name="🔍 **Project Information**", 
        value="""
`/list_projects_ui` - List projects with filters
`/list_submitted` - List submitted projects awaiting review
`/project_details <id>` - Get detailed project information
`/search_projects` - Search projects by criteria
`/project_summary` - Get overview of all projects
        """, 
        inline=False
    )
    
    # Utility Commands
    embed.add_field(
        name="🛠️ **Utility**", 
        value="""
`/ping` - Test bot connectivity
`/help` - Show this help message
        """, 
        inline=False
    )
    
    embed.set_footer(text="Use these commands to manage your video editing projects efficiently!")
    await interaction.response.send_message(embed=embed, ephemeral=True)


class AddProjectModal(Modal, title="Add Project"):
    def __init__(self, database_path: str, editor: discord.Member):
        super().__init__()
        self.database_path = database_path
        self.editor = editor
        self.project_name = TextInput(
            label="Project name",
            placeholder="e.g., Client Promo Video",
            required=True,
            max_length=100,
            style=TextStyle.short,
        )
        self.start_time = TextInput(
            label="Start time (MM:SS, optional)",
            placeholder="MM:SS",
            required=False,
            style=TextStyle.short,
        )
        self.end_time = TextInput(
            label="End time (MM:SS, optional)",
            placeholder="MM:SS",
            required=False,
            style=TextStyle.short,
        )
        self.rate = TextInput(
            label="Rate",
            placeholder="$100 or 5,000 PHP",
            required=True,
            style=TextStyle.short,
            max_length=50,
        )
        self.deadline = TextInput(
            label="Deadline (days from now or date)",
            placeholder="e.g., 3 or 10/25/2025",
            required=False,
            style=TextStyle.short,
        )
        # Add inputs to the modal
        self.add_item(self.project_name)
        self.add_item(self.start_time)
        self.add_item(self.end_time)
        self.add_item(self.deadline)
        self.add_item(self.rate)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        async with aiosqlite.connect(self.database_path) as db:
            # Normalize date inputs
            try:
                # Interpret start/end as MM:SS and store as plain strings
                start_str = parse_minutes_seconds_or_none(self.start_time.value)
                end_str = parse_minutes_seconds_or_none(self.end_time.value)
                # Deadline: allow simple day offsets or full dates
                deadline_iso = compute_deadline_from_days_or_iso(self.deadline.value)
            except ValueError as e:
                await interaction.response.send_message(str(e), ephemeral=True)
                return

            cursor = await db.execute(
                'INSERT INTO projects (name, resource_links, start_time, end_time, deadline, notes, assigned_editor_id, rate, status) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                (
                    str(self.project_name.value).strip(),
                    '',  # resources will be collected via follow-up modal
                    start_str,
                    end_str,
                    deadline_iso,
                    None,  # notes omitted from modal
                    int(self.editor.id),
                    str(self.rate.value).strip(),
                    'assigned',
                ),
            )
            await db.commit()
            new_project_id = cursor.lastrowid
            await cursor.close()
        # Send an ephemeral button for the user to open the Resources modal
        await interaction.response.send_message(
            "Click the button to add resources and finish posting the assignment.",
            view=CollectResourcesView(self.database_path, new_project_id),
            ephemeral=True,
        )
        
        # Notify manager user via DM about new project creation
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'🆕 **New Project Created!**\nProject ID: {new_project_id}\nProject: {str(self.project_name.value).strip()}\nAssigned to: {self.editor} ({self.editor.display_name})\nRate: {str(self.rate.value).strip()}\nDeadline: {format_discord_deadline(deadline_iso) if deadline_iso else "N/A"}\nCreated by: {interaction.user} ({interaction.user.display_name})\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the channel about new project creation
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **New project {new_project_id} created** and assigned to {self.editor.mention} with rate {str(self.rate.value).strip()}.')
        except Exception:
            pass


class AddProjectResourcesModal(Modal, title="Add Resources"):
    def __init__(self, database_path: str, project_id: int):
        super().__init__()
        self.database_path = database_path
        self.project_id = project_id
        self.resource_links = TextInput(
            label="Resource links",
            placeholder="Drive/Dropbox links, brief, etc.",
            required=True,
            style=TextStyle.paragraph,
        )
        self.add_item(self.resource_links)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        resources = str(self.resource_links.value).strip()
        if not resources:
            await interaction.response.send_message('Resources are required.', ephemeral=True)
            return
        # Update DB
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('UPDATE projects SET resource_links = ? WHERE id = ?', (resources, self.project_id))
            await db.commit()
            # Fetch details to rebuild embed
            async with db.execute('SELECT name, start_time, end_time, deadline, rate, assigned_editor_id FROM projects WHERE id = ?', (self.project_id,)) as cur:
                row = await cur.fetchone()
        if not row:
            await interaction.response.send_message('Project not found.', ephemeral=True)
            return
        name, start_time, end_time, deadline, rate, assigned_editor_id = row
        # Post the assignment embed now that resources are known
        embed = discord.Embed(title=f"Project Assignment: {name}", description=f"ID: {self.project_id}", color=discord.Color.blue())
        embed.add_field(name="Editor", value=f"<@{assigned_editor_id}>", inline=False)
        # Attempt to show the editor's profile picture
        avatar_url = None
        try:
            member = interaction.guild.get_member(assigned_editor_id) if interaction.guild else None
            if member and member.display_avatar:
                avatar_url = member.display_avatar.url
            else:
                user = await bot.fetch_user(assigned_editor_id)
                if user and user.display_avatar:
                    avatar_url = user.display_avatar.url
        except Exception:
            avatar_url = None
        embed.add_field(name="Deadline", value=deadline or 'N/A', inline=True)
        embed.add_field(name="Start Time", value=start_time or 'N/A', inline=True)
        embed.add_field(name="End Time", value=end_time or 'N/A', inline=True)
        embed.add_field(name="Rate", value=rate or 'N/A', inline=True)
        embed.add_field(name="Resources", value=resources, inline=False)
        if avatar_url:
            embed.set_thumbnail(url=avatar_url)
        # Build dynamic View Resources button with the actual link
        view = AssignmentButtons(resources_link=resources)
        # Patch the link button's URL if present
        for item in view.children:
            if isinstance(item, Button) and item.label == 'View Resources':
                item.url = resources
        msg = await interaction.channel.send(embed=embed, view=view)
        # Track message for reaction handling
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('INSERT INTO project_messages (message_id, project_id, channel_id) VALUES (?, ?, ?)', (msg.id, self.project_id, interaction.channel.id))
            await db.commit()
        await interaction.response.send_message('Resources saved and assignment posted.', ephemeral=True)


class CollectResourcesView(View):
    def __init__(self, database_path: str, project_id: int):
        super().__init__(timeout=300)
        self.database_path = database_path
        self.project_id = project_id

    @discord.ui.button(label="Add Resources", style=discord.ButtonStyle.primary, emoji="🧩")
    async def open_modal(self, interaction: discord.Interaction, button: Button):
        await interaction.response.send_modal(AddProjectResourcesModal(self.database_path, self.project_id))


@bot.tree.command(name="add_project_ui", description="Assign an editor, then open a form to add a new project")
async def slash_add_project_ui(interaction: discord.Interaction, editor: discord.Member):
    await interaction.response.send_modal(AddProjectModal(db_path, editor))

# Modal for Register Editor
class RegisterEditorModal(Modal, title="Register as Editor"):
    def __init__(self, database_path: str):
        super().__init__()
        self.database_path = database_path
        self.name = TextInput(label="Name", required=True, max_length=100)
        self.position = TextInput(label="Position", placeholder="e.g., Video Editor", required=True, max_length=100)
        self.gcash = TextInput(label="GCash Number", placeholder="09XXXXXXXXX or reference", required=True, max_length=200)
        self.gcash_qr_url = TextInput(label="GCash QR Code URL (optional)", placeholder="https://...", required=False, max_length=300)
        self.email = TextInput(label="Email", required=True, max_length=200)
        
        # Keep within 5 inputs total; default max_projects to 3 automatically
        self.add_item(self.name)
        self.add_item(self.position)
        self.add_item(self.gcash)
        self.add_item(self.gcash_qr_url)
        self.add_item(self.email)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        avatar_url = interaction.user.avatar.url if interaction.user.avatar else interaction.user.default_avatar.url
        max_projects = 3  # defaulted (no field in modal now)
        gcash_qr_url_value = str(self.gcash_qr_url.value).strip() if self.gcash_qr_url.value else None
        
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT user_id FROM editors WHERE user_id = ?', (user_id,)) as cursor:
                if await cursor.fetchone():
                    await interaction.response.send_message('You are already registered as an editor.', ephemeral=True)
                    return
            await db.execute(
                'INSERT INTO editors (user_id, name, position, gcash, email, avatar_url, max_concurrent_projects, gcash_qr_url) VALUES (?, ?, ?, ?, ?, ?, ?, ?)',
                (user_id, str(self.name.value).strip(), str(self.position.value).strip(), str(self.gcash.value).strip(), str(self.email.value).strip(), avatar_url, max_projects, gcash_qr_url_value),
            )
            await db.commit()
        
        embed = discord.Embed(
            title="🎉 Registration Successful!", 
            description=f"Welcome to the team, **{str(self.name.value).strip()}**!",
            color=discord.Color.green()
        )
        embed.add_field(name="Position", value=str(self.position.value).strip(), inline=True)
        embed.add_field(name="Max Projects", value=f"{max_projects} concurrent", inline=True)
        embed.add_field(name="Status", value="✅ Available", inline=True)
        embed.add_field(name="GCash", value=str(self.gcash.value).strip(), inline=False)
        if gcash_qr_url_value:
            try:
                embed.set_image(url=gcash_qr_url_value)
            except Exception:
                pass
        embed.set_footer(text="You can now be assigned to projects!")
        
        await interaction.response.send_message(embed=embed, ephemeral=True)
        
        # Notify manager user via DM about new editor registration
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'👤 **New Editor Registered!**\nName: {str(self.name.value).strip()}\nPosition: {str(self.position.value).strip()}\nGCash: {str(self.gcash.value).strip()}\nUser: {interaction.user} ({interaction.user.display_name})\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the channel about new editor registration
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **New editor registered**: {interaction.user.mention} as {str(self.name.value).strip()} ({str(self.position.value).strip()})!')
        except Exception:
            pass


@bot.tree.command(name="register_editor_ui", description="Open a form to register as an editor")
async def slash_register_editor_ui(interaction: discord.Interaction):
    await interaction.response.send_modal(RegisterEditorModal(db_path))


async def check_editor_availability(editor_id: int, database_path: str) -> dict:
    """Check if an editor is available for new projects"""
    async with aiosqlite.connect(database_path) as db:
        # Get editor info
        async with db.execute('''
            SELECT max_concurrent_projects, current_projects, availability_status 
            FROM editors WHERE user_id = ?
        ''', (editor_id,)) as cursor:
            editor_row = await cursor.fetchone()
            if not editor_row:
                return {"available": False, "reason": "Editor not registered"}
            
            max_projects, current_projects, status = editor_row
            
            # Check if manually set to unavailable
            if status == "unavailable":
                return {"available": False, "reason": "Editor marked as unavailable"}
            
            # Check if at capacity
            if current_projects >= max_projects:
                return {"available": False, "reason": f"At capacity ({current_projects}/{max_projects} projects)"}
            
            # Check active projects
            async with db.execute('''
                SELECT COUNT(*) FROM projects 
                WHERE assigned_editor_id = ? AND status IN ('assigned', 'agreed', 'in_progress')
            ''', (editor_id,)) as cursor:
                active_count = (await cursor.fetchone())[0]
            
            if active_count >= max_projects:
                return {"available": False, "reason": f"Too many active projects ({active_count}/{max_projects})"}
            
            return {
                "available": True, 
                "current_projects": current_projects,
                "max_projects": max_projects,
                "can_take_more": max_projects - active_count
            }


# Modal for Assign Project (paired with member option in slash)
class AssignProjectModal(Modal, title="Assign Project"):
    def __init__(self, database_path: str, editor: discord.Member):
        super().__init__()
        self.database_path = database_path
        self.editor = editor
        self.project_id = TextInput(label="Project ID", placeholder="Numeric ID", required=True, max_length=10)
        self.rate = TextInput(label="Rate", placeholder="e.g., $100", required=True, max_length=50)
        self.deadline = TextInput(label="Deadline (optional, ISO 8601)", placeholder="YYYY-MM-DDTHH:MM", required=False, max_length=25)
        self.notes = TextInput(label="Notes (optional)", required=False, style=TextStyle.paragraph, max_length=1000)
        self.add_item(self.project_id)
        self.add_item(self.rate)
        self.add_item(self.deadline)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        editor = self.editor
        
        # Check editor availability first
        availability = await check_editor_availability(editor.id, self.database_path)
        if not availability["available"]:
            await interaction.response.send_message(
                f'❌ Cannot assign to {editor.mention}: {availability["reason"]}', 
                ephemeral=True
            )
            return
        
        try:
            pid = int(str(self.project_id.value).strip())
        except Exception:
            await interaction.response.send_message('Project ID must be a number.', ephemeral=True)
            return

        # Check editor is registered
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT user_id, name, position, avatar_url FROM editors WHERE user_id = ?', (editor.id,)) as cursor:
                editor_row = await cursor.fetchone()
                if not editor_row:
                    await interaction.response.send_message(f'{editor.mention} is not registered as an editor and cannot be assigned.', ephemeral=True)
                    return
                editor_name, editor_position, avatar_url = editor_row[1], editor_row[2], editor_row[3]

        # Check project exists and update
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT name, resource_links, start_time, end_time, notes FROM projects WHERE id = ?', (pid,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message(f'Project with ID {pid} not found.', ephemeral=True)
                    return
                project_name, resource_links, start_time, end_time, existing_notes = row
            deadline_val = str(self.deadline.value).strip() or None
            await db.execute(
                'UPDATE projects SET assigned_editor_id = ?, rate = ?, status = ?, deadline = ? WHERE id = ?',
                (editor.id, str(self.rate.value).strip(), 'assigned', deadline_val, pid),
            )
            await db.commit()
            
            # Update editor's current project count
            await db.execute('''
                UPDATE editors 
                SET current_projects = current_projects + 1 
                WHERE user_id = ?
            ''', (editor.id,))
            await db.commit()

        # Notify
        try:
            await editor.send(
                f'You have been assigned to project "{project_name}" (ID: {pid}) with rate: {self.rate.value}.\n'
                f'Resources: {resource_links}\nStart: {start_time}\nEnd: {end_time}\nDeadline: {format_discord_deadline(deadline_val) if deadline_val else "N/A"}\nNotes: {existing_notes or self.notes.value}'
            )
        except Exception:
            pass

        embed = discord.Embed(title=f"Project Assignment: {project_name}", description=f"ID: {pid}", color=discord.Color.blue())
        embed.add_field(name="Editor", value=f"{editor.mention} ({editor_name}, {editor_position})", inline=False)
        embed.add_field(name="Rate", value=str(self.rate.value).strip(), inline=True)
        embed.add_field(name="Deadline", value=format_discord_deadline(deadline_val) if deadline_val else 'N/A', inline=True)
        embed.add_field(name="Start Time", value=start_time or 'N/A', inline=True)
        embed.add_field(name="End Time", value=end_time or 'N/A', inline=True)
        embed.add_field(name="Resources", value=resource_links, inline=False)
        if existing_notes or self.notes.value:
            embed.add_field(name="Notes", value=(existing_notes or '') + ("\n" + str(self.notes.value).strip() if self.notes.value else ''), inline=False)
        try:
            if editor_row[3]:
                embed.set_thumbnail(url=editor_row[3])
        except Exception:
            pass

        await interaction.response.send_message("Assignment posted.", ephemeral=True)
        msg = await interaction.channel.send(embed=embed)
        await msg.add_reaction("✅")
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('INSERT INTO project_messages (message_id, project_id) VALUES (?, ?)', (msg.id, pid))
            await db.commit()
        
        # Notify manager user via DM about project assignment
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'📋 **Project Assigned!**\nProject ID: {pid}\nProject: {project_name}\nAssigned to: {editor} ({editor_name})\nRate: {self.rate.value}\nDeadline: {format_discord_deadline(deadline_val) if deadline_val else "N/A"}\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the channel about project assignment
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Project {pid} has been assigned** to {editor.mention} with rate {self.rate.value}.')
        except Exception:
            pass


@bot.tree.command(name="assign_ui", description="Open a form to assign a project to an editor")
async def slash_assign_ui(interaction: discord.Interaction, editor: discord.Member):
    await interaction.response.send_modal(AssignProjectModal(db_path, editor))


@bot.tree.command(name="assign_project_ui", description="Assign a project to an editor with availability check")
async def slash_assign_project_ui(interaction: discord.Interaction, editor: discord.Member):
    # Check availability first
    availability = await check_editor_availability(editor.id, db_path)
    
    if not availability["available"]:
        embed = discord.Embed(
            title="❌ Editor Unavailable", 
            description=f"{editor.mention} cannot take new projects right now.",
            color=discord.Color.red()
        )
        embed.add_field(name="Reason", value=availability["reason"], inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    # Show availability info and proceed with assignment
    embed = discord.Embed(
        title="✅ Editor Available", 
        description=f"{editor.mention} can take this project.",
        color=discord.Color.green()
    )
    embed.add_field(name="Current Projects", value=f"{availability['current_projects']}/{availability['max_projects']}", inline=True)
    embed.add_field(name="Can Take More", value=f"{availability['can_take_more']} projects", inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)
    await interaction.followup.send_modal(AssignProjectModal(db_path, editor))


@bot.tree.command(name="set_availability", description="Set your availability status")
async def slash_set_availability(interaction: discord.Interaction, status: str):
    """Set availability: available, unavailable, busy"""
    if status.lower() not in ['available', 'unavailable', 'busy']:
        await interaction.response.send_message('Status must be: available, unavailable, or busy', ephemeral=True)
        return
    
    async with aiosqlite.connect(db_path) as db:
        await db.execute('UPDATE editors SET availability_status = ? WHERE user_id = ?', (status.lower(), interaction.user.id))
        await db.commit()
    
    await interaction.response.send_message(f'Availability set to: {status.lower()}', ephemeral=True)


@bot.tree.command(name="check_availability", description="Check editor availability")
async def slash_check_availability(interaction: discord.Interaction, editor: discord.Member):
    """Check if an editor is available for projects"""
    availability = await check_editor_availability(editor.id, db_path)
    
    embed = discord.Embed(title=f"📊 {editor.display_name}'s Availability", color=discord.Color.blue())
    
    if availability["available"]:
        embed.color = discord.Color.green()
        embed.add_field(name="Status", value="✅ Available", inline=True)
        embed.add_field(name="Current Projects", value=f"{availability['current_projects']}/{availability['max_projects']}", inline=True)
        embed.add_field(name="Can Take More", value=f"{availability['can_take_more']} projects", inline=True)
    else:
        embed.color = discord.Color.red()
        embed.add_field(name="Status", value="❌ Unavailable", inline=True)
        embed.add_field(name="Reason", value=availability["reason"], inline=False)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="my_status", description="Check your own availability status")
async def slash_my_status(interaction: discord.Interaction):
    """Check your own availability and project count"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute('''
            SELECT name, position, availability_status, max_concurrent_projects, current_projects 
            FROM editors WHERE user_id = ?
        ''', (interaction.user.id,)) as cursor:
            row = await cursor.fetchone()
            if not row:
                await interaction.response.send_message('You are not registered as an editor.', ephemeral=True)
                return
            
            name, position, status, max_projects, current_projects = row
            
            # Get active project count
            async with db.execute('''
                SELECT COUNT(*) FROM projects 
                WHERE assigned_editor_id = ? AND status IN ('assigned', 'agreed', 'in_progress')
            ''', (interaction.user.id,)) as cursor:
                active_count = (await cursor.fetchone())[0]
            
            embed = discord.Embed(title=f"📊 {name}'s Status", color=discord.Color.blue())
            embed.add_field(name="Position", value=position, inline=True)
            embed.add_field(name="Status", value=status.title(), inline=True)
            embed.add_field(name="Projects", value=f"{active_count}/{max_projects}", inline=True)
            
            if active_count >= max_projects:
                embed.color = discord.Color.red()
                embed.add_field(name="⚠️ Warning", value="You are at maximum capacity!", inline=False)
            elif active_count > 0:
                embed.color = discord.Color.orange()
                embed.add_field(name="ℹ️ Info", value=f"You can take {max_projects - active_count} more projects", inline=False)
            else:
                embed.color = discord.Color.green()
                embed.add_field(name="✅ Status", value="Ready for new projects!", inline=False)
            
            await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="search_projects", description="Search projects by status, editor, or other criteria")
async def slash_search_projects(interaction: discord.Interaction, status: Optional[str] = None, editor: Optional[discord.Member] = None):
    """Search projects by various criteria"""
    query = '''
        SELECT p.id, p.name, p.status, p.deadline, p.assigned_editor_id, p.payment_status, p.submission_link,
               e.name as editor_name, e.position as editor_position
        FROM projects p 
        LEFT JOIN editors e ON p.assigned_editor_id = e.user_id
        WHERE 1=1
    '''
    params = []
    
    if status:
        query += ' AND p.status = ?'
        params.append(status.lower())
    
    if editor:
        query += ' AND p.assigned_editor_id = ?'
        params.append(editor.id)
    
    query += ' ORDER BY p.id DESC LIMIT 25'
    
    async with aiosqlite.connect(db_path) as db:
        async with db.execute(query, params) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        embed = discord.Embed(
            title="🔍 Project Search Results", 
            description="No projects found matching your criteria.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title="🔍 Project Search Results", 
        description=f"Found {len(rows)} project(s):",
        color=discord.Color.blue()
    )
    
    for row in rows:
        pid, name, pstatus, deadline, assigned_editor_id, payment_status, submission_link, editor_name, editor_position = row
        
        field_value = f"**Status:** {pstatus.title()}\n"
        if assigned_editor_id and editor_name:
            field_value += f"**Editor:** {editor_name} ({editor_position})\n"
        else:
            field_value += f"**Editor:** Unassigned\n"
        field_value += f"**Deadline:** {deadline or 'N/A'}\n"
        field_value += f"**Payment:** {payment_status.title()}\n"
        
        if submission_link:
            field_value += f"**📤 Submitted:** Yes\n"
        else:
            field_value += f"**📤 Submitted:** No\n"
        
        embed.add_field(
            name=f"📁 Project {pid}: {name}", 
            value=field_value, 
            inline=False
        )
    
    embed.set_footer(text="Use /project_details <id> for more information")
    await interaction.response.send_message(embed=embed, ephemeral=True)


# Modal for Submit
class SubmitModal(Modal, title="Submit Project"):
    def __init__(self, database_path: str):
        super().__init__()
        self.database_path = database_path
        self.project_id = TextInput(label="Project ID", required=True, max_length=10)
        self.submission_link = TextInput(label="Submission link", required=True, style=TextStyle.paragraph)
        self.add_item(self.project_id)
        self.add_item(self.submission_link)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pid = int(str(self.project_id.value).strip())
        except Exception:
            await interaction.response.send_message('Project ID must be a number.', ephemeral=True)
            return
        user_id = interaction.user.id
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT assigned_editor_id, status FROM projects WHERE id = ?', (pid,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message(f'Project with ID {pid} not found.', ephemeral=True)
                    return
                assigned_editor_id, status = row
            if assigned_editor_id != user_id:
                await interaction.response.send_message('You are not assigned to this project.', ephemeral=True)
                return
            if status not in ('assigned', 'agreed'):
                await interaction.response.send_message('This project cannot be submitted in its current status.', ephemeral=True)
                return
            await db.execute('UPDATE projects SET submission_link = ?, status = ? WHERE id = ?', (str(self.submission_link.value).strip(), 'submitted', pid))
            await db.commit()
        await interaction.response.send_message(f'Project {pid} submitted!', ephemeral=True)
        await interaction.channel.send(f'<@{interaction.user.id}> has submitted project {pid}: {str(self.submission_link.value).strip()}')
        
        # Notify manager user via DM about new project submission
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'📤 **New Project Submission!**\nProject ID: {pid}\nSubmitted by: {interaction.user} ({interaction.user.display_name})\nSubmission: {str(self.submission_link.value).strip()}\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Also DM all members of the manager role, if configured
        try:
            if MANAGER_ROLE_ID and interaction.guild:
                role = interaction.guild.get_role(MANAGER_ROLE_ID)
                if role:
                    for member in role.members:
                        try:
                            await member.send(f'📤 **New Project Submission!**\nProject ID: {pid}\nSubmitted by: {interaction.user} ({interaction.user.display_name})\nSubmission: {str(self.submission_link.value).strip()}\nChannel: {interaction.channel.mention}')
                        except Exception:
                            continue
        except Exception:
            pass
        
        # Notify managers in the channel about new project submission
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **New project submission** received for project {pid} from {interaction.user.mention}.')
        except Exception:
            pass


@bot.tree.command(name="submit_ui", description="Open a form to submit a project")
async def slash_submit_ui(interaction: discord.Interaction):
    await interaction.response.send_modal(SubmitModal(db_path))


# Modal for Approve
class ApproveModal(Modal, title="Approve Project"):
    def __init__(self, database_path: str):
        super().__init__()
        self.database_path = database_path
        self.project_id = TextInput(label="Project ID", required=True, max_length=10)
        self.add_item(self.project_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pid = int(str(self.project_id.value).strip())
        except Exception:
            await interaction.response.send_message('Project ID must be a number.', ephemeral=True)
            return
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT assigned_editor_id, status FROM projects WHERE id = ?', (pid,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message(f'Project with ID {pid} not found.', ephemeral=True)
                    return
                assigned_editor_id, status = row
            if status != 'submitted':
                await interaction.response.send_message('This project is not in a submitted state.', ephemeral=True)
                return
            await db.execute('UPDATE projects SET status = ? WHERE id = ?', ('approved', pid))
            await db.commit()
        editor = interaction.guild.get_member(assigned_editor_id) if interaction.guild else None
        if editor:
            try:
                await editor.send(f'Your submission for project {pid} has been approved!')
            except Exception:
                pass
        await interaction.response.send_message(f'Project {pid} approved!', ephemeral=True)
        
        # Notify manager user via DM about project approval
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'✅ **Project Approved!**\nProject ID: {pid}\nApproved by: {interaction.user} ({interaction.user.display_name})\nStatus: Approved\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the channel about project approval
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Project {pid} has been approved** by {interaction.user.mention}! 🎉')
        except Exception:
            pass


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="approve_ui", description="Open a form to approve a submitted project")
async def slash_approve_ui(interaction: discord.Interaction):
    await interaction.response.send_modal(ApproveModal(db_path))


# Modal for Reject
class RejectModal(Modal, title="Reject Project"):
    def __init__(self, database_path: str):
        super().__init__()
        self.database_path = database_path
        self.project_id = TextInput(label="Project ID", required=True, max_length=10)
        self.reason = TextInput(label="Reason", required=True, style=TextStyle.paragraph, max_length=1000)
        self.add_item(self.project_id)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pid = int(str(self.project_id.value).strip())
        except Exception:
            await interaction.response.send_message('Project ID must be a number.', ephemeral=True)
            return
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT assigned_editor_id, status FROM projects WHERE id = ?', (pid,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message(f'Project with ID {pid} not found.', ephemeral=True)
                    return
                assigned_editor_id, status = row
            if status != 'submitted':
                await interaction.response.send_message('This project is not in a submitted state.', ephemeral=True)
                return
            await db.execute('UPDATE projects SET status = ? WHERE id = ?', ('rejected', pid))
            await db.commit()
        editor = interaction.guild.get_member(assigned_editor_id) if interaction.guild else None
        if editor:
            try:
                await editor.send(f'Your submission for project {pid} has been rejected. Reason: {str(self.reason.value).strip()}')
            except Exception:
                pass
        await interaction.response.send_message(f'Project {pid} rejected. Reason: {str(self.reason.value).strip()}', ephemeral=True)
        
        # Notify manager user via DM about project rejection
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'❌ **Project Rejected!**\nProject ID: {pid}\nRejected by: {interaction.user} ({interaction.user.display_name})\nReason: {str(self.reason.value).strip()}\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the channel about project rejection
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Project {pid} has been rejected** by {interaction.user.mention}. Reason: {str(self.reason.value).strip()}')
        except Exception:
            pass


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="reject_ui", description="Open a form to reject a submitted project")
async def slash_reject_ui(interaction: discord.Interaction):
    await interaction.response.send_modal(RejectModal(db_path))


# Modal for List Projects filters
class ListProjectsModal(Modal, title="List Projects Filters"):
    def __init__(self, database_path: str):
        super().__init__()
        self.database_path = database_path
        self.status = TextInput(label="Status (optional)", required=False, max_length=50)
        self.editor_id = TextInput(label="Editor ID (optional)", placeholder="Numeric Discord ID", required=False, max_length=25)
        self.add_item(self.status)
        self.add_item(self.editor_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        status_val = str(self.status.value).strip() or None
        editor_id_val = str(self.editor_id.value).strip()
        editor_id_num: Optional[int] = None
        if editor_id_val:
            try:
                editor_id_num = int(editor_id_val)
            except Exception:
                await interaction.response.send_message('Editor ID must be a number.', ephemeral=True)
                return
        query = 'SELECT id, name, status, deadline, assigned_editor_id, payment_status FROM projects'
        params = []
        filters = []
        if status_val:
            filters.append('status = ?')
            params.append(status_val)
        if editor_id_num is not None:
            filters.append('assigned_editor_id = ?')
            params.append(editor_id_num)
        if filters:
            query += ' WHERE ' + ' AND '.join(filters)
        query += ' ORDER BY deadline IS NULL, deadline'
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute(query, params) as cursor:
                rows = await cursor.fetchall()
        if not rows:
            await interaction.response.send_message('No projects found.', ephemeral=True)
            return
        embed = discord.Embed(title="Project List", color=discord.Color.green())
        for row in rows:
            pid, name, pstatus, deadline, assigned_editor_id, payment_status = row
            editor_mention = f'<@{assigned_editor_id}>' if assigned_editor_id else 'Unassigned'
            embed.add_field(name=f'ID: {pid} | {name}', value=f'Status: {pstatus}\nDeadline: {format_discord_deadline(deadline) if deadline else "N/A"}\nEditor: {editor_mention}\nPayment: {payment_status}', inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="list_projects_ui", description="Open a form to filter the project list")
async def slash_list_projects_ui(interaction: discord.Interaction):
    await interaction.response.send_modal(ListProjectsModal(db_path))


# Modal for Mark Paid
class MarkPaidModal(Modal, title="Mark Project Paid"):
    def __init__(self, database_path: str):
        super().__init__()
        self.database_path = database_path
        self.project_id = TextInput(label="Project ID", required=True, max_length=10)
        self.add_item(self.project_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pid = int(str(self.project_id.value).strip())
        except Exception:
            await interaction.response.send_message('Project ID must be a number.', ephemeral=True)
            return
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('UPDATE projects SET payment_status = ? WHERE id = ?', ('paid', pid))
            await db.commit()
        await interaction.response.send_message(f'Project {pid} marked as paid.', ephemeral=True)
        
        # Notify manager user via DM about project marked as paid
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'💰 **Project Marked as Paid!**\nProject ID: {pid}\nMarked by: {interaction.user} ({interaction.user.display_name})\nStatus: Paid\nChannel: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the channel about project marked as paid
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Project {pid} has been marked as paid** by {interaction.user.mention}! 💰')
        except Exception:
            pass


@app_commands.default_permissions(manage_messages=True)
@bot.tree.command(name="mark_paid_ui", description="Open a form to mark a project as paid")
async def slash_mark_paid_ui(interaction: discord.Interaction):
    await interaction.response.send_modal(MarkPaidModal(db_path))


class AssignmentButtons(View):
    def __init__(self, resources_link: Optional[str] = None):
        super().__init__(timeout=None)
        self.resources_link = resources_link or ""
        # Add a link-style button if resources link is available
        if self.resources_link:
            link_btn = Button(label="View Resources", style=discord.ButtonStyle.link, url=self.resources_link, row=1)
            self.add_item(link_btn)

    @discord.ui.button(label="Accept", style=discord.ButtonStyle.success, emoji="✅", custom_id="assign_accept")
    async def accept(self, interaction: discord.Interaction, button: Button):
        # Lookup project from the tracked message
        async with aiosqlite.connect(db_path) as db:
            async with db.execute('SELECT project_id FROM project_messages WHERE message_id = ?', (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message("This message is not tracked.", ephemeral=True)
                    return
                project_id = row[0]
            async with db.execute('SELECT assigned_editor_id, resource_links, name, rate, deadline FROM projects WHERE id = ?', (project_id,)) as cursor:
                p = await cursor.fetchone()
                if not p:
                    await interaction.response.send_message("Project not found.", ephemeral=True)
                    return
                assigned_editor_id, resources_link, project_name, rate, deadline = p
            if interaction.user.id != assigned_editor_id:
                await interaction.response.send_message("Only the assigned editor can accept.", ephemeral=True)
                return
            # Move status: assigned -> agreed -> in_progress
            await db.execute('UPDATE projects SET status = ? WHERE id = ?', ('agreed', project_id))
            await db.commit()
            await db.execute('UPDATE projects SET status = ?, started_at = ? WHERE id = ?', ('in_progress', datetime.utcnow().isoformat(timespec='minutes'), project_id))
            await db.commit()
        thread = await interaction.message.create_thread(name=f"Project {project_id} - {interaction.user.display_name}")
        await interaction.response.send_message("Accepted. A thread was created.", ephemeral=True)
        await thread.send(
            f"{interaction.user.mention} accepted project ID {project_id}. Please start the project. Use this thread for updates and questions.\nResources: {self.resources_link}",
            view=ThreadActionsView(project_id),
        )
        
        # Enhanced notification to manager user via DM about project acceptance
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            notification_msg = f'🎯 **Project Accepted & Started!**\n\n'
            notification_msg += f'**Project ID:** {project_id}\n'
            notification_msg += f'**Project Name:** {project_name}\n'
            notification_msg += f'**Editor:** {interaction.user} ({interaction.user.display_name})\n'
            notification_msg += f'**Rate:** {rate or "N/A"}\n'
            notification_msg += f'**Deadline:** {format_discord_deadline(deadline) if deadline else "N/A"}\n'
            notification_msg += f'**Status:** In Progress\n'
            notification_msg += f'**Thread:** {thread.mention}\n'
            notification_msg += f'**Channel:** {interaction.channel.mention}\n\n'
            notification_msg += f'**Action:** Project is now in progress. Monitor the thread for updates.'
            await mgr.send(notification_msg)
        except Exception:
            pass
        
        # Enhanced notification to managers in the original channel about project acceptance
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                embed = discord.Embed(
                    title="🎯 Project Accepted & Started!", 
                    description=f"**Project {project_id}** has been accepted by {interaction.user.mention}",
                    color=discord.Color.green()
                )
                embed.add_field(name="Project Name", value=project_name, inline=True)
                embed.add_field(name="Editor", value=f"{interaction.user.display_name}", inline=True)
                embed.add_field(name="Rate", value=rate or "N/A", inline=True)
                embed.add_field(name="Status", value="🚀 In Progress", inline=True)
                embed.add_field(name="Thread", value=thread.mention, inline=True)
                embed.add_field(name="Deadline", value=format_discord_deadline(deadline) if deadline else "N/A", inline=True)
                embed.set_footer(text=f"Project started at {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}")
                await interaction.message.reply(f'{mention}:', embed=embed)
        except Exception:
            pass

    @discord.ui.button(label="Decline", style=discord.ButtonStyle.danger, emoji="🚫", custom_id="assign_decline")
    async def decline(self, interaction: discord.Interaction, button: Button):
        async with aiosqlite.connect(db_path) as db:
            async with db.execute('SELECT project_id FROM project_messages WHERE message_id = ?', (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message("This message is not tracked.", ephemeral=True)
                    return
                project_id = row[0]
            async with db.execute('SELECT assigned_editor_id, name, rate, deadline FROM projects WHERE id = ?', (project_id,)) as cursor:
                p = await cursor.fetchone()
                if not p:
                    await interaction.response.send_message("Project not found.", ephemeral=True)
                    return
                assigned_editor_id, project_name, rate, deadline = p
            if interaction.user.id != assigned_editor_id:
                await interaction.response.send_message("Only the assigned editor can decline.", ephemeral=True)
                return
            await db.execute('UPDATE projects SET status = ?, assigned_editor_id = NULL, rate = NULL WHERE id = ?', ('unassigned', project_id))
            await db.commit()
        thread = await interaction.message.create_thread(name=f"Project {project_id} - Declined")
        await interaction.response.send_message("Declined. A thread was created.", ephemeral=True)
        await thread.send(f"{interaction.user.mention} declined project ID {project_id}. The project has been unassigned and is open for reassignment.")
        
        # Enhanced notification to manager user via DM about project decline
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            notification_msg = f'❌ **Project Declined!**\n\n'
            notification_msg += f'**Project ID:** {project_id}\n'
            notification_msg += f'**Project Name:** {project_name}\n'
            notification_msg += f'**Declined by:** {interaction.user} ({interaction.user.display_name})\n'
            notification_msg += f'**Previous Rate:** {rate or "N/A"}\n'
            notification_msg += f'**Deadline:** {format_discord_deadline(deadline) if deadline else "N/A"}\n'
            notification_msg += f'**Status:** Unassigned\n'
            notification_msg += f'**Thread:** {thread.mention}\n'
            notification_msg += f'**Channel:** {interaction.channel.mention}\n\n'
            notification_msg += f'**Action Required:** Project needs to be reassigned to another editor.'
            await mgr.send(notification_msg)
        except Exception:
            pass
        
        # Enhanced notification to managers in the original channel about project decline
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                embed = discord.Embed(
                    title="❌ Project Declined!", 
                    description=f"**Project {project_id}** has been declined by {interaction.user.mention}",
                    color=discord.Color.red()
                )
                embed.add_field(name="Project Name", value=project_name, inline=True)
                embed.add_field(name="Declined by", value=f"{interaction.user.display_name}", inline=True)
                embed.add_field(name="Previous Rate", value=rate or "N/A", inline=True)
                embed.add_field(name="Status", value="⏳ Unassigned", inline=True)
                embed.add_field(name="Thread", value=thread.mention, inline=True)
                embed.add_field(name="Deadline", value=format_discord_deadline(deadline) if deadline else "N/A", inline=True)
                embed.set_footer(text="⚠️ Action Required: Project needs to be reassigned to another editor")
                await interaction.message.reply(f'{mention}:', embed=embed)
        except Exception:
            pass

    @discord.ui.button(label="Mark In Progress", style=discord.ButtonStyle.secondary, emoji="🚀", custom_id="assign_mark_in_progress", row=1)
    async def mark_in_progress(self, interaction: discord.Interaction, button: Button):
        async with aiosqlite.connect(db_path) as db:
            async with db.execute('SELECT project_id FROM project_messages WHERE message_id = ?', (interaction.message.id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message("This message is not tracked.", ephemeral=True)
                    return
                project_id = row[0]
            async with db.execute('SELECT assigned_editor_id FROM projects WHERE id = ?', (project_id,)) as cursor:
                r = await cursor.fetchone()
                if not r:
                    await interaction.response.send_message("Project not found.", ephemeral=True)
                    return
                assigned_editor_id = r[0]
            if interaction.user.id != assigned_editor_id:
                await interaction.response.send_message("Only the assigned editor can mark in progress.", ephemeral=True)
                return
            await db.execute('UPDATE projects SET status = ?, started_at = ? WHERE id = ?', ('in_progress', datetime.utcnow().isoformat(timespec='minutes'), project_id))
            await db.commit()
        await interaction.response.send_message('Marked as In Progress.', ephemeral=True)
        
        # Notify manager user via DM about project marked in progress
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'🚀 **Project In Progress!**\nProject ID: {project_id}\nMarked by: {interaction.user} ({interaction.user.display_name})\nStatus: In Progress')
        except Exception:
            pass
        
        # Notify managers in the original channel about project marked in progress
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.message.reply(f'{mention}: **Project {project_id} has been marked as In Progress** by {interaction.user.mention}.')
        except Exception:
            pass

@bot.event
async def on_reaction_add(reaction, user):
    # Reactions no longer used for actions; buttons handle Accept/Decline
    return

 

async def deadline_reminder_task():
    await bot.wait_until_ready()
    while not bot.is_closed():
        now = datetime.utcnow()
        async with aiosqlite.connect(db_path) as db:
            async with db.execute('''
                SELECT id, name, deadline, assigned_editor_id, status FROM projects
                WHERE deadline IS NOT NULL AND status IN ('assigned', 'agreed', 'in_progress')
            ''') as cursor:
                rows = await cursor.fetchall()
            for pid, name, deadline_str, editor_id, status in rows:
                try:
                    deadline_dt = datetime.fromisoformat(deadline_str)
                except Exception:
                    continue
                time_left = deadline_dt - now
                editor = None
                for guild in bot.guilds:
                    member = guild.get_member(editor_id)
                    if member:
                        editor = member
                        break
                if not editor:
                    continue
                # Reminder if within 24 hours
                if timedelta(hours=0) < time_left <= timedelta(hours=24):
                    try:
                        await editor.send(f'Reminder: Project "{name}" (ID: {pid}) is due in {time_left}. Please submit before the deadline: {deadline_str}')
                    except Exception:
                        pass
                # Overdue notification
                elif time_left < timedelta(0):
                    try:
                        await editor.send(f'Project "{name}" (ID: {pid}) is overdue! Deadline was: {deadline_str}')
                    except Exception:
                        pass
                    # Notify manager in the first text channel
                    for guild in bot.guilds:
                        channel = discord.utils.get(guild.text_channels, permissions__send_messages=True)
                        if channel:
                            await channel.send(f'Project "{name}" (ID: {pid}) assigned to <@{editor_id}> is overdue!')
                            break
        await asyncio.sleep(600)  # Check every 10 minutes


async def update_editor_project_counts():
    """Background task to keep editor project counts accurate"""
    await bot.wait_until_ready()
    while not bot.is_closed():
        async with aiosqlite.connect(db_path) as db:
            # Update current project counts for all editors
            await db.execute('''
                UPDATE editors 
                SET current_projects = (
                    SELECT COUNT(*) FROM projects 
                    WHERE assigned_editor_id = editors.user_id 
                    AND status IN ('assigned', 'agreed', 'in_progress')
                )
            ''')
            await db.commit()
        
        await asyncio.sleep(300)  # Update every 5 minutes

# More commands will be added here

@bot.tree.command(name="list_submitted", description="List all submitted projects awaiting review")
async def slash_list_submitted(interaction: discord.Interaction):
    """List all projects that have been submitted and are awaiting review"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute('''
            SELECT p.id, p.name, p.submission_link, p.assigned_editor_id, p.deadline, p.rate,
                   e.name as editor_name, e.position as editor_position
            FROM projects p 
            LEFT JOIN editors e ON p.assigned_editor_id = e.user_id
            WHERE p.status = 'submitted'
            ORDER BY p.id DESC
        ''') as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        embed = discord.Embed(
            title="📋 Submitted Projects", 
            description="No projects are currently awaiting review.",
            color=discord.Color.green()
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return
    
    embed = discord.Embed(
        title="📋 Submitted Projects Awaiting Review", 
        description=f"Found {len(rows)} project(s) awaiting review:",
        color=discord.Color.orange()
    )
    
    for row in rows:
        pid, name, submission_link, editor_id, deadline, rate, editor_name, editor_position = row
        
        # Truncate submission link for display
        display_link = submission_link[:100] + "..." if len(submission_link) > 100 else submission_link
        
        field_value = f"**Editor:** {editor_name} ({editor_position})\n"
        field_value += f"**Rate:** {rate or 'N/A'}\n"
        field_value += f"**Deadline:** {format_discord_deadline(deadline) if deadline else 'N/A'}\n"
        field_value += f"**Submission:** {display_link}"
        
        embed.add_field(
            name=f"📁 Project {pid}: {name}", 
            value=field_value, 
            inline=False
        )
    
    embed.set_footer(text="Use /approve_ui or /reject_ui to review these projects")
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="project_details", description="Get detailed information about a specific project")
async def slash_project_details(interaction: discord.Interaction, project_id: int):
    """Get detailed information about a specific project including submission details"""
    async with aiosqlite.connect(db_path) as db:
        async with db.execute('''
            SELECT p.*, e.name as editor_name, e.position as editor_position, e.email as editor_email
            FROM projects p 
            LEFT JOIN editors e ON p.assigned_editor_id = e.user_id
            WHERE p.id = ?
        ''', (project_id,)) as cursor:
            row = await cursor.fetchone()
    
    if not row:
        await interaction.response.send_message(f'Project with ID {project_id} not found.', ephemeral=True)
        return
    
    # Unpack all the data
    (pid, name, resource_links, start_time, end_time, deadline, notes, 
     assigned_editor_id, rate, status, submission_link, payment_status, 
     thread_id, created_by, started_at, editor_name, editor_position, editor_email) = row
    
    embed = discord.Embed(
        title=f"📊 Project Details: {name}", 
        description=f"**ID:** {pid} | **Status:** {status.title()}",
        color=discord.Color.blue()
    )
    
    # Basic project info
    embed.add_field(name="Project Name", value=name, inline=True)
    embed.add_field(name="Status", value=status.title(), inline=True)
    embed.add_field(name="Payment Status", value=payment_status.title(), inline=True)
    
    # Editor info
    if assigned_editor_id:
        embed.add_field(name="Assigned Editor", value=f"<@{assigned_editor_id}>\n{editor_name} ({editor_position})", inline=True)
        if editor_email:
            embed.add_field(name="Editor Email", value=editor_email, inline=True)
    else:
        embed.add_field(name="Assigned Editor", value="Unassigned", inline=True)
    
    # Timing info
    embed.add_field(name="Start Time", value=start_time or 'N/A', inline=True)
    embed.add_field(name="End Time", value=end_time or 'N/A', inline=True)
    embed.add_field(name="Deadline", value=format_discord_deadline(deadline) if deadline else 'N/A', inline=True)
    
    # Financial info
    embed.add_field(name="Rate", value=rate or 'N/A', inline=True)
    
    # Resources and submission
    if resource_links:
        embed.add_field(name="Resources", value=resource_links[:1024] or 'N/A', inline=False)
    
    if submission_link:
        embed.add_field(name="📤 Submission Link", value=submission_link[:1024], inline=False)
    
    if notes:
        embed.add_field(name="Notes", value=notes[:1024], inline=False)
    
    # Thread info
    if thread_id:
        embed.add_field(name="Thread", value=f"<#{thread_id}>", inline=True)
    
    # Created info
    if created_by:
        embed.add_field(name="Created By", value=f"<@{created_by}>", inline=True)
    
    if started_at:
        embed.add_field(name="Started At", value=started_at, inline=True)
    
    await interaction.response.send_message(embed=embed, ephemeral=True)


class ThreadActionsView(View):
    def __init__(self, project_id: int):
        super().__init__(timeout=None)
        self.project_id = project_id
        submit_button = Button(label="Submit", style=discord.ButtonStyle.primary, emoji="📤")
        submit_button.callback = self.open_submit_modal
        self.add_item(submit_button)
        extend_button = Button(label="Request Extension", style=discord.ButtonStyle.secondary, emoji="⏳")
        extend_button.callback = self.open_extension_modal
        self.add_item(extend_button)
        manager_button = Button(label="Manager Controls", style=discord.ButtonStyle.secondary, emoji="🛠️")
        manager_button.callback = self.open_manager_controls
        self.add_item(manager_button)

    async def open_submit_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(SubmitProjectModal(db_path, self.project_id))

    async def open_extension_modal(self, interaction: discord.Interaction):
        await interaction.response.send_modal(ExtensionRequestModal(db_path, self.project_id))

    async def open_manager_controls(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message('Only managers can open controls.', ephemeral=True)
            return
        await interaction.response.send_message('Manager controls', view=ManagerControlsView(self.project_id), ephemeral=True)


class SubmitProjectModal(Modal, title="Submit Project"):
    def __init__(self, database_path: str, project_id: int):
        super().__init__()
        self.database_path = database_path
        self.project_id = project_id
        self.submission_link = TextInput(label="Submission link", required=True, style=TextStyle.paragraph)
        self.attachment = TextInput(label="Attachment URL (optional)", required=False, style=TextStyle.short)
        self.notes = TextInput(label="Notes (optional)", required=False, style=TextStyle.paragraph, max_length=500)
        self.add_item(self.submission_link)
        self.add_item(self.attachment)
        self.add_item(self.notes)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        user_id = interaction.user.id
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT assigned_editor_id, status FROM projects WHERE id = ?', (self.project_id,)) as cursor:
                row = await cursor.fetchone()
                if not row:
                    await interaction.response.send_message(f'Project with ID {self.project_id} not found.', ephemeral=True)
                    return
                assigned_editor_id, status = row
            if assigned_editor_id != user_id:
                await interaction.response.send_message('You are not assigned to this project.', ephemeral=True)
                return
            if status not in ('assigned', 'agreed', 'in_progress'):
                await interaction.response.send_message('This project cannot be submitted in its current status.', ephemeral=True)
                return
            payload = str(self.submission_link.value).strip()
            if self.attachment.value:
                payload += f"\nAttachment: {str(self.attachment.value).strip()}"
            if self.notes.value:
                payload += f"\nNotes: {str(self.notes.value).strip()}"
            await db.execute('UPDATE projects SET submission_link = ?, status = ? WHERE id = ?', (payload, 'submitted', self.project_id))
            await db.commit()
            
            # Update editor's current project count (project is no longer "in progress")
            await db.execute('''
                UPDATE editors 
                SET current_projects = current_projects - 1 
                WHERE user_id = ? AND current_projects > 0
            ''', (user_id,))
            await db.commit()
        await interaction.response.send_message(f'Project {self.project_id} submitted!', ephemeral=True)
        
        # Get project details for enhanced notification
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('''
                SELECT p.name, p.rate, p.deadline, p.resource_links, e.name as editor_name, e.position as editor_position
                FROM projects p 
                LEFT JOIN editors e ON p.assigned_editor_id = e.user_id
                WHERE p.id = ?
            ''', (self.project_id,)) as cursor:
                project_details = await cursor.fetchone()
        
        if project_details:
            project_name, rate, deadline, resources, editor_name, editor_position = project_details
            
            # Enhanced notification to manager user via DM
            try:
                mgr = await bot.fetch_user(NOTIFY_USER_ID)
                notification_msg = f'📤 **New Project Submission!**\n\n'
                notification_msg += f'**Project ID:** {self.project_id}\n'
                notification_msg += f'**Project Name:** {project_name}\n'
                notification_msg += f'**Editor:** {interaction.user} ({editor_name} - {editor_position})\n'
                notification_msg += f'**Rate:** {rate or "N/A"}\n'
                notification_msg += f'**Deadline:** {format_discord_deadline(deadline) if deadline else "N/A"}\n'
                notification_msg += f'**Status:** Submitted\n'
                notification_msg += f'**Thread:** {interaction.channel.mention}\n\n'
                notification_msg += f'**📤 Submission Details:**\n{payload}\n\n'
                notification_msg += f'**Action Required:** Review and approve/reject this submission.'
                await mgr.send(notification_msg)
            except Exception:
                pass
            
            # Also DM all members of the manager role, if configured
            try:
                if MANAGER_ROLE_ID and interaction.guild:
                    role = interaction.guild.get_role(MANAGER_ROLE_ID)
                    if role:
                        for member in role.members:
                            try:
                                await member.send(notification_msg)
                            except Exception:
                                continue
            except Exception:
                pass
            
            # Enhanced notification to managers in thread
            try:
                if MANAGER_ROLE_ID:
                    mention = f'<@&{MANAGER_ROLE_ID}>'
                    embed = discord.Embed(
                        title="📤 Project Submitted!", 
                        description=f"**Project {self.project_id}** has been submitted by {interaction.user.mention}",
                        color=discord.Color.blue()
                    )
                    embed.add_field(name="Project Name", value=project_name, inline=True)
                    embed.add_field(name="Editor", value=f"{editor_name} ({editor_position})", inline=True)
                    embed.add_field(name="Rate", value=rate or "N/A", inline=True)
                    embed.add_field(name="Status", value="📤 Submitted", inline=True)
                    embed.add_field(name="Deadline", value=format_discord_deadline(deadline) if deadline else "N/A", inline=True)
                    embed.add_field(name="Resources", value=resources[:100] + "..." if len(resources) > 100 else resources, inline=False)
                    embed.add_field(name="📤 Submission", value=payload[:1024], inline=False)
                    embed.set_footer(text="Use Manager Controls to approve/reject or request changes")
                    await interaction.channel.send(f'{mention}:', embed=embed)
                else:
                    await interaction.channel.send(f'**Managers:** Submission received for project {self.project_id} from {interaction.user.mention}.')
            except Exception:
                pass
        else:
            # Fallback notification if project details not found
            try:
                mgr = await bot.fetch_user(NOTIFY_USER_ID)
                await mgr.send(f'📤 **New Project Submission!**\nProject ID: {self.project_id}\nEditor: {interaction.user} ({interaction.user.display_name})\nSubmission: {payload}')
            except Exception:
                pass
            
            try:
                mention = f'<@&{MANAGER_ROLE_ID}>' if MANAGER_ROLE_ID else 'Managers'
                await interaction.channel.send(f'{mention}: Submission received for project {self.project_id} from {interaction.user.mention}.')
            except Exception:
                pass
        
        # Lock thread to freeze further submissions
        try:
            await interaction.channel.edit(locked=True)
        except Exception:
            pass


class ExtensionRequestModal(Modal, title="Request Extension"):
    def __init__(self, database_path: str, project_id: int):
        super().__init__()
        self.database_path = database_path
        self.project_id = project_id
        self.days = TextInput(label="Extra days", placeholder="e.g., 1", required=True, max_length=3)
        self.reason = TextInput(label="Reason", required=True, style=TextStyle.paragraph, max_length=500)
        self.add_item(self.days)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            extra_days = int(str(self.days.value).strip())
            if extra_days <= 0:
                raise ValueError
        except Exception:
            await interaction.response.send_message('Please provide a valid number of days.', ephemeral=True)
            return
        async with aiosqlite.connect(self.database_path) as db:
            async with db.execute('SELECT deadline FROM projects WHERE id = ?', (self.project_id,)) as cur:
                row = await cur.fetchone()
            if not row or not row[0]:
                await interaction.response.send_message('No existing deadline to extend.', ephemeral=True)
                return
            try:
                current_deadline = datetime.fromisoformat(row[0])
            except Exception:
                await interaction.response.send_message('Stored deadline has invalid format.', ephemeral=True)
                return
            new_deadline = current_deadline + timedelta(days=extra_days)
            await db.execute('UPDATE projects SET deadline = ? WHERE id = ?', (new_deadline.isoformat(timespec='minutes'), self.project_id))
            await db.commit()
        await interaction.response.send_message(f'Deadline extended to {format_discord_deadline(new_deadline.isoformat(timespec="minutes"))}', ephemeral=True)
        
        # Notify manager user via DM about deadline extension
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'⏰ **Deadline Extended!**\nProject ID: {self.project_id}\nExtended by: {interaction.user} ({interaction.user.display_name})\nNew Deadline: {format_discord_deadline(new_deadline.isoformat(timespec="minutes"))}\nThread: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the thread about deadline extension
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Deadline extended** for project {self.project_id} by {interaction.user.mention} to {format_discord_deadline(new_deadline.isoformat(timespec="minutes"))}.')
        except Exception:
            pass


class RequestChangesModal(Modal, title="Request Changes"):
    def __init__(self, database_path: str, project_id: int):
        super().__init__()
        self.database_path = database_path
        self.project_id = project_id
        self.reason = TextInput(label="Reason for changes", required=True, style=TextStyle.paragraph, max_length=1000)
        self.add_item(self.reason)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message('Only managers can request changes.', ephemeral=True)
            return
        async with aiosqlite.connect(self.database_path) as db:
            await db.execute('UPDATE projects SET status = ? WHERE id = ?', ('changes_requested', self.project_id))
            await db.commit()
            async with db.execute('SELECT assigned_editor_id FROM projects WHERE id = ?', (self.project_id,)) as cur:
                row = await cur.fetchone()
        if row:
            try:
                user = await bot.fetch_user(row[0])
                await user.send(f'Changes requested for project {self.project_id}: {str(self.reason.value).strip()}')
            except Exception:
                pass
        # Unlock thread and re-enable submit
        try:
            await interaction.channel.edit(locked=False, name=f"🟡 {interaction.channel.name}")
        except Exception:
            pass
        await interaction.response.send_message('Requested changes and re-opened the thread.', ephemeral=True)
        
        # Notify manager user via DM about changes requested
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'🔄 **Changes Requested!**\nProject ID: {self.project_id}\nRequested by: {interaction.user} ({interaction.user.display_name})\nReason: {str(self.reason.value).strip()}\nThread: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the thread about changes requested
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Changes requested** for project {self.project_id} by {interaction.user.mention}. Thread has been re-opened for resubmission.')
        except Exception:
            pass


class ManagerControlsView(View):
    def __init__(self, project_id: int):
        super().__init__(timeout=300)
        self.project_id = project_id
        approve_button = Button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
        approve_button.callback = self.approve
        self.add_item(approve_button)
        changes_button = Button(label="Request Changes", style=discord.ButtonStyle.danger, emoji="✏️")
        changes_button.callback = self.request_changes
        self.add_item(changes_button)

    async def approve(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message('Only managers can approve.', ephemeral=True)
            return
        async with aiosqlite.connect(db_path) as db:
            await db.execute('UPDATE projects SET status = ? WHERE id = ?', ('approved', self.project_id))
            await db.commit()
            async with db.execute('SELECT assigned_editor_id FROM projects WHERE id = ?', (self.project_id,)) as cur:
                row = await cur.fetchone()
        if row:
            try:
                user = await bot.fetch_user(row[0])
                await user.send(f'Your submission for project {self.project_id} has been approved!')
            except Exception:
                pass
        await interaction.response.send_message('Approved.', ephemeral=True)
        try:
            await interaction.channel.edit(locked=True, name=f"✅ {interaction.channel.name}")
        except Exception:
            pass
        
        # Notify manager user via DM about project approval
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f'✅ **Project Approved!**\nProject ID: {self.project_id}\nApproved by: {interaction.user} ({interaction.user.display_name})\nStatus: Approved\nThread: {interaction.channel.mention}')
        except Exception:
            pass
        
        # Notify managers in the thread about project approval
        try:
            if MANAGER_ROLE_ID:
                mention = f'<@&{MANAGER_ROLE_ID}>'
                await interaction.channel.send(f'{mention}: **Project {self.project_id} has been approved** by {interaction.user.mention}! 🎉')
        except Exception:
            pass

    async def request_changes(self, interaction: discord.Interaction):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message('Only managers can request changes.', ephemeral=True)
            return
        await interaction.response.send_modal(RequestChangesModal(db_path, self.project_id))


@bot.tree.command(name="project_summary", description="Get a summary of all projects by status")
async def slash_project_summary(interaction: discord.Interaction):
    """Get a summary of all projects grouped by status"""
    async with aiosqlite.connect(db_path) as db:
        # Get count by status
        async with db.execute('''
            SELECT status, COUNT(*) as count 
            FROM projects 
            GROUP BY status 
            ORDER BY count DESC
        ''') as cursor:
            status_counts = await cursor.fetchall()
        
        # Get count of submitted projects
        async with db.execute('SELECT COUNT(*) FROM projects WHERE status = "submitted"') as cursor:
            submitted_count = (await cursor.fetchone())[0]
        
        # Get count of overdue projects
        now = datetime.utcnow()
        async with db.execute('''
            SELECT COUNT(*) FROM projects 
            WHERE deadline IS NOT NULL 
            AND status IN ('assigned', 'agreed', 'in_progress')
            AND datetime(deadline) < datetime(?)
        ''', (now.isoformat(),)) as cursor:
            overdue_count = (await cursor.fetchone())[0]
    
    embed = discord.Embed(
        title="📊 Project Summary Dashboard", 
        description="Overview of all projects in the system",
        color=discord.Color.blue()
    )
    
    # Status breakdown
    for status, count in status_counts:
        status_emoji = {
            'unassigned': '⏳',
            'assigned': '📋',
            'agreed': '🤝',
            'in_progress': '🚀',
            'submitted': '📤',
            'approved': '✅',
            'rejected': '❌',
            'changes_requested': '🔄'
        }.get(status, '📁')
        
        embed.add_field(
            name=f"{status_emoji} {status.title()}", 
            value=f"{count} project(s)", 
            inline=True
        )
    
    # Special highlights
    if submitted_count > 0:
        embed.add_field(
            name="⚠️ **Action Required**", 
            value=f"{submitted_count} project(s) awaiting review\nUse `/list_submitted` to see details", 
            inline=False
        )
    
    if overdue_count > 0:
        embed.add_field(
            name="🚨 **Overdue Projects**", 
            value=f"{overdue_count} project(s) past deadline", 
            inline=False
        )
    
    embed.set_footer(text="Use /search_projects or /project_details for more information")
    await interaction.response.send_message(embed=embed, ephemeral=True)

def parse_rate_to_amount(rate_raw: Optional[str]) -> Optional[tuple[str, float]]:
    """Best-effort parse of a rate string into (currency_code, amount).
    Supports examples like "$100", "5,000 PHP", "₱2,500", "USD 75", "€120".
    Returns None if no amount is found.
    """
    if not rate_raw:
        return None
    text = str(rate_raw).strip()
    if not text:
        return None
    lowered = text.lower()
    # Detect currency
    if '$' in text or 'usd' in lowered:
        currency = 'USD'
    elif 'php' in lowered or '₱' in text:
        currency = 'PHP'
    elif 'eur' in lowered or '€' in text:
        currency = 'EUR'
    else:
        # Default to USD if unknown
        currency = 'USD'
    # Extract first numeric amount
    match = re.search(r'([0-9]{1,3}(?:,[0-9]{3})*(?:\.[0-9]{1,2})?|[0-9]+(?:\.[0-9]{1,2})?)', text)
    if not match:
        return None
    num_str = match.group(1).replace(',', '')
    try:
        amount = float(num_str)
    except ValueError:
        return None
    return currency, amount


def format_currency_amount(currency: str, amount: float) -> str:
    if currency == 'USD':
        return f"${amount:,.2f}"
    if currency == 'PHP':
        return f"₱{amount:,.2f}"
    if currency == 'EUR':
        return f"€{amount:,.2f}"
    return f"{currency} {amount:,.2f}"


class PaymentRequestView(View):
    def __init__(self, request_id: int):
        super().__init__(timeout=None)
        self.request_id = request_id

    @discord.ui.button(label="Mark Paid", style=discord.ButtonStyle.success, emoji="💰", custom_id="pay_mark_paid")
    async def mark_paid(self, interaction: discord.Interaction, button: Button):
        if not interaction.user.guild_permissions.manage_messages:
            await interaction.response.send_message('Only managers can mark as paid.', ephemeral=True)
            return
        # Load request
        async with aiosqlite.connect(db_path) as db:
            async with db.execute('SELECT project_ids, status FROM payment_requests WHERE id = ?', (self.request_id,)) as cur:
                row = await cur.fetchone()
            if not row:
                await interaction.response.send_message('Payment request not found.', ephemeral=True)
                return
            project_ids_csv, req_status = row
            if req_status == 'paid':
                await interaction.response.send_message('This request is already marked as paid.', ephemeral=True)
                return
            project_ids = [int(x) for x in project_ids_csv.split(',') if x.strip().isdigit()]
            if project_ids:
                placeholders = ','.join('?' for _ in project_ids)
                await db.execute(f"UPDATE projects SET payment_status = 'paid' WHERE id IN ({placeholders})", project_ids)
            await db.execute('UPDATE payment_requests SET status = ?, resolved_at = ? WHERE id = ?', ('paid', datetime.utcnow().isoformat(timespec='minutes'), self.request_id))
            await db.commit()
        # Acknowledge and edit original message
        await interaction.response.send_message('Marked as paid. Thank you!', ephemeral=True)
        try:
            await interaction.message.edit(content=f"✅ Payment request #{self.request_id} has been marked as PAID by {interaction.user.mention}.", view=None)
        except Exception:
            pass
        # DM manager copy
        try:
            mgr = await bot.fetch_user(NOTIFY_USER_ID)
            await mgr.send(f"✅ Payment request #{self.request_id} marked as PAID by {interaction.user} ({interaction.user.display_name}).")
        except Exception:
            pass


# Update request_payment to also post publicly and create a payment_requests row
@bot.tree.command(name="request_payment", description="Request payment for your approved projects")
async def slash_request_payment(interaction: discord.Interaction):
    """When an editor runs this, list all approved (unpaid) projects, sum totals by currency, mark them as requested, and notify managers publicly and via DM."""
    user_id = interaction.user.id
    async with aiosqlite.connect(db_path) as db:
        # Fetch approved projects that are not paid yet
        async with db.execute('''
            SELECT id, name, rate, payment_status
            FROM projects
            WHERE assigned_editor_id = ?
              AND status = 'approved'
              AND (payment_status IS NULL OR payment_status IN ('pending','requested'))
            ORDER BY id DESC
        ''', (user_id,)) as cursor:
            rows = await cursor.fetchall()
    
    if not rows:
        await interaction.response.send_message('You have no approved, unpaid projects to request payment for.', ephemeral=True)
        return
    
    totals: dict[str, float] = {}
    items_lines = []
    unparsable_count = 0
    pending_ids = []
    requested_count = 0
    project_ids = []
    
    for pid, name, rate, payment_status in rows:
        project_ids.append(pid)
        parsed = parse_rate_to_amount(rate)
        if parsed is None:
            unparsable_count += 1
            display_rate = rate or 'N/A'
        else:
            currency, amount = parsed
            totals[currency] = totals.get(currency, 0.0) + amount
            display_rate = format_currency_amount(currency, amount)
        state = payment_status or 'pending'
        if state == 'pending':
            pending_ids.append(pid)
        elif state == 'requested':
            requested_count += 1
        items_lines.append(f"• ID {pid} — {name} — {display_rate} ({state})")
    
    # Update payment_status -> requested for pending ones
    if pending_ids:
        placeholders = ','.join('?' for _ in pending_ids)
        query = f"UPDATE projects SET payment_status = 'requested' WHERE id IN ({placeholders})"
        async with aiosqlite.connect(db_path) as db:
            await db.execute(query, pending_ids)
            await db.commit()
    
    # Persist a payment request record
    totals_json = json.dumps(totals)
    project_ids_csv = ','.join(str(x) for x in project_ids)
    async with aiosqlite.connect(db_path) as db:
        cursor = await db.execute('INSERT INTO payment_requests (editor_id, project_ids, totals_json, status) VALUES (?, ?, ?, ?)', (user_id, project_ids_csv, totals_json, 'requested'))
        await db.commit()
        request_id = cursor.lastrowid
        await cursor.close()
    
    # Build summary embed
    embed = discord.Embed(
        title=f"💵 Payment Request #{request_id}",
        description=f"Approved projects for {interaction.user.mention}",
        color=discord.Color.blurple()
    )
    embed.add_field(name="Projects", value='\n'.join(items_lines)[:1024] or 'None', inline=False)
    
    # Include editor payment details
    try:
        async with aiosqlite.connect(db_path) as db:
            async with db.execute('SELECT gcash, gcash_qr_url FROM editors WHERE user_id = ?', (user_id,)) as cur:
                pay_row = await cur.fetchone()
        if pay_row:
            gcash_number, gcash_qr_url = pay_row
            if gcash_number:
                embed.add_field(name="GCash Number", value=gcash_number, inline=True)
            if gcash_qr_url:
                try:
                    embed.set_image(url=gcash_qr_url)
                except Exception:
                    pass
    except Exception:
        pass
    
    if totals:
        totals_lines = []
        for cur, amt in totals.items():
            totals_lines.append(f"{cur}: {format_currency_amount(cur, amt)}")
        embed.add_field(name="Totals", value='\n'.join(totals_lines), inline=False)
    else:
        embed.add_field(name="Totals", value="No parsable rates found.", inline=False)
    
    footer_bits = []
    if pending_ids:
        footer_bits.append(f"Marked {len(pending_ids)} project(s) as requested")
    if requested_count:
        footer_bits.append(f"{requested_count} already requested")
    if unparsable_count:
        footer_bits.append(f"{unparsable_count} rate(s) not parsed")
    if footer_bits:
        embed.set_footer(text=' • '.join(footer_bits))
    
    # Respond to editor (ephemeral)
    await interaction.response.send_message(embed=embed, ephemeral=True)
    
    # Public post in the channel so everyone sees it + manager-only button
    try:
        view = PaymentRequestView(request_id)
        public_msg = await interaction.channel.send(content=f"📢 {interaction.user.mention} has requested payment for approved projects.", embed=embed, view=view)
    except Exception:
        public_msg = None
    
    # Notify manager via DM
    try:
        mgr = await bot.fetch_user(NOTIFY_USER_ID)
        await mgr.send(f"💵 Payment request from {interaction.user} ({interaction.user.display_name})", embed=embed)
    except Exception:
        pass
    
    # Mention managers in channel
    try:
        if MANAGER_ROLE_ID:
            mention = f'<@&{MANAGER_ROLE_ID}>'
            await interaction.channel.send(f"{mention}: Please review payment request #{request_id} from {interaction.user.mention}.")
    except Exception:
        pass

def format_discord_deadline(deadline_iso: Optional[str]) -> str:
    """Render an ISO timestamp as a Discord timestamp (absolute + relative). Falls back to the raw string or 'N/A'."""
    if not deadline_iso:
        return 'N/A'
    try:
        from datetime import timezone
        dt = datetime.fromisoformat(deadline_iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        ts = int(dt.timestamp())
        return f'<t:{ts}:f> (<t:{ts}:R>)'
    except Exception:
        return str(deadline_iso)

async def dm_managers(interaction: discord.Interaction, message: str, embed: Optional[discord.Embed] = None) -> None:
    """Best-effort DM to the configured manager user, manager role members, and the guild owner.
    Silently ignores failures (e.g., DMs disabled)."""
    recipients = []
    # Env-configured single manager
    if NOTIFY_USER_ID:
        try:
            user = await bot.fetch_user(NOTIFY_USER_ID)
            if user and not user.bot:
                recipients.append(user)
        except Exception:
            pass
    # Manager role members
    try:
        if MANAGER_ROLE_ID and interaction.guild:
            role = interaction.guild.get_role(MANAGER_ROLE_ID)
            if role:
                for member in role.members:
                    if member and not member.bot:
                        recipients.append(member)
    except Exception:
        pass
    # Guild owner fallback
    try:
        if interaction.guild and interaction.guild.owner and not interaction.guild.owner.bot:
            recipients.append(interaction.guild.owner)
    except Exception:
        pass
    # Deduplicate by id
    seen = set()
    unique = []
    for r in recipients:
        if r.id not in seen:
            seen.add(r.id)
            unique.append(r)
    # Send
    for r in unique:
        try:
            if embed is not None:
                await r.send(message, embed=embed)
            else:
                await r.send(message)
        except Exception:
            continue

if __name__ == '__main__':
    # Load variables from .env in project root (if present). Allow .env to override shell env to avoid stale tokens during local dev.
    load_dotenv(override=True)
    # Read token from environment variable
    #  for security. Set DISCORD_BOT_TOKEN in your env.
    TOKEN = os.getenv('DISCORD_BOT_TOKEN')
    if not TOKEN:
        raise RuntimeError('Missing DISCORD_BOT_TOKEN environment variable.')
    bot.run(TOKEN)