# Steam Notifier

A Discord bot that watches Steam game prices and sends you a DM when one drops.

## Commands

| Command | What it does |
| --- | --- |
| `/watch <appid> [alert_below]` | Watch a game. Set `alert_below` to only hear about prices under that amount. Leave it empty to hear about every new low. |
| `/unwatch <appid>` | Stop watching a game. |
| `/list` | Show the games you're watching. |

The appid is the number in the game's store URL. Portal 2 is `620`. Replies are visible only to you, and each user can watch up to 25 games.
