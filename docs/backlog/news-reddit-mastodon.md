# News: Reddit / Mastodon sources

The framework exists (news_sources registry + news_poll worker,
`src/precis/handlers/news.py`). Subreddits and Mastodon accounts expose
public .rss feeds — ingestible credential-free as news_sources rows. Only
build a bespoke API client if the RSS path proves insufficient. Feed content
inherits the tier-0 injection-scan gate in news_poll automatically.
Mechanical.
