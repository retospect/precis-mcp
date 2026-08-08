# serverInfo.title not set (blocked upstream)

MCP spec 2025-06-18 §A1 recommends a serverInfo.title; FastMCP(...) takes no
title= kwarg. One-line fix once FastMCP accepts it — file the upstream
request when the next mcp-critic pass surfaces it. Owner
`src/precis/server.py`; test test_serverinfo_carries_title.
