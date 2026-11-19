"""How threads are shown to Claude. Shared so every prompt sees them the same way."""

from __future__ import annotations

from reviewr.models import StaleTrace, Thread


def render_thread(t: Thread, trace: StaleTrace | None = None, extra: str = "") -> str:
    convo = "\n".join(f"{c.author}: {c.body.strip()}" for c in t.comments)
    hunk = "\n".join(t.diff_hunk.splitlines()[-10:])
    parts = [
        (
            f'<thread id="{t.id}" path="{t.path}" line="{t.line or t.original_line}"'
            f' stale="{str(t.is_outdated).lower()}">'
        ),
        f"<code_when_commented>\n{hunk}\n</code_when_commented>",
        f"<conversation>\n{convo}\n</conversation>",
    ]
    if trace and trace.status != "unknown":
        parts.append(
            f'<current_code status="{trace.status}" path="{trace.current_path}"'
            f' line="{trace.current_line}">\n{trace.current_snippet}\n</current_code>'
        )
    if extra:
        parts.append(extra)
    parts.append("</thread>")
    return "\n".join(parts)
