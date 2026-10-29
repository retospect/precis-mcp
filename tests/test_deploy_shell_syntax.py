"""Deploy shell scripts must parse, and the ensure script's single-quoted
RUN_CMD literal must stay free of stray single quotes.

RUN_CMD holds the container's prepare script inside a '...' literal. A
quote inside it is a delimiter, not text: an apostrophe in a comment left
the unquoted tail unparsable (``bash -n`` fails, the container gets a
broken command), while code quotes like ``'%s'`` would be silently eaten —
the generated precis-prepare.sh loses them either way (gr529877 fix
round). The rig tests extract the heredoc text directly, so neither
failure is visible there; these guards read the file itself.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

_SCRIPTS = sorted(
    (Path(__file__).resolve().parents[1] / "deploy" / "mcp-http").glob("*.sh")
)
_ENSURE = (
    Path(__file__).resolve().parents[1] / "deploy/mcp-http/precis-mcp-http-ensure.sh"
)

pytestmark = pytest.mark.skipif(
    sys.platform == "win32", reason="POSIX-only: runs bash -n"
)


def test_deploy_mcp_http_still_holds_shell_scripts() -> None:
    assert _SCRIPTS, "the deploy/mcp-http/*.sh glob went stale — guard sees nothing"


@pytest.mark.parametrize("script", _SCRIPTS, ids=[p.name for p in _SCRIPTS])
def test_shell_script_parses_under_bash_n(script: Path) -> None:
    result = subprocess.run(
        ["bash", "-n", str(script)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_run_cmd_literal_has_no_inner_single_quotes() -> None:
    """A `'` inside the literal is consumed as a delimiter: unbalanced it
    breaks ``bash -n``; balanced it silently rewrites the generated
    script. The only quotes allowed are the ``'"${BACKEND_PORT}"'``
    interpolation pairs."""
    src = _ENSURE.read_text(encoding="utf-8")
    start = src.index("RUN_CMD='set -e")
    end = src.index("\n", src.index("exec precis serve --transport streamable-http"))
    body = src[start:end].replace("RUN_CMD='", "", 1)
    body = body.replace("'\"${BACKEND_PORT}\"'", "").replace('\'"${BACKEND_PORT}"', "")
    assert "'" not in body, (
        "single quote inside the single-quoted RUN_CMD literal — "
        "it ends the string; use double quotes in the prepare body"
    )
