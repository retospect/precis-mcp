# Connect your apps to Precis

Connect Claude or another supported MCP app to your Precis account. Each
authorization creates a separate connection: removing one leaves your other
apps connected. OAuth must first be enabled by your deployment owner.

## Connect Claude on the web

1. Open **Customize → Connectors → Add custom connector** in Claude. On a
   Team or Enterprise account, an authorized administrator first adds the web
   connector in **Organization settings → Connectors**.
2. Enter **Precis** and the public MCP URL supplied by your deployment owner.
   A localhost URL or a private VPN address will not work: Claude connects
   from Anthropic's servers.
3. Choose **Sign in now**. Under OAuth client choose **Use your own OAuth
   client**, enter client ID `precis-claude`, and leave the client secret blank.
   In the older dialog, these fields are under **Advanced settings**. Do not
   paste an install token into either field.
4. Add/connect the connector. Sign in with your Precis account when the
   browser opens Precis, check the requested permissions, name this connection,
   and approve it. Denying the request gives the app no access.
5. Back in Claude, enable Precis through the chat's **+ → Connectors** menu.
   Ask it to perform a harmless read before using write operations.

This uses OAuth, not Claude's limited-beta static Request headers setting.
The deployment owner must enable the public endpoint before connecting.
See [Claude's connector guide](https://support.claude.com/en/articles/11175166-get-started-with-custom-connectors-using-remote-mcp)
and [authentication requirements](https://claude.com/docs/connectors/building/authentication).

## Permissions

**Full MCP access** lets the app use all seven MCP tools, including writes,
deletions, shared corpus data and configured services. This is the default
requested permission. It is broad access, not a restriction to documents you
own. Existing tool restrictions still apply. Features that enforce personal
ownership, such as learning, use your signed-in account.

**Private learning** is a narrower permission that an operator can offer
instead. It lets the app read and update only your own learning activity and
settings. It does not grant access to another person's activity.

Connections also have access to the OAuth and learning help documents. The
older manually copied personal MCP tokens remain restricted to learning.

## Review or disconnect apps

Open **Account → Connected apps**. Each row identifies one authorization,
including its name, client, permissions and expiry. Revoke the row you want
to disconnect; that connection's access and refresh credentials stop working.
Other connections, including another authorization for the same app, remain.

Access credentials renew automatically while the connection remains valid.
Connections expire after 30 days and require a new sign-in. If you revoke a
connection or disable the account, the app cannot renew it. Reconnecting makes
a new row; it does not reactivate the old one.
