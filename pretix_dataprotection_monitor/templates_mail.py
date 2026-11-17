import re
from dataclasses import dataclass
from string import Formatter
from urllib.parse import urlsplit

from django.core.exceptions import ValidationError
from django.utils.translation import gettext_lazy as _
from django.utils.html import escape, format_html, format_html_join

DEFAULT_SUBJECT = "Data protection review required — {event_name}"
DEFAULT_BODY = """Hello,

Our automated data protection review found that customer or attendee data may still be identifiable for the following event:

Event: {event_name}
Organizer: {organizer_name}
Event end: {event_end}
Days since completion: {days_since_event}
Review due: {due_date}

Review findings:
{issues_summary}

Please review this event in pretix:
{event_url}

If the personal information is no longer needed and no retention obligation applies, please use pretix's built-in personal-data deletion functions.

If the data must still be retained, please document the reason through the agreed review process.

This message was generated automatically by the pretix Data Protection Monitor."""

PLACEHOLDERS = frozenset({
    "event_name", "organizer_name", "event_end", "days_since_event",
    "issues_summary", "event_url", "due_date",
})
FORMATTER = Formatter()
FIELD_PATTERN = re.compile(r"\{\{ | \}\} | \{([a-z_]+)\}", re.X)


def validate_template(value, *, subject=False):
    limit = 300 if subject else 12000
    if not value.strip() or len(value) > limit:
        raise ValidationError(_("Enter a template of 1 to %(limit)s characters.") % {"limit": limit})
    if "\x00" in value or (subject and len(value.splitlines()) > 1) or (
        subject and any(char in value for char in "\r\n")
    ):
        raise ValidationError(_("This template contains unsupported control characters."))
    # Reject even an empty format specifier, including constructions such as
    # {event_name:}. Only exact fields and doubled literal braces are valid.
    remainder = FIELD_PATTERN.sub("", value)
    if "{" in remainder or "}" in remainder:
        raise ValidationError(_("Use exact placeholder names or double literal braces."))
    for match in FIELD_PATTERN.finditer(value):
        if match[1] is not None and match[1] not in PLACEHOLDERS:
            raise ValidationError(_("The template contains an unknown placeholder."))


def _render(template, values):
    return "".join(
        literal + (str(values[name]) if name is not None else "")
        for literal, name, _, _ in FORMATTER.parse(template)
    )


LINK = re.compile(
    r"""\[(?P<label>[^\]\r\n]+)\]\((?P<target>https?://[^\s<>"')]+)\)"""
    r"""|(?P<bare>https?://[^\s<>"']+)""",
    re.IGNORECASE,
)


def linked_html(body):
    parts = []
    offset = 0
    for match in LINK.finditer(body):
        parts.append(escape(body[offset:match.start()]))
        address = match["target"] or match["bare"].rstrip(".,;:!?]")
        if match["bare"]:
            while address.endswith(")") and address.count(")") > address.count("("):
                address = address[:-1]
        try:
            host = urlsplit(address).hostname
        except ValueError:
            host = None
        if host:
            parts.append(format_html('<a href="{}">{}</a>', address, match["label"] or address))
            if match["bare"]:
                parts.append(escape(match["bare"][len(address):]))
        else:
            parts.append(escape(match[0]))
        offset = match.end()
    parts.append(escape(body[offset:]))
    return format_html(
        '<pre style="white-space:pre-wrap;font-family:inherit">{}</pre>',
        format_html_join("", "{}", ((part,) for part in parts)),
    )


@dataclass(frozen=True)
class Message:
    subject: str
    body: str

    def preview_html(self):
        return format_html("<strong>{}</strong>{}", self.subject, linked_html(self.body))


def render_message(subject, body, values):
    validate_template(subject, subject=True)
    validate_template(body)
    # Values are plain strings, interpolated once. Flatten all line separators
    # in the rendered subject before it can reach a mail header.
    header = " ".join(_render(subject, values).replace("\x00", "").splitlines()).strip()
    if not header:
        raise ValidationError(_("The rendered subject is empty."))
    return Message(header[:300], _render(body, values))
