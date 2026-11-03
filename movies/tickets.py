from io import BytesIO
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

import qrcode
from django.conf import settings
from django.core import signing
from django.urls import reverse
from django.utils import timezone
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas


TICKET_SIGNING_SALT = "movies.ticket-verification"


def ticket_verification_url(payment):
    token = signing.dumps(payment.pk, salt=TICKET_SIGNING_SALT)
    path = reverse("verify_ticket", args=[payment.pk])
    return (
        f"{settings.PUBLIC_BASE_URL.rstrip('/')}{path}?"
        f"{urlencode({'token': token})}"
    )


def is_valid_ticket_token(payment, token):
    try:
        return signing.loads(token, salt=TICKET_SIGNING_SALT) == payment.pk
    except signing.BadSignature:
        return False


def build_ticket_pdf(payment):
    bookings = list(
        payment.bookings.select_related("seat").order_by("seat__seat_number")
    )
    show = payment.show
    local_show_time = timezone.localtime(
        show.date_time, ZoneInfo("Asia/Kolkata")
    )
    width, height = A4
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)

    pdf.setFillColor(colors.HexColor("#17152b"))
    pdf.rect(0, height - 150, width, 150, fill=1, stroke=0)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 24)
    pdf.drawString(48, height - 65, "BOOKMYSEAT")
    pdf.setFont("Helvetica", 12)
    pdf.drawString(48, height - 91, "MOVIE ADMISSION TICKET")
    pdf.setFont("Helvetica-Bold", 18)
    pdf.drawString(48, height - 128, show.movie.name[:48])

    qr_image = qrcode.make(ticket_verification_url(payment))
    qr_buffer = BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)
    pdf.drawImage(
        ImageReader(qr_buffer),
        width - 150,
        height - 340,
        width=100,
        height=100,
        preserveAspectRatio=True,
        mask="auto",
    )
    pdf.setFillColor(colors.HexColor("#17152b"))
    pdf.setFont("Helvetica-Bold", 12)
    pdf.drawString(width - 158, height - 359, "SCAN TO VERIFY")

    details = [
        ("BOOKING ID", f"BMS-{payment.pk:08d}"),
        ("BOOKING RECORDS", ", ".join(f"#{booking.pk}" for booking in bookings)),
        ("THEATER", show.theater.name),
        ("ADDRESS", show.theater.address or show.theater.city or "India"),
        ("SCREEN", show.screen_name),
        ("SHOW TIME", local_show_time.strftime("%a, %d %b %Y  |  %I:%M %p IST")),
        ("LANGUAGE / FORMAT", f"{show.language or 'N/A'}  |  {show.screen_format}"),
        ("SEATS", ", ".join(booking.seat.seat_number for booking in bookings)),
        ("AMOUNT PAID", f"Rs {payment.amount} {payment.currency}"),
        ("PAYMENT REFERENCE", payment.razorpay_payment_id or payment.razorpay_order_id),
    ]
    y = height - 202
    for detail_index, (label, value) in enumerate(details):
        lines = simpleSplit(value, "Helvetica", 11, 345)
        row_height = 52 + max(0, len(lines) - 1) * 14
        if y - row_height < 80 and detail_index > 0:
            pdf.showPage()
            y = height - 60
            pdf.setFillColor(colors.HexColor("#17152b"))
            pdf.setFont("Helvetica-Bold", 16)
            pdf.drawString(48, y, f"BOOKING BMS-{payment.pk:08d} (CONTINUED)")
            y -= 48

        pdf.setFillColor(colors.HexColor("#6b6878"))
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(48, y, label)
        pdf.setFillColor(colors.HexColor("#17152b"))
        pdf.setFont("Helvetica", 11)
        for line_index, line in enumerate(lines):
            pdf.drawString(48, y - 17 - (line_index * 14), line)
        y -= row_height

    if y > 90:
        pdf.setStrokeColor(colors.HexColor("#dedce6"))
        pdf.line(48, 65, width - 48, 65)
        pdf.setFillColor(colors.HexColor("#6b6878"))
        pdf.setFont("Helvetica", 9)
        pdf.drawString(
            48, 46, "Please arrive before the show starts. This ticket is valid for the listed seats."
        )
    pdf.save()
    return output.getvalue()
