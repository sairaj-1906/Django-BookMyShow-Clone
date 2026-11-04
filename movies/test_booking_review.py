from datetime import timedelta
from decimal import Decimal
import json
from unittest.mock import patch

from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import (
    Booking,
    Movie,
    Payment,
    PaymentRefund,
    Review,
    Seat,
    SeatHold,
    Show,
    Theater,
)
from .tasks import (
    enqueue_pending_payment_refunds,
    enqueue_pending_ticket_emails,
    process_payment_refund,
    release_expired_seat_holds,
)
from .views import _finalize_booking_for_payment, _queue_ticket_email


class BookingReviewTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="viewer", password="test-pass", email="viewer@example.com"
        )
        self.other = User.objects.create_user(username="other", password="test-pass")
        self.movie = Movie.objects.create(name="Completed Movie")
        self.theater = Theater.objects.create(name="Test Cinema", city="Pune")
        self.show = Show.objects.create(
            movie=self.movie,
            theater=self.theater,
            date_time=timezone.now() - timedelta(hours=2),
            price=Decimal("200.00"),
        )
        self.seat = Seat.objects.create(show=self.show, seat_number="A1")
        self.payment = Payment.objects.create(
            user=self.user,
            show=self.show,
            seat_numbers="A1",
            amount=Decimal("200.00"),
            razorpay_order_id="order-review-test",
            razorpay_payment_id="pay-review-test",
            status=Payment.STATUS_SUCCESS,
            paid_at=timezone.now(),
        )
        self.payment.seats.add(self.seat)
        self.booking = Booking.objects.create(
            user=self.user,
            seat=self.seat,
            movie=self.movie,
            theater=self.theater,
            show=self.show,
            payment=self.payment,
        )

    def test_only_paid_completed_show_bookings_can_be_reviewed(self):
        self.client.force_login(self.user)
        url = reverse("submit_review", args=[self.movie.pk])
        self.assertEqual(
            self.client.post(
                url, {"rating": "5", "title": "Good", "body": "Enjoyed it"}
            ).status_code,
            302,
        )
        review = Review.objects.get(movie=self.movie, user=self.user)
        self.assertTrue(review.is_verified_viewer)
        self.movie.refresh_from_db()
        self.assertEqual(self.movie.average_rating, Decimal("5.00"))

    def test_future_or_unpaid_show_does_not_unlock_a_review(self):
        unwatched_movie = Movie.objects.create(name="Not Watched")
        future = Show.objects.create(
            movie=unwatched_movie,
            theater=self.theater,
            date_time=timezone.now() + timedelta(days=1),
            price=Decimal("200.00"),
        )
        seat = Seat.objects.create(show=future, seat_number="A2")
        unpaid = Payment.objects.create(
            user=self.user,
            show=future,
            seat_numbers="A2",
            amount=Decimal("200.00"),
            razorpay_order_id="order-unpaid-review",
            status=Payment.STATUS_CREATED,
        )
        unpaid.seats.add(seat)
        Booking.objects.create(
            user=self.user,
            seat=seat,
            movie=unwatched_movie,
            theater=self.theater,
            show=future,
            payment=unpaid,
        )
        self.client.force_login(self.user)
        response = self.client.post(
            reverse("submit_review", args=[unwatched_movie.pk]),
            {"rating": "5", "body": "Not eligible"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertFalse(Review.objects.filter(user=self.user, movie=unwatched_movie).exists())

    def test_live_holds_are_exclusive_and_can_be_released(self):
        hold_show = Show.objects.create(
            movie=self.movie,
            theater=self.theater,
            date_time=timezone.now() + timedelta(days=1),
            price=Decimal("200.00"),
        )
        first_seat = Seat.objects.create(show=hold_show, seat_number="A1")
        second_seat = Seat.objects.create(show=hold_show, seat_number="A2")
        hold_url = reverse("hold_seats", args=[hold_show.pk])
        self.client.force_login(self.user)
        response = self.client.post(hold_url, {"seats": [first_seat.pk]})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SeatHold.objects.filter(seat=first_seat, user=self.user).exists())
        self.client.force_login(self.other)
        response = self.client.post(hold_url, {"seats": [first_seat.pk, second_seat.pk]})
        self.assertEqual(response.status_code, 409)
        self.assertFalse(SeatHold.objects.filter(seat=second_seat).exists())
        self.assertFalse(SeatHold.objects.filter(seat=first_seat, user=self.other).exists())

    @patch("movies.views.process_payment_refund.delay")
    def test_captured_payment_conflict_never_creates_partial_booking(self, queue_refund):
        available = Seat.objects.create(show=self.show, seat_number="A2")
        occupied = Seat.objects.create(show=self.show, seat_number="A3", is_booked=True)
        Booking.objects.create(
            user=self.other,
            seat=occupied,
            movie=self.movie,
            theater=self.theater,
            show=self.show,
        )
        payment = Payment.objects.create(
            user=self.user,
            show=self.show,
            seat_numbers="A2, A3",
            amount=Decimal("400.00"),
            razorpay_order_id="order-conflict-test",
            razorpay_payment_id="pay-conflict-test",
        )
        payment.seats.add(available, occupied)
        with self.captureOnCommitCallbacks(execute=True):
            result = _finalize_booking_for_payment(payment.pk, "pay-conflict-test")

        self.assertEqual(result["result"], "refund_pending")
        self.assertFalse(Booking.objects.filter(seat=available).exists())
        refund = PaymentRefund.objects.get(payment=payment)
        self.assertEqual(refund.amount, payment.amount)
        self.assertEqual(payment.__class__.objects.get(pk=payment.pk).status, Payment.STATUS_SUCCESS)
        queue_refund.assert_called_once_with(refund.pk)

    def test_duplicate_payment_confirmation_does_not_duplicate_bookings_or_refunds(self):
        seat = Seat.objects.create(show=self.show, seat_number="A2")
        payment = Payment.objects.create(
            user=self.user,
            show=self.show,
            seat_numbers="A2",
            amount=Decimal("200.00"),
            razorpay_order_id="order-idempotency-test",
            razorpay_payment_id="pay-idempotency-test",
        )
        payment.seats.add(seat)
        first = _finalize_booking_for_payment(payment.pk, "pay-idempotency-test")
        second = _finalize_booking_for_payment(payment.pk, "pay-idempotency-test")

        self.assertEqual(first["result"], "success")
        self.assertEqual(second["result"], "already_processed")
        self.assertEqual(Booking.objects.filter(payment=payment).count(), 1)
        self.assertFalse(PaymentRefund.objects.filter(payment=payment).exists())

    @patch("movies.tasks.create_refund")
    def test_refund_task_records_provider_reference_and_status(self, create_refund):
        create_refund.return_value = {"id": "rfnd_test", "status": "processed"}
        refund = PaymentRefund.objects.create(
            payment=self.payment,
            amount=Decimal("200.00"),
            idempotency_key="test-refund",
            reason="Seat conflict",
        )

        process_payment_refund.run(refund.pk)
        refund.refresh_from_db()

        self.assertEqual(refund.status, PaymentRefund.STATUS_SUCCESS)
        self.assertEqual(refund.razorpay_refund_id, "rfnd_test")
        create_refund.assert_called_once_with(
            "pay-review-test",
            20000,
            "test-refund",
            notes={"reason": "Seat conflict", "payment_id": str(self.payment.pk)},
        )

    @patch("movies.views.send_booking_ticket_email.delay", side_effect=RuntimeError("broker down"))
    def test_ticket_email_enqueue_failure_stays_pending(self, enqueue):
        with self.assertLogs("movies.views", level="ERROR"):
            _queue_ticket_email(self.payment.pk)

        self.payment.refresh_from_db()
        self.assertEqual(self.payment.ticket_email_status, Payment.EMAIL_PENDING)
        enqueue.assert_called_once_with(self.payment.pk)

    @patch("movies.tasks.send_booking_ticket_email.delay")
    def test_periodic_dispatch_enqueues_pending_ticket_email(self, enqueue):
        queued = enqueue_pending_ticket_emails.run()

        self.payment.refresh_from_db()
        self.assertEqual(queued, 1)
        self.assertEqual(self.payment.ticket_email_status, Payment.EMAIL_QUEUED)
        enqueue.assert_called_once_with(self.payment.pk)

    @patch("movies.tasks.process_payment_refund.delay")
    def test_periodic_dispatch_enqueues_pending_refund(self, enqueue):
        refund = PaymentRefund.objects.create(
            payment=self.payment,
            amount=Decimal("200.00"),
            idempotency_key="pending-refund-test",
        )

        queued = enqueue_pending_payment_refunds.run()
        refund.refresh_from_db()

        self.assertEqual(queued, 1)
        self.assertEqual(refund.status, PaymentRefund.STATUS_QUEUED)
        enqueue.assert_called_once_with(refund.pk)

    @patch("movies.views.verify_webhook_signature")
    def test_refund_webhook_updates_status_by_receipt(self, verify_signature):
        refund = PaymentRefund.objects.create(
            payment=self.payment,
            amount=Decimal("200.00"),
            idempotency_key="webhook-refund-test",
            status=PaymentRefund.STATUS_QUEUED,
        )
        response = self.client.post(
            reverse("razorpay_webhook"),
            data=json.dumps(
                {
                    "event": "refund.processed",
                    "payload": {
                        "refund": {
                            "entity": {
                                "id": "rfnd_webhook_test",
                                "receipt": refund.idempotency_key,
                            }
                        }
                    },
                }
            ),
            content_type="application/json",
            HTTP_X_RAZORPAY_SIGNATURE="valid-signature",
        )

        self.assertEqual(response.status_code, 200)
        refund.refresh_from_db()
        self.assertEqual(refund.status, PaymentRefund.STATUS_SUCCESS)
        self.assertEqual(refund.razorpay_refund_id, "rfnd_webhook_test")
        verify_signature.assert_called_once()

    def test_periodic_cleanup_deletes_expired_holds(self):
        hold_seat = Seat.objects.create(show=self.show, seat_number="A9")
        hold = SeatHold.objects.create(
            seat=hold_seat,
            show=self.show,
            user=self.user,
            expires_at=timezone.now() - timedelta(seconds=1),
        )

        self.assertEqual(release_expired_seat_holds.run(), 1)
        self.assertFalse(SeatHold.objects.filter(pk=hold.pk).exists())
