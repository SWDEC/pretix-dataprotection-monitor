from django.utils.translation import gettext_lazy as _

from .audit import Status


DELIVERY_LABELS = {
    "blocked": _("Blocked"),
    "queued": _("Queued"),
    "retrying": _("Retrying"),
    "sent": _("Sent"),
    "failed": _("Failed"),
    "needs_review": _("Needs review"),
}

RUN_LABELS = {
    "queued": _("Queued"),
    "failed": _("Failed"),
    "running": _("Running"),
    "completed": _("Completed"),
}

ISSUE_LABELS = {
    "customer_accounts": _("Customer accounts"),
    "customer_emails": _("Customer emails"),
    "customer_phones": _("Customer phones"),
    "attendee_emails": _("Attendee emails"),
    "attendee_names": _("Attendee names"),
    "attendee_contact_details": _("Attendee contact details"),
    "attendee_location": _("Attendee location"),
    "waiting_list_contacts": _("Waiting list contacts"),
    "question_contact_answers": _("Question contact answers"),
    "other_question_answers": _("Other question answers"),
    "question_uploads": _("Question uploads"),
    "cart_contact_fields": _("Cart contact fields"),
    "cart_metadata": _("Cart metadata"),
    "checkout_customer_accounts": _("Checkout customer accounts"),
    "checkout_metadata": _("Checkout metadata"),
    "invoice_addresses": _("Invoice addresses"),
    "invoice_documents": _("Invoice documents"),
    "invoice_recipient_fields": _("Invoice recipient fields"),
    "invoice_metadata": _("Invoice metadata"),
    "invoice_line_attendees": _("Invoice line attendees"),
    "generated_tickets": _("Generated tickets"),
    "outgoing_customer_email": _("Outgoing customer email"),
    "unclassified_event_email": _("Unclassified event email"),
    "certificate_attachments": _("Certificate attachments"),
    "certificate_logs": _("Certificate logs"),
    "historical_logs": _("Historical logs"),
    "historical_invoice_data": _("Historical invoice data"),
    "unclassified_logs": _("Unclassified logs"),
    "order_metadata": _("Order metadata"),
    "attendee_metadata": _("Attendee metadata"),
    "payment_metadata": _("Payment metadata"),
    "refund_metadata": _("Refund metadata"),
    "order_comments": _("Order comments"),
    "waiting_list_voucher_notes": _("Waiting list voucher notes"),
    "unclassified_event_plugins": _("Unclassified event plugins"),
    "uncertain_schedule": _("Uncertain schedule"),
}

ORGANIZER_PERMISSION = "organizer.settings.general:write"


STATUS_LABELS = {
    "": _("Awaiting audit"),
    Status.NOT_DUE: _("Not due"),
    Status.NO_KNOWN_IDENTIFIERS: _("No known identifiers"),
    Status.ACTION_REQUIRED: _("Action required"),
    Status.MANUAL_REVIEW: _("Manual review"),
    Status.DEFERRED: _("Deferred"),
    Status.SCAN_ERROR: _("Scan error"),
}
