import logging

from celery import shared_task
from django.core.mail import EmailMessage

from .models import Payment
from .tickets import build_ticket_pdf

logger = logging.getLogger(__name__)


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=5,
)
def send_booking_ticket_email(self, payment_id):
    payment = (
        Payment.objects.filter(
            pk=payment_id,
            status=Payment.STATUS_SUCCESS,
        )
        .select_related("user", "show__movie", "show__theater", "show__language")
        .prefetch_related("bookings__seat")
        .first()
    )
    if payment is None or not payment.bookings.exists():
        logger.warning(
            "Ticket email skipped for payment %s without a confirmed booking",
            payment_id,
        )
        return
    if not payment.user.email:
        logger.error(
            "Ticket email skipped for payment %s: user has no email address",
            payment_id,
        )
        return

    pdf = build_ticket_pdf(payment)
    message = EmailMessage(
        subject=f"Your {payment.show.movie.name} tickets — BMS-{payment.pk:08d}",
        body=(
            f"Hi {payment.user.get_full_name() or payment.user.username},\n\n"
            f"Your booking for {payment.show.movie.name} is confirmed. "
            "Your ticket is attached to this email. You can also download it "
            "from your booking history.\n\n"
            "Enjoy the show!\nBookMySeat"
        ),
        to=[payment.user.email],
    )
    message.attach(
        f"booking-{payment.pk}-ticket.pdf", pdf, "application/pdf"
    )
    if not message.send():
        raise RuntimeError(f"Ticket email delivery failed for payment {payment.pk}")
