# asa-slack live smoke test (ADR 0062)

Code shipped + deployed + connected (`com.asa.slack` on melchior); remaining
is the manual smoke: threading (never posts to channel root), a paper-search
question actually works, a "kick off a job" request is refused with
`Unsupported` (not just declined in prose), and a repeat message from the
same person shows the per-person memory note. Note: prompt/config
(SOUL/HINTS) changes still need the full 48-asa-slack.yml / 31-asa-bot.yml
run from a grimoire-checkout controller. Owner `src/asa_slack/`.
