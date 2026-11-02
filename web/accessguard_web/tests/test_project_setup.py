"""Project setup forms submitted the way a browser submits them.

A browser sends every field of the form, including the empty extra rows that Django renders
below the saved ones, and for a <select> without a selected option it sends the first option.
These tests build the POST data from the rendered page with those rules, so a form that only
works when a client omits the empty rows (as hand-written test data does) fails here.
"""

from __future__ import annotations

from html.parser import HTMLParser
from typing import Any

from django.test import TestCase
from django.urls import reverse

from accessguard_web.projects.models import Project
from accessguard_web.tests.support import login, make_org, sample_project


class _FormFields(HTMLParser):
    """Collects (name, value) pairs as a browser would submit the form containing ``marker``."""

    def __init__(self, marker: str) -> None:
        super().__init__(convert_charrefs=True)
        self.marker = marker
        self.forms: list[list[tuple[str, str]]] = []
        self._select: dict[str, Any] | None = None
        self._textarea: list[str] | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = {k: v or "" for k, v in attrs}
        if tag == "form":
            self.forms.append([])
        elif tag == "input" and a.get("name") and a.get("type") not in ("submit", "button"):
            if a.get("type") not in ("checkbox", "radio") or "checked" in a:
                self.forms[-1].append((a["name"], a.get("value", "")))
        elif tag == "select":
            self._select = {"name": a["name"], "options": [], "selected": None}
        elif tag == "option" and self._select is not None:
            self._select["options"].append(a.get("value", ""))
            if "selected" in a:
                self._select["selected"] = a.get("value", "")
        elif tag == "textarea":
            self._textarea = [a["name"], ""]

    def handle_data(self, data: str) -> None:
        if self._textarea is not None:
            self._textarea[1] += data

    def handle_endtag(self, tag: str) -> None:
        if tag == "select" and self._select is not None:
            chosen = self._select["selected"]
            if chosen is None and self._select["options"]:
                chosen = self._select["options"][0]
            if chosen is not None:
                self.forms[-1].append((self._select["name"], chosen))
            self._select = None
        elif tag == "textarea" and self._textarea is not None:
            name, value = self._textarea
            self.forms[-1].append((name, value.removeprefix("\n")))
            self._textarea = None

    def result(self) -> dict[str, Any]:
        form = next(f for f in self.forms if any(n == self.marker for n, _ in f))
        data: dict[str, Any] = {}
        for name, value in form:
            data.setdefault(name, []).append(value)
        return data


def browser_post_data(html: str, marker: str, **changes: Any) -> dict[str, Any]:
    parser = _FormFields(marker)
    parser.feed(html)
    data = parser.result()
    data.update({name.replace("__", "-"): value for name, value in changes.items()})
    return data


class RulesPageBrowserTests(TestCase):
    def setUp(self) -> None:
        organization, owner = make_org("Setup")
        self.client = login(owner)
        self.project = sample_project(organization, "http://127.0.0.1:9")
        self.url = reverse("projects:setup_rules", args=[self.project.pk])

    def post_page(self, **changes: Any) -> Any:
        page = self.client.get(self.url)
        return self.client.post(
            self.url, browser_post_data(page.content.decode(), "records-TOTAL_FORMS", **changes)
        )

    def test_the_empty_record_row_offers_no_preselected_owner(self) -> None:
        page = self.client.get(self.url).content.decode()
        total = len(self.project.config["policy"]["resources"])
        self.assertIn(
            f'<select name="records-{total}-owner" id="id_records-{total}-owner">\n'
            '  <option value="" selected>Choose a test user</option>',
            page,
        )

    def test_adding_only_a_list_saves_although_an_empty_record_row_is_shown(self) -> None:
        policy = self.project.config["policy"]
        policy["collections"] = []
        for resource in policy["resources"]:
            resource.pop("collection", None)
        self.project.save()
        response = self.post_page(lists__0__id="audit", lists__0__path="/api/v1/audit")
        self.assertRedirects(
            response, reverse("projects:setup_limits", args=[self.project.pk]),
            fetch_redirect_response=False,
        )  # fmt: skip
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.config["policy"]["collections"], [{"id": "audit", "path": "/api/v1/audit"}]
        )

    def test_editing_a_saved_record_saves_although_an_empty_record_row_is_shown(self) -> None:
        response = self.post_page(records__0__category="renamed-category")
        self.assertEqual(response.status_code, 302)
        self.project.refresh_from_db()
        self.assertEqual(
            self.project.config["policy"]["resources"][0]["category"], "renamed-category"
        )

    def test_a_filled_record_still_needs_an_owner(self) -> None:
        index = len(self.project.config["policy"]["resources"])
        before = Project.objects.get(pk=self.project.pk).config
        response = self.post_page(**{
            f"records__{index}__id": "extra", f"records__{index}__category": "note",
            f"records__{index}__path": "/api/v1/notes/1", f"records__{index}__protected_fields": "data.title",
            f"records__{index}__access": ["orbit:owner"],
        })  # fmt: skip
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "This field is required.")
        self.assertEqual(Project.objects.get(pk=self.project.pk).config, before)
