from urllib.parse import parse_qs, urlsplit
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth.models import User
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Booking, Movie, Payment, Seat, Show, Theater
from .tasks import send_booking_ticket_email
from .tickets import ticket_verification_url


class TicketFlowTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="ticketbuyer",
            email="buyer@example.com",
            password="test-password",
        )
        self.movie = Movie.objects.create(name="Ticket Test Movie", image="poster.jpg")
        self.theater = Theater.objects.create(
            name="Central Cinema", city="Mumbai", address="1 Cinema Road, Mumbai"
        )
        self.show = Show.objects.create(
            movie=self.movie,
            theater=self.theater,
            date_time=timezone.now() + timedelta(days=1),
            price=Decimal("250.00"),
            screen_name="Screen 4",
        )
        self.seat = Seat.objects.create(
            show=self.show, seat_number="G7", is_booked=True
        )
        self.payment = Payment.objects.create(
            user=self.user,
            show=self.show,
            seat_numbers="G7",
            amount=Decimal("250.00"),
            razorpay_order_id="order_ticket_test",
            razorpay_payment_id="pay_ticket_test",
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

    def test_ticket_download_requires_owner_and_returns_pdf(self):
        url = reverse("download_ticket", args=[self.payment.pk])
        self.assertEqual(self.client.get(url).status_code, 302)

        second_seat = Seat.objects.create(
            show=self.show, seat_number="G8", is_booked=True
        )
        self.payment.seats.add(second_seat)
        Booking.objects.create(
            user=self.user,
            seat=second_seat,
            movie=self.movie,
            theater=self.theater,
            show=self.show,
            payment=self.payment,
        )
        other = User.objects.create_user("other", password="test-password")
        self.client.force_login(other)
        self.assertEqual(self.client.get(url).status_code, 404)

        self.client.force_login(self.user)
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

    def test_booking_history_links_to_ticket_download(self):
        self.client.force_login(self.user)
        response = self.client.get(reverse("payment_history"))

        self.assertEqual(response.status_code, 200)
        self.assertContains(
            response,
            reverse("download_ticket", args=[self.payment.pk]),
        )
        self.assertContains(response, "Download PDF")

    @override_settings(PUBLIC_BASE_URL="https://cinema.example")
    def test_signed_qr_verification_confirms_booking_and_rejects_bad_token(self):
        ticket_url = ticket_verification_url(self.payment)
        self.assertTrue(ticket_url.startswith("https://cinema.example/"))
        token = parse_qs(urlsplit(ticket_url).query)["token"][0]
        verify_url = reverse("verify_ticket", args=[self.payment.pk])

        response = self.client.get(verify_url, {"token": token})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "Ticket verified")
        self.assertContains(response, "Ticket Test Movie")
        self.assertContains(response, "G7")
        self.assertContains(response, "Screen 4")

        response = self.client.get(verify_url, {"token": "not-a-valid-token"})
        self.assertEqual(response.status_code, 404)
        self.assertContains(response, "could not be verified", status_code=404)

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend")
    def test_celery_task_emails_pdf_ticket(self):
        send_booking_ticket_email.run(self.payment.pk)
        self.payment.refresh_from_db()

        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(self.payment.ticket_email_status, Payment.EMAIL_SENT)
        self.assertEqual(mail.outbox[0].to, [self.user.email])
        self.assertEqual(len(mail.outbox[0].attachments), 1)
        filename, attachment, content_type = mail.outbox[0].attachments[0]
        self.assertEqual(filename, f"booking-{self.payment.pk}-ticket.pdf")
        self.assertEqual(content_type, "application/pdf")
        self.assertTrue(attachment.startswith(b"%PDF"))
        self.assertEqual(send_booking_ticket_email.max_retries, 5)

    @patch("movies.views.send_booking_ticket_email.delay")
    @patch("movies.views.verify_payment_signature")
    def test_successful_payment_queues_ticket_email_after_booking_commit(
        self, verify_signature, enqueue_email
    ):
        self.booking.delete()
        self.seat.is_booked = False
        self.seat.save(update_fields=["is_booked"])
        self.payment.status = Payment.STATUS_CREATED
        self.payment.paid_at = None
        self.payment.save(update_fields=["status", "paid_at"])
        self.client.force_login(self.user)
        url = reverse("verify_payment", args=[self.payment.pk])

        with self.captureOnCommitCallbacks(execute=True):
            response = self.client.post(
                url,
                {
                    "razorpay_order_id": self.payment.razorpay_order_id,
                    "razorpay_payment_id": "pay_ticket_test",
                    "razorpay_signature": "valid-signature",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        self.assertEqual(Booking.objects.filter(payment=self.payment).count(), 1)
        verify_signature.assert_called_once()
        enqueue_email.assert_called_once_with(self.payment.pk)
