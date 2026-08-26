"""Shared Guardian Maintenance header and navigation."""

from __future__ import annotations

from html import escape


NAVIGATION = (
    ("modules", "Module & Stack"),
    ("configuration", "Konfiguration"),
    ("maintenance", "Maintenance-Logbuch"),
    ("timeline", "Verlauf"),
    ("history", "Zeitverläufe"),
)


def render_guardian_header(*, active: str, paths: dict[str, str],
                           subtitle: str | None = None) -> str:
    """Render the same ingress-safe header on all five application pages."""

    if active not in {key for key, _label in NAVIGATION}:
        raise ValueError("unknown Guardian navigation item")
    links = []
    for key, label in NAVIGATION:
        selected = ' class="active" aria-current="page"' if key == active else ""
        links.append(f'<a{selected} href="{escape(paths[key], quote=True)}">{escape(label)}</a>')
    detail = f"<div>{escape(subtitle)}</div>" if subtitle else ""
    return f"<header><h1>Guardian Maintenance</h1>{detail}<nav>{''.join(links)}</nav></header>"
