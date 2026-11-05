from io import BytesIO
from zoneinfo import ZoneInfo

import qrcode
from django.conf import settings
from django.core import signing
from django.urls import reverse
from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.utils import ImageReader, simpleSplit
from reportlab.pdfgen import canvas


def render_ticket_pdf(payment):
    bookings = list(
        payment.bookings.select_related("seat").order_by("seat__seat_number")
    )
    if not bookings:
        raise ValueError(f"Payment {payment.pk} has no confirmed bookings.")

    booking_id = f"BMS-{bookings[0].pk:08d}"
    show = payment.show
    local_show_time = show.date_time.astimezone(ZoneInfo("Asia/Kolkata"))
    token = signing.dumps(payment.pk, salt="movies.ticket")
    verification_url = (
        f"{settings.SITE_URL}{reverse('verify_ticket', args=[token])}"
    )

    qr_image = qrcode.make(verification_url)
    qr_buffer = BytesIO()
    qr_image.save(qr_buffer, format="PNG")
    qr_buffer.seek(0)

    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    page_width, page_height = A4
    margin = 44

    pdf.setFillColor(colors.HexColor("#17152f"))
    pdf.roundRect(margin, page_height - 145, page_width - 2 * margin, 100, 12, fill=1)
    pdf.setFillColor(colors.white)
    pdf.setFont("Helvetica-Bold", 22)
    pdf.drawString(margin + 22, page_height - 82, "BOOKMYSEAT")
    pdf.setFont("Helvetica", 11)
    pdf.drawString(margin + 22, page_height - 105, "MOVIE ADMISSION TICKET")

    y = page_height - 185
    pdf.setFillColor(colors.HexColor("#77738a"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(margin, y, "BOOKING ID")
    pdf.drawString(page_width - margin - 132, y, "PAYMENT REFERENCE")
    y -= 18
    pdf.setFillColor(colors.HexColor("#17152f"))
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(margin, y, booking_id)
    pdf.setFont("Helvetica", 9)
    reference = payment.razorpay_payment_id or payment.razorpay_order_id
    pdf.drawString(page_width - margin - 132, y + 2, reference[:26])

    y -= 42
    details = [
        ("MOVIE", show.movie.name),
        ("THEATER", show.theater.name),
        ("ADDRESS", show.theater.address or show.theater.city or "India"),
        ("SCREEN", show.screen_name),
        ("SHOW TIME", local_show_time.strftime("%a, %d %b %Y - %I:%M %p IST")),
        ("FORMAT / LANGUAGE", show.get_screen_format_display()),
        ("SEATS", ", ".join(booking.seat.seat_number for booking in bookings)),
    ]
    for label, value in details:
        pdf.setFillColor(colors.HexColor("#77738a"))
        pdf.setFont("Helvetica-Bold", 8)
        pdf.drawString(margin, y, label)
        pdf.setFillColor(colors.HexColor("#17152f"))
        pdf.setFont("Helvetica", 12)
        lines = simpleSplit(str(value), "Helvetica", 12, page_width - 2 * margin - 12)
        for line in lines:
            y -= 17
            pdf.drawString(margin, y, line)
        y -= 26

    pdf.setStrokeColor(colors.HexColor("#dedbe8"))
    pdf.line(margin, y, page_width - margin, y)
    pdf.setFillColor(colors.HexColor("#77738a"))
    pdf.setFont("Helvetica", 9)
    pdf.drawString(margin, y - 22, "TOTAL PAID")
    pdf.setFillColor(colors.HexColor("#17152f"))
    pdf.setFont("Helvetica-Bold", 13)
    pdf.drawString(margin + 78, y - 22, f"INR {payment.amount}")
    pdf.drawImage(
        ImageReader(qr_buffer),
        page_width - margin - 102,
        y - 115,
        width=92,
        height=92,
        preserveAspectRatio=True,
    )
    pdf.setFillColor(colors.HexColor("#77738a"))
    pdf.setFont("Helvetica", 8)
    pdf.drawString(margin, 38, "Present this QR code at the venue for ticket verification.")
    pdf.save()
    return output.getvalue()
