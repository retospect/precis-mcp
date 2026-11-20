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

**Private GTD** lets the app capture and read items in your own GTD inbox.

Connections also have access to the OAuth and learning help documents. The
older manually copied personal MCP tokens remain restricted to your own
learning and GTD data, and only work where your deployment accepts them (see
the last section).

## Review or disconnect apps

Open **Account → Connected apps**. Each row identifies one authorization,
including its name, client, permissions and expiry. Revoke the row you want
to disconnect; that connection's access and refresh credentials stop working.
Other connections, including another authorization for the same app, remain.

Access credentials renew automatically while the connection remains valid.
Connections expire after 30 days and require a new sign-in. If you revoke a
connection or disable the account, the app cannot renew it. Reconnecting makes
a new row; it does not reactivate the old one.

## Capability tokens for other clients

Capability tokens and personal MCP tokens work only where the deployment
accepts those credential classes. The default public endpoint accepts OAuth
connections only: it refuses both, and **Account** offers no way to mint them
(tokens already minted stay listed so you can revoke them). Your podcast
subscribe link is a separate credential and is not affected. If this page's
sections are missing from your **Account**, connect through the OAuth
instructions above.

OAuth is the recommended path for a claude.ai chat connector. Where a client
can present a static bearer but cannot complete the OAuth sign-in — or where
a narrower scope is the point — a **client capability token** is the third
credential class: it names the consumer, not you, so minting one never hands
a client your personal data.

Open **Account → Client capability tokens**, name the consumer (for example
"claude.ai chat"), and pick its allow-list of scopes:

- `read` — the whole get/search read surface. Blank scope input means this.
- `read:<kind>` — reads on one kind only, e.g. `read:paper`.
- `write:<kind>` — all seven verbs on one kind (writes imply same-kind
  reads). There is no whole-corpus write scope.
- `feed` — the podcast `?t=` credential only; it grants no MCP verb.

Copy the token once — it is shown exactly once and stored only as a digest.
Send it as `Authorization: Bearer <token>` on the streamable-HTTP MCP
endpoint. Claude's *Request headers* setting is a limited organization beta;
an organization Owner enters the credential, which all members then share.
If that setting is absent, use the OAuth connection above. These constraints
are documented in [Claude's authentication guide](https://claude.com/docs/connectors/building/authentication).
The credential never goes in the MCP URL; feed listeners are the exception
because podcast apps cannot set headers, which is what the `feed` scope and
the subscribe link are for.

Revoke a row from the same section to cut that consumer off; revoking or
disabling takes effect on the next request. Minting a token does not share
your account: the token resolves to its own allow-list, so `read` plus
`write:memory` lets a connector read the corpus and write memories, but
never reach private kinds — those still require a personal token or an
OAuth connection.
