import logging
from zoneinfo import ZoneInfo

from celery import shared_task
from django.conf import settings
from django.core.mail import EmailMessage

from .models import Payment
from .tickets import render_ticket_pdf

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
    max_retries=5,
)
def send_booking_ticket(self, payment_id):
    payment = (
        Payment.objects.filter(id=payment_id, status=Payment.STATUS_SUCCESS)
        .select_related("user", "show__movie", "show__theater")
        .prefetch_related("bookings__seat")
        .first()
    )
    if payment is None:
        logger.warning("Skipping ticket email for non-confirmed payment %s", payment_id)
        return
    if not payment.user.email:
        raise ValueError(f"User {payment.user_id} has no email address for ticket delivery.")

    pdf = render_ticket_pdf(payment)
    local_show_time = payment.show.date_time.astimezone(ZoneInfo("Asia/Kolkata"))
    email = EmailMessage(
        subject=f"Your {payment.show.movie.name} movie ticket",
        body=(
            f"Hi {payment.user.get_full_name() or payment.user.get_username()},\n\n"
            "Your booking is confirmed. Your ticket is attached as a PDF.\n"
            f"Show: {local_show_time:%d %b %Y %I:%M %p IST} "
            f"at {payment.show.theater.name}.\n\n"
            "Please keep the attached QR ticket ready for verification at the venue."
        ),
        from_email=settings.DEFAULT_FROM_EMAIL,
        to=[payment.user.email],
    )
    email.attach(f"ticket-{payment.pk}.pdf", pdf, "application/pdf")
    sent = email.send(fail_silently=False)
    if sent != 1:
        raise RuntimeError(f"Ticket email for payment {payment.pk} was not accepted.")
    return sent
