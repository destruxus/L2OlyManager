# Bot invite link

Application (client) ID: `1533144560056799273`

Invite / re-authorize the bot with this URL:

```
https://discord.com/oauth2/authorize?client_id=1533144560056799273&permissions=326686035024&integration_type=0&scope=bot+applications.commands
```

The `permissions=326686035024` value grants exactly what the bot needs:
View Channels, Manage Channels, Manage Roles, Manage Threads, Create Public
Threads, Send Messages, Send Messages in Threads, Embed Links, Add Reactions,
Read Message History. (It does **not** include Administrator — that's fine.)

## Still required, separate from this link
- **Bot tab → Message Content Intent → ON** (the score listener needs it).
- After inviting, drag the **bot's role above the class roles** in
  Server Settings → Roles, so it can create/assign them.
- Your `client_id` here is also the Application ID; the value you still need in
  `config.json` is the **Server ID** (right-click server → Copy Server ID).
```
