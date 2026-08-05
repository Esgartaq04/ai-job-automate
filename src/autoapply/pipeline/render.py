"""HTML -> PDF via the Chromium that Playwright already ships for the submit worker.

No extra rendering dependency, and what you review in the browser is byte-for-byte
what gets uploaded.
"""

from __future__ import annotations

import datetime as dt
from functools import lru_cache
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape

from ..config import get_settings

TEMPLATE_DIR = Path(__file__).parent / "templates"


@lru_cache
def _env() -> Environment:
    return Environment(
        loader=FileSystemLoader(TEMPLATE_DIR),
        autoescape=select_autoescape(["html"]),
        trim_blocks=True,
        lstrip_blocks=True,
    )


def launch_kwargs() -> dict[str, Any]:
    """Chromium launch options, honouring an explicit binary override."""
    kwargs: dict[str, Any] = {"args": ["--no-sandbox"]}
    path = get_settings().chromium_path
    if path:
        kwargs["executable_path"] = path
    return kwargs


def html_to_pdf(html: str) -> bytes:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(**launch_kwargs())
        try:
            page = browser.new_page()
            page.set_content(html, wait_until="load")
            return page.pdf(format="Letter", print_background=True)
        finally:
            browser.close()


def _contact(profile: Any) -> dict[str, Any]:
    resume = profile.base_resume_json or {}
    return {
        "name": resume.get("name", "Candidate"),
        "headline": resume.get("headline"),
        "email": resume.get("email"),
        "phone": resume.get("phone"),
        "location": resume.get("location"),
        "links": [v for v in (resume.get("links") or []) if v],
    }


def group_bullets(bullets: list[dict[str, str]], facts: list[Any]) -> list[dict[str, Any]]:
    """Group verified bullets under the role/org of the fact each one cites."""
    by_id = {fact.id: fact for fact in facts}
    order: list[tuple[str, str]] = []
    groups: dict[tuple[str, str], dict[str, Any]] = {}

    for bullet in bullets:
        fact = by_id.get(bullet.get("fact_id", ""))
        if fact is None:
            continue
        key = (fact.org or "", fact.role or "")
        if key not in groups:
            window = f"{fact.start_date or ''} – {fact.end_date or 'Present'}".strip(" –")
            groups[key] = {"org": fact.org, "role": fact.role or "Experience", "window": window, "bullets": []}
            order.append(key)
        groups[key]["bullets"].append(bullet["text"])

    return [groups[key] for key in order]


def render_resume_html(
    profile: Any, job: Any, company: Any, summary: str, bullets: list[dict[str, str]], facts: list[Any]
) -> str:
    contact = _contact(profile)
    skills = sorted({skill for fact in facts for skill in (fact.skills or [])})
    return _env().get_template("resume.html").render(
        **contact, summary=summary, groups=group_bullets(bullets, facts), skills=skills
    )


def render_resume_pdf(
    profile: Any, job: Any, company: Any, summary: str, bullets: list[dict[str, str]], facts: list[Any]
) -> bytes:
    return html_to_pdf(render_resume_html(profile, job, company, summary, bullets, facts))


def render_cover_letter_html(profile: Any, job: Any, company: Any, body: str) -> str:
    contact = _contact(profile)
    paragraphs = [p.strip() for p in (body or "").split("\n") if p.strip()]
    return _env().get_template("cover_letter.html").render(
        **contact,
        date=dt.date.today().strftime("%B %d, %Y").replace(" 0", " "),
        company=company.name if company else None,
        title=job.title,
        paragraphs=paragraphs,
    )


def render_cover_letter_pdf(profile: Any, job: Any, company: Any, body: str) -> bytes:
    return html_to_pdf(render_cover_letter_html(profile, job, company, body))
