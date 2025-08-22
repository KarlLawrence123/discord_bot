<<<<<<< HEAD
# Discord Bot - Video Editing Project Management

## Enhanced Notification System

This bot now provides comprehensive notifications for all project activities. Here's how it works:

### 🔔 Notification Types

1. **Project Acceptance** - When an editor accepts a project
2. **Project Decline** - When an editor declines a project  
3. **Project Submission** - When an editor submits a completed project
4. **Project Approval/Rejection** - When managers review submissions
5. **Payment Status Changes** - When projects are marked as paid

### 📱 How You Get Notified

- **Direct Messages (DM)**: You'll receive detailed notifications via DM for all major events
- **Channel Mentions**: The bot mentions managers in relevant channels using `@ManagerRole`
- **Rich Embeds**: Notifications include detailed project information in beautiful Discord embeds

### 🎯 Key Commands for Managers

#### View Submitted Projects
- `/list_submitted` - Lists all projects awaiting review
- `/project_details <id>` - Get detailed information about a specific project
- `/project_summary` - Overview of all projects by status
- `/search_projects` - Search projects by various criteria

#### Manage Projects
- `/approve_ui` - Approve a submitted project
- `/reject_ui` - Reject a submitted project with reason
- `/mark_paid_ui` - Mark a project as paid

### 📊 Project Status Flow

1. **Unassigned** → **Assigned** → **Agreed** → **In Progress** → **Submitted** → **Approved/Rejected**
2. **Changes Requested** → **In Progress** → **Submitted** → **Approved/Rejected**

### 🔧 Environment Variables

Set these in your `.env` file:

```env
DISCORD_BOT_TOKEN=your_bot_token_here
MANAGER_ROLE_ID=your_manager_role_id_here
NOTIFY_USER_ID=your_user_id_here
ARCHIVE_AFTER_DAYS=30
```

### 📋 Example Notification

When a project is submitted, you'll receive:

```
📤 New Project Submission!

Project ID: 123
Project Name: Client Promo Video
Editor: John Doe (Video Editor)
Rate: $100
Deadline: 2024-01-15T18:00
Status: Submitted
Thread: #project-123-john-doe

📤 Submission Details:
https://drive.google.com/file/...
Attachment: https://example.com/file.mp4
Notes: Project completed as requested

Action Required: Review and approve/reject this submission.
```

### 🚀 Getting Started

1. Set up your environment variables
2. Run the bot: `python bot.py`
3. Use `/help` to see all available commands
4. Use `/list_submitted` to see projects awaiting review

### 💡 Tips

- Use `/project_summary` to get a quick overview of all projects
- Use `/search_projects status:submitted` to find specific project types
- All notifications include direct links to project threads and details
- The bot automatically tracks project status changes and notifies relevant parties
=======
# discord_bot
>>>>>>> 8e421aa35c20979f20c97abbf8bd9e9e908ed703
