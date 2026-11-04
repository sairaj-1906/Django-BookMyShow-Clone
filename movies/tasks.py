import logging
from datetime import timedelta

from celery import shared_task
from django.core.mail import EmailMessage
from django.db.models import Q
from django.utils import timezone

from .models import Payment, PaymentRefund, SeatHold
from .payments import create_refund
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
        payment.ticket_email_status = Payment.EMAIL_FAILED
        payment.save(update_fields=["ticket_email_status", "updated_at"])
        logger.error(
            "Ticket email skipped for payment %s: user has no email address",
            payment_id,
        )
        return

    pdf = build_ticket_pdf(payment)
    payment.ticket_email_queued_at = timezone.now()
    payment.save(update_fields=["ticket_email_queued_at", "updated_at"])
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
    payment.ticket_email_status = Payment.EMAIL_SENT
    payment.ticket_email_sent_at = timezone.now()
    payment.save(
        update_fields=[
            "ticket_email_status",
            "ticket_email_sent_at",
            "updated_at",
        ]
    )


@shared_task(
    bind=True,
    autoretry_for=(Exception,),
    retry_backoff=True,
    retry_backoff_max=3600,
    retry_jitter=True,
    max_retries=5,
)
def process_payment_refund(self, refund_id):
    refund = (
        PaymentRefund.objects.select_related("payment")
        .filter(pk=refund_id)
        .first()
    )
    if refund is None or refund.status == PaymentRefund.STATUS_SUCCESS:
        return
    refund.status = PaymentRefund.STATUS_QUEUED
    refund.queued_at = timezone.now()
    refund.save(update_fields=["status", "queued_at", "updated_at"])
    payment = refund.payment
    if not payment.razorpay_payment_id:
        raise RuntimeError(
            f"Cannot refund payment {payment.pk} without a provider payment ID"
        )

    response = create_refund(
        payment.razorpay_payment_id,
        int(refund.amount * 100),
        refund.idempotency_key,
        notes={"reason": refund.reason, "payment_id": str(payment.pk)},
    )
    provider_status = response.get("status", "pending")
    status = (
        PaymentRefund.STATUS_SUCCESS
        if provider_status == "processed"
        else PaymentRefund.STATUS_FAILED
        if provider_status == "failed"
        else PaymentRefund.STATUS_PENDING
    )
    PaymentRefund.objects.filter(
        pk=refund.pk, status=PaymentRefund.STATUS_QUEUED
    ).update(
        razorpay_refund_id=response.get("id") or refund.razorpay_refund_id,
        failure_reason="",
        status=status,
        queued_at=None,
        submitted_at=timezone.now(),
        updated_at=timezone.now(),
    )


@shared_task
def release_expired_seat_holds():
    expired, _ = SeatHold.objects.filter(expires_at__lte=timezone.now()).delete()
    return expired


@shared_task
def enqueue_pending_ticket_emails():
    now = timezone.now()
    stale_before = now - timedelta(minutes=10)
    candidates = Payment.objects.filter(
        status=Payment.STATUS_SUCCESS,
        user__email__gt="",
        bookings__isnull=False,
    ).filter(
        Q(ticket_email_status=Payment.EMAIL_PENDING)
        | Q(
            ticket_email_status=Payment.EMAIL_QUEUED,
            ticket_email_queued_at__lte=stale_before,
        )
    ).values_list("pk", flat=True).distinct()

    queued_count = 0
    for payment_id in candidates.iterator(chunk_size=500):
        claimed = Payment.objects.filter(pk=payment_id).filter(
            Q(ticket_email_status=Payment.EMAIL_PENDING)
            | Q(
                ticket_email_status=Payment.EMAIL_QUEUED,
                ticket_email_queued_at__lte=stale_before,
            )
        ).update(
            ticket_email_status=Payment.EMAIL_QUEUED,
            ticket_email_queued_at=now,
        )
        if not claimed:
            continue
        try:
            send_booking_ticket_email.delay(payment_id)
        except Exception:
            Payment.objects.filter(
                pk=payment_id, ticket_email_status=Payment.EMAIL_QUEUED
            ).update(
                ticket_email_status=Payment.EMAIL_PENDING,
                ticket_email_queued_at=None,
            )
            logger.exception(
                "Could not queue ticket email for payment %s; it remains pending",
                payment_id,
            )
        else:
            queued_count += 1
    return queued_count


@shared_task
def enqueue_pending_payment_refunds():
    now = timezone.now()
    stale_before = now - timedelta(minutes=10)
    candidates = PaymentRefund.objects.filter(
        Q(status=PaymentRefund.STATUS_PENDING, submitted_at__isnull=True)
        | Q(
            status=PaymentRefund.STATUS_QUEUED,
            queued_at__lte=stale_before,
        )
    ).values_list("pk", flat=True)
    queued_count = 0
    for refund_id in candidates.iterator(chunk_size=500):
        claimed = PaymentRefund.objects.filter(pk=refund_id).filter(
            Q(status=PaymentRefund.STATUS_PENDING, submitted_at__isnull=True)
            | Q(
                status=PaymentRefund.STATUS_QUEUED,
                queued_at__lte=stale_before,
            )
        ).update(status=PaymentRefund.STATUS_QUEUED, queued_at=now)
        if not claimed:
            continue
        try:
            process_payment_refund.delay(refund_id)
        except Exception:
            PaymentRefund.objects.filter(
                pk=refund_id, status=PaymentRefund.STATUS_QUEUED
            ).update(status=PaymentRefund.STATUS_PENDING, queued_at=None)
            logger.exception(
                "Could not queue refund %s; it remains pending for retry",
                refund_id,
            )
        else:
            queued_count += 1
    return queued_count
