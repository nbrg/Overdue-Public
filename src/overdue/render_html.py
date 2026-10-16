"""Render the daily report to a static HTML page (GitHub Pages source)."""

from __future__ import annotations

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape

from . import config
from .report import Report


def render(report: Report, out_dir: Path | None = None) -> Path:
    """Render ``report`` to ``site/index.html`` and return the written path."""
    out_dir = out_dir or config.SITE_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    env = Environment(
        loader=FileSystemLoader(str(config.TEMPLATES_DIR)),
        autoescape=select_autoescape(["html", "xml"]),
    )
    template = env.get_template("index.html.j2")
    html = template.render(
        report=report,
        baseline_seasons=config.MODEL.baseline_seasons,
    )
    out_path = out_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
