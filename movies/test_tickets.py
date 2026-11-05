from datetime import timedelta
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import signing
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Booking, Movie, Payment, Seat, Show, Theater
from .tasks import send_booking_ticket


class TicketDeliveryTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ticketbuyer",
            password="test-password",
            email="buyer@example.com",
        )
        self.other_user = User.objects.create_user(
            username="otherbuyer",
            password="test-password",
            email="other@example.com",
        )
        self.movie = Movie.objects.create(name="Ticket Test Movie", image="test.jpg")
        self.theater = Theater.objects.create(
            name="Central Cinema", city="Pune", address="1 Main Street"
        )
        self.show = Show.objects.create(
            movie=self.movie,
            theater=self.theater,
            date_time=timezone.now() + timedelta(days=1),
            price=250,
            screen_name="Screen 4",
        )
        self.seat = Seat.objects.create(
            show=self.show, theater=self.theater, seat_number="B7", is_booked=True
        )
        self.payment = Payment.objects.create(
            user=self.user,
            show=self.show,
            seat_numbers="B7",
            amount=250,
            razorpay_order_id="order_ticket_test",
            razorpay_payment_id="pay_ticket_test",
            status=Payment.STATUS_SUCCESS,
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

    def test_owner_can_download_confirmed_ticket_pdf(self):
        self.client.force_login(self.user)
        response = self.client.get(
            reverse("download_ticket", args=[self.payment.pk])
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn(b"%PDF", response.content[:10])
        self.assertIn(
            f'ticket-{self.payment.pk}.pdf', response["Content-Disposition"]
        )

    def test_ticket_download_is_private_and_requires_confirmed_payment(self):
        self.client.force_login(self.other_user)
        self.assertEqual(
            self.client.get(reverse("download_ticket", args=[self.payment.pk])).status_code,
            404,
        )
        self.payment.status = Payment.STATUS_FAILED
        self.payment.save(update_fields=["status"])
        self.client.force_login(self.user)
        self.assertEqual(
            self.client.get(reverse("download_ticket", args=[self.payment.pk])).status_code,
            404,
        )

    def test_qr_verification_returns_ticket_details_without_personal_data(self):
        token = signing.dumps(self.payment.pk, salt="movies.ticket")
        response = self.client.get(reverse("verify_ticket", args=[token]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "valid": True,
                "booking_id": f"BMS-{self.booking.pk:08d}",
                "movie": "Ticket Test Movie",
                "theater": "Central Cinema",
                "screen": "Screen 4",
                "show_time": self.show.date_time.isoformat(),
                "seats": ["B7"],
            },
        )
        self.assertNotIn("buyer@example.com", response.content.decode())

    def test_invalid_qr_signature_is_rejected(self):
        response = self.client.get(reverse("verify_ticket", args=["invalid-token"]))
        self.assertEqual(response.status_code, 400)
        self.assertFalse(response.json()["valid"])

    @patch("movies.tasks.EmailMessage")
    def test_email_task_attaches_pdf_ticket(self, email_class):
        email = email_class.return_value
        email.send.return_value = 1
        self.assertEqual(send_booking_ticket.run(self.payment.pk), 1)
        self.assertEqual(email_class.call_args.kwargs["to"], ["buyer@example.com"])
        email.attach.assert_called_once()
        filename, content, mimetype = email.attach.call_args.args
        self.assertEqual(filename, f"ticket-{self.payment.pk}.pdf")
        self.assertEqual(mimetype, "application/pdf")
        self.assertTrue(content.startswith(b"%PDF"))
        self.assertEqual(send_booking_ticket.max_retries, 5)

    @patch("movies.views.send_booking_ticket.delay")
    def test_successful_payment_queues_ticket_after_commit(self, queue_email):
        payment = Payment.objects.create(
            user=self.user,
            show=self.show,
            seat_numbers="C1",
            amount=250,
            razorpay_order_id="order_new_ticket",
        )
        seat = Seat.objects.create(
            show=self.show, theater=self.theater, seat_number="C1"
        )
        payment.seats.add(seat)

        with self.captureOnCommitCallbacks(execute=True):
            from .views import _finalize_booking_for_payment

            result = _finalize_booking_for_payment(
                payment.pk, "pay_new_ticket", "verified-signature"
            )

        self.assertEqual(result["result"], "success")
        queue_email.assert_called_once_with(payment.pk)
        self.assertTrue(
            Booking.objects.filter(payment=payment, seat=seat).exists()
        )
