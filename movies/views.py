import json
import logging
import time

from django.conf import settings
from django.core.paginator import Paginator
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.http import HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_POST

from . import discovery
from .forms import ReviewForm, ReviewReportForm
from .models import Booking, Movie, Payment, Review, Seat, SeatHold, Show, Theater
from .payments import (
    PaymentVerificationError,
    create_order,
    verify_payment_signature,
    verify_webhook_signature,
)


logger = logging.getLogger(__name__)


def _run_atomic_with_retry(fn, attempts=5, base_delay=0.05):
    """Run fn() inside a transaction, retrying a few times on a transient 'database is
    locked' error. Postgres (used in production) resolves this contention with real
    row-level locks and never hits this path — it only matters for SQLite in local dev,
    which serializes writes at the file level instead of the row level."""
    last_error = None
    for attempt in range(attempts):
        try:
            with transaction.atomic():
                return fn()
        except OperationalError as exc:
            if "locked" not in str(exc).lower():
                raise
            last_error = exc
            time.sleep(base_delay * (attempt + 1))
    raise last_error


def movie_list(request):
    """Movie discovery: title search, filters, sorting, live result count and pagination.

    Normal requests render the whole page. The page's JavaScript re-requests the same URL with
    an `X-Requested-With: XMLHttpRequest` header every time a filter changes and gets back
    JSON ({count, html, url}) so the result count and grid update without a reload — while the
    page still works as plain links/forms if JavaScript is off."""
    form, filters = discovery.parse(request.GET)
    paginator = Paginator(discovery.discovery_queryset(filters), discovery.PAGE_SIZE)
    page_obj = paginator.get_page(filters.page)

    context = {
        "form": form,
        "filters": filters,
        "page_obj": page_obj,
        "total": paginator.count,
        "chips": discovery.active_chips(filters, form),
        "page_links": discovery.page_links(page_obj, filters) if paginator.num_pages > 1 else [],
        "clear_url": reverse("movie_list"),
    }
    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JsonResponse(
            {
                "count": paginator.count,
                "html": render_to_string("movies/_discover_results.html", context, request=request),
                "url": discovery.build_url(discovery.canonical_pairs(filters), page_obj.number),
            }
        )

    first_page_unfiltered = not filters.active and page_obj.number == 1
    if first_page_unfiltered:
        context["recommended"] = discovery.recommend_for(request, limit=8)
        context["recently_viewed"] = discovery.recently_viewed(request, limit=6)
    return render(request, "movies/movie_list.html", context)


def movie_detail(request, movie_id):
    movie = get_object_or_404(
        Movie.objects.prefetch_related(
            "genres", "languages", "posters", "credits__cast_member"
        ),
        id=movie_id,
    )
    reviews = (
        movie.reviews.filter(is_hidden=False)
        .select_related("user")
        .order_by("-created_at")
    )

    user_review = None
    can_review = False
    review_form = None

    if request.user.is_authenticated:
        user_review = movie.reviews.filter(user=request.user).first()
        eligible_booking = (
            Booking.objects.filter(user=request.user, movie=movie)
            .order_by("-booked_at")
            .first()
        )
        can_review = bool(eligible_booking and eligible_booking.has_been_watched())
        if can_review and not user_review:
            review_form = ReviewForm()

    discovery.record_view(request, movie)  # feeds "Recommended for You"

    context = {
        "movie": movie,
        "reviews": reviews,
        "user_review": user_review,
        "can_review": can_review,
        "review_form": review_form,
        "report_form": ReviewReportForm(),
        "similar_movies": movie.similar_movies(),
        "trending_movies": Movie.trending(),
        "recent_movies": Movie.recently_released(),
    }
    return render(request, "movies/movie_detail.html", context)


@login_required(login_url="/login/")
def submit_review(request, movie_id):
    movie = get_object_or_404(Movie, id=movie_id)

    if request.method != "POST":
        return redirect("movie_detail", movie_id=movie.id)

    eligible_booking = (
        Booking.objects.filter(user=request.user, movie=movie)
        .order_by("-booked_at")
        .first()
    )
    if not eligible_booking or not eligible_booking.has_been_watched():
        messages.error(
            request, "You can review a movie only after booking and watching it."
        )
        return redirect("movie_detail", movie_id=movie.id)

    if Review.objects.filter(movie=movie, user=request.user).exists():
        messages.info(
            request, "You've already reviewed this movie — you can edit it below."
        )
        return redirect("movie_detail", movie_id=movie.id)

    form = ReviewForm(request.POST)
    if form.is_valid():
        review = form.save(commit=False)
        review.movie = movie
        review.user = request.user
        review.booking = eligible_booking
        review.is_verified_viewer = True
        review.save()
        messages.success(request, "Thanks — your review has been saved.")
    else:
        messages.error(request, "Please correct the errors in your review.")
    return redirect("movie_detail", movie_id=movie.id)


@login_required(login_url="/login/")
def edit_review(request, review_id):
    review = get_object_or_404(Review, id=review_id, user=request.user)
    if request.method == "POST":
        form = ReviewForm(request.POST, instance=review)
        if form.is_valid():
            form.save()
            messages.success(request, "Your review has been updated.")
    return redirect("movie_detail", movie_id=review.movie_id)


@login_required(login_url="/login/")
def delete_review(request, review_id):
    review = get_object_or_404(Review, id=review_id, user=request.user)
    movie_id = review.movie_id
    if request.method == "POST":
        review.delete()
        messages.success(request, "Your review has been deleted.")
    return redirect("movie_detail", movie_id=movie_id)


@login_required(login_url="/login/")
def report_review(request, review_id):
    review = get_object_or_404(Review, id=review_id)
    if request.method == "POST":
        form = ReviewReportForm(request.POST)
        if form.is_valid():
            report = form.save(commit=False)
            report.review = review
            report.reported_by = request.user
            try:
                report.save()
                messages.success(request, "Thanks — we'll take a look at this review.")
            except IntegrityError:
                messages.info(request, "You've already reported this review.")
    return redirect("movie_detail", movie_id=review.movie_id)


def show_list(request, movie_id):
    """List upcoming show schedules (theater + time + language + format) for a movie."""
    movie = get_object_or_404(Movie, id=movie_id)
    shows = (
        Show.objects.filter(movie=movie, date_time__gte=timezone.now())
        .select_related("theater", "language")
        .order_by("date_time")
    )
    # Backward compatibility: surface any legacy Theater rows that were never migrated to Show.
    legacy_theaters = Theater.objects.filter(movie=movie, time__isnull=False)
    return render(
        request,
        "movies/theater_list.html",
        {"movie": movie, "shows": shows, "legacy_theaters": legacy_theaters},
    )


def _purge_expired_holds(show=None):
    """Lazily release any holds whose 2-minute window has passed. Cheap and safe to call
    on every read/write path that touches seat availability, so a stale hold never blocks
    another user for longer than the hold window even without a background job running.
    """
    qs = SeatHold.objects.filter(expires_at__lte=timezone.now())
    if show is not None:
        qs = qs.filter(show=show)
    qs.delete()


def _seat_status_payload(show, user):
    _purge_expired_holds(show)
    seats = (
        Seat.objects.filter(show=show)
        .select_related("hold", "hold__user")
        .order_by("seat_number")
    )
    payload = {}
    for seat in seats:
        hold = seat.hold if hasattr(seat, "hold") else None
        if hold is not None and hold.is_expired():
            hold = None
        if seat.is_booked:
            status = "booked"
        elif hold is None:
            status = "available"
        elif user.is_authenticated and hold.user_id == user.id:
            status = "selected"
        else:
            status = "reserved"
        payload[str(seat.id)] = {
            "seat_number": seat.seat_number,
            "status": status,
            "expires_at": (
                hold.expires_at.isoformat() if (hold and status == "selected") else None
            ),
        }
    return payload


@login_required(login_url="/login/")
def book_seats(request, show_id):
    """Render the live seat map. Seat selection itself happens via AJAX calls to
    hold_seats/release_seats so availability can update in real time without a page reload.
    """
    show = get_object_or_404(Show, id=show_id)
    _purge_expired_holds(show)
    seats = (
        Seat.objects.filter(show=show).select_related("hold").order_by("seat_number")
    )
    my_held_seats = [
        seat
        for seat in seats
        if getattr(seat, "hold", None)
        and seat.hold.user_id == request.user.id
        and not seat.hold.is_expired()
    ]
    return render(
        request,
        "movies/seat_selection.html",
        {
            "show": show,
            "seats": seats,
            "my_held_seat_ids": [s.id for s in my_held_seats],
            "hold_minutes": int(SeatHold.HOLD_DURATION.total_seconds() // 60),
        },
    )


@login_required(login_url="/login/")
def seat_status(request, show_id):
    """Polled by the seat map's JavaScript every few seconds for live availability."""
    show = get_object_or_404(Show, id=show_id)
    return JsonResponse({"seats": _seat_status_payload(show, request.user)})


@login_required(login_url="/login/")
@require_POST
def hold_seats(request, show_id):
    """Temporarily reserve one or more seats for this user for SeatHold.HOLD_DURATION.

    Wrapped in a transaction with row-level locking (select_for_update) so that when two
    users click the same seat at the same instant, one of them is made to wait for the
    other's transaction to finish and then sees the seat is already taken — never a
    duplicate hold. The OneToOneField unique constraint on SeatHold.seat is a second,
    database-enforced backstop against duplicates even where row locking isn't available.
    """
    show = get_object_or_404(Show, id=show_id)
    try:
        seat_ids = [int(sid) for sid in request.POST.getlist("seats")]
    except (TypeError, ValueError):
        return JsonResponse({"success": False, "error": "Invalid seat id."}, status=400)

    if not seat_ids:
        return JsonResponse(
            {"success": False, "error": "No seats specified."}, status=400
        )

    _purge_expired_holds(show)

    unavailable = []
    expires_at = SeatHold.new_expiry()

    def _do_hold():
        seats = list(
            Seat.objects.select_for_update()
            .filter(id__in=seat_ids, show=show)
            .order_by(
                "id"
            )  # consistent lock ordering avoids deadlocks between concurrent requests
        )
        found_ids = {seat.id for seat in seats}
        for missing_id in set(seat_ids) - found_ids:
            unavailable.append(str(missing_id))

        for seat in seats:
            if seat.is_booked:
                unavailable.append(seat.seat_number)
                continue

            existing_hold = (
                SeatHold.objects.select_for_update().filter(seat=seat).first()
            )
            if existing_hold and existing_hold.is_expired():
                existing_hold.delete()
                existing_hold = None

            if existing_hold and existing_hold.user_id != request.user.id:
                unavailable.append(seat.seat_number)
                continue

            if existing_hold and existing_hold.user_id == request.user.id:
                continue  # already held by this user — leave their original expiry in place

            try:
                SeatHold.objects.create(
                    seat=seat, show=show, user=request.user, expires_at=expires_at
                )
            except IntegrityError:
                # Someone else's hold slipped in between our check and our insert.
                unavailable.append(seat.seat_number)

    try:
        _run_atomic_with_retry(_do_hold)
    except OperationalError:
        return JsonResponse(
            {
                "success": False,
                "error": "The seat map is busy — please try again in a moment.",
            },
            status=503,
        )

    if unavailable:
        return JsonResponse(
            {
                "success": False,
                "error": f"These seats just became unavailable: {', '.join(unavailable)}",
                "seats": _seat_status_payload(show, request.user),
            },
            status=409,
        )

    return JsonResponse(
        {"success": True, "seats": _seat_status_payload(show, request.user)}
    )


@login_required(login_url="/login/")
@require_POST
def release_seats(request, show_id):
    """Let a user deselect seats (or all of their held seats) before paying."""
    show = get_object_or_404(Show, id=show_id)
    seat_ids = request.POST.getlist("seats")

    def _do_release():
        holds = SeatHold.objects.select_for_update().filter(
            show=show, user=request.user
        )
        if seat_ids:
            holds = holds.filter(seat_id__in=seat_ids)
        holds.delete()

    try:
        _run_atomic_with_retry(_do_release)
    except OperationalError:
        return JsonResponse(
            {
                "success": False,
                "error": "The seat map is busy — please try again in a moment.",
            },
            status=503,
        )
    return JsonResponse(
        {"success": True, "seats": _seat_status_payload(show, request.user)}
    )


@login_required(login_url="/login/")
def checkout(request, show_id):
    """Create (or resume) a Razorpay order for the seats the user currently has on hold,
    and render the payment page embedding Razorpay's Checkout.js widget."""
    show = get_object_or_404(Show, id=show_id)
    _purge_expired_holds(show)
    holds = list(
        SeatHold.objects.filter(show=show, user=request.user)
        .select_related("seat")
        .order_by("seat__seat_number")
    )
    if not holds:
        messages.info(
            request, "Your seat selection has expired. Please choose your seats again."
        )
        return redirect("book_seats", show_id=show.id)

    seats = [hold.seat for hold in holds]
    seat_ids = sorted(seat.id for seat in seats)
    total_price = show.price * len(seats)
    earliest_expiry = min(hold.expires_at for hold in holds)

    # Reuse a still-pending order for the exact same seats (e.g. the user refreshed the
    # page) instead of creating a brand-new Razorpay order on every load.
    existing = (
        Payment.objects.filter(
            user=request.user, show=show, status=Payment.STATUS_CREATED
        )
        .prefetch_related("seats")
        .order_by("-created_at")
        .first()
    )
    if existing and sorted(s.id for s in existing.seats.all()) == seat_ids:
        payment = existing
    else:
        if not (settings.RAZORPAY_KEY_ID and settings.RAZORPAY_KEY_SECRET):
            messages.error(
                request,
                "Online payments are not configured yet (RAZORPAY_KEY_ID / RAZORPAY_KEY_SECRET are not set).",
            )
            return redirect("book_seats", show_id=show.id)
        try:
            order = create_order(
                amount_in_paise=int(total_price * 100),
                currency="INR",
                receipt=f"show{show.id}-user{request.user.id}-{int(timezone.now().timestamp())}",
                notes={"show_id": str(show.id), "user_id": str(request.user.id)},
            )
        except Exception:  # Razorpay rejected the request (bad keys) or could not be reached
            logger.exception("Razorpay order creation failed for show %s", show.id)
            messages.error(
                request,
                "We couldn't start the payment right now. Please try again in a moment.",
            )
            return redirect("book_seats", show_id=show.id)
        payment = Payment.objects.create(
            user=request.user,
            show=show,
            seat_numbers=", ".join(seat.seat_number for seat in seats),
            amount=total_price,
            currency="INR",
            razorpay_order_id=order["id"],
        )
        payment.seats.set(seats)

    return render(
        request,
        "movies/checkout.html",
        {
            "show": show,
            "holds": holds,
            "payment": payment,
            "total_price": total_price,
            "expires_at": earliest_expiry,
            "razorpay_key_id": settings.RAZORPAY_KEY_ID,
        },
    )


def _finalize_booking_for_payment(
    payment_id, razorpay_payment_id=None, razorpay_signature=None
):
    """Idempotently turn a paid-for Payment's seats into real Bookings.

    Safe to call twice for the same payment — e.g. once from the browser's success
    callback and again from the async webhook. The second call sees status already
    'success' under the row lock and does nothing further, so duplicate confirmations can
    never create duplicate bookings. Booking.seat's OneToOneField is a further,
    database-enforced backstop even if that check were ever bypassed.
    """
    outcome = {}

    def _do():
        payment = (
            Payment.objects.select_for_update()
            .select_related("show")
            .get(id=payment_id)
        )
        if payment.status == Payment.STATUS_SUCCESS:
            outcome["result"] = "already_processed"
            return

        if razorpay_payment_id:
            payment.razorpay_payment_id = razorpay_payment_id
        if razorpay_signature:
            payment.razorpay_signature = razorpay_signature

        seats = list(Seat.objects.select_for_update().filter(payments=payment))
        failed = []
        created_bookings = []
        for seat in seats:
            if seat.is_booked:
                failed.append(seat.seat_number)
                continue
            try:
                booking = Booking.objects.create(
                    user=payment.user,
                    seat=seat,
                    movie=payment.show.movie,
                    theater=payment.show.theater,
                    show=payment.show,
                    payment=payment,
                )
            except IntegrityError:
                failed.append(seat.seat_number)
                continue
            seat.is_booked = True
            seat.save(update_fields=["is_booked"])
            created_bookings.append(booking)

        SeatHold.objects.filter(seat__in=seats, user=payment.user).delete()

        payment.status = Payment.STATUS_SUCCESS
        payment.paid_at = timezone.now()
        if failed:
            payment.failure_reason = (
                f"Seats already taken at confirmation time: {', '.join(failed)}"
            )[:255]
        payment.save(
            update_fields=[
                "status",
                "failure_reason",
                "razorpay_payment_id",
                "razorpay_signature",
                "paid_at",
                "updated_at",
            ]
        )

        if failed and created_bookings:
            outcome["result"] = "partial_failure"
        elif failed:
            outcome["result"] = "failure"
        else:
            outcome["result"] = "success"
        outcome["failed"] = failed

    _run_atomic_with_retry(_do)
    return outcome


def _mark_payment_unsuccessful(payment_id, status, reason="", razorpay_payment_id=None):
    """Mark a payment failed or cancelled and immediately release its held seats, so they
    become bookable by someone else right away instead of waiting out the hold window.
    """

    def _do():
        payment = Payment.objects.select_for_update().get(id=payment_id)
        if payment.status != Payment.STATUS_CREATED:
            return  # already resolved (success/failed/cancelled) via another path — leave it alone
        payment.status = status
        payment.failure_reason = reason[:255]
        if razorpay_payment_id:
            payment.razorpay_payment_id = razorpay_payment_id
        payment.save(
            update_fields=[
                "status",
                "failure_reason",
                "razorpay_payment_id",
                "updated_at",
            ]
        )
        SeatHold.objects.filter(
            seat__in=payment.seats.all(), user=payment.user
        ).delete()

    _run_atomic_with_retry(_do)


@login_required(login_url="/login/")
@require_POST
def verify_payment(request, payment_id):
    """Called by the browser (Razorpay Checkout.js 'handler' callback) right after the
    user completes payment. Verifies the cryptographic signature server-side before
    trusting anything the client sent, then finalizes the booking."""
    payment = get_object_or_404(Payment, id=payment_id, user=request.user)
    razorpay_payment_id = request.POST.get("razorpay_payment_id", "")
    razorpay_order_id = request.POST.get("razorpay_order_id", "")
    razorpay_signature = request.POST.get("razorpay_signature", "")

    if razorpay_order_id != payment.razorpay_order_id:
        return JsonResponse({"success": False, "error": "Order mismatch."}, status=400)

    try:
        verify_payment_signature(
            razorpay_order_id, razorpay_payment_id, razorpay_signature
        )
    except PaymentVerificationError:
        _mark_payment_unsuccessful(
            payment.id,
            Payment.STATUS_FAILED,
            reason="Signature verification failed",
            razorpay_payment_id=razorpay_payment_id,
        )
        return JsonResponse(
            {
                "success": False,
                "error": "Payment verification failed. Please try again.",
            },
            status=400,
        )

    outcome = _finalize_booking_for_payment(
        payment.id, razorpay_payment_id, razorpay_signature
    )
    result = outcome["result"]

    if result in ("success", "already_processed"):
        return JsonResponse({"success": True, "redirect_url": reverse("profile")})
    if result == "partial_failure":
        return JsonResponse(
            {
                "success": True,
                "redirect_url": reverse("profile"),
                "warning": (
                    f"Note: these seats were already taken by the time payment was confirmed: "
                    f"{', '.join(outcome['failed'])}. Please contact support for a refund on those."
                ),
            }
        )
    return JsonResponse(
        {
            "success": False,
            "error": (
                f"Sorry — these seats were already booked by someone else: "
                f"{', '.join(outcome['failed'])}. Your payment was captured; please contact "
                "support for a refund."
            ),
        },
        status=409,
    )


@login_required(login_url="/login/")
@require_POST
def cancel_payment(request, payment_id):
    """Called when the user dismisses the Razorpay modal without paying. Releases the
    held seats immediately instead of making other users wait out the full hold window.
    """
    payment = get_object_or_404(Payment, id=payment_id, user=request.user)
    _mark_payment_unsuccessful(
        payment.id, Payment.STATUS_CANCELLED, reason="Cancelled by user"
    )
    return JsonResponse({"success": True})


@csrf_exempt
@require_POST
def razorpay_webhook(request):
    """Server-to-server notification from Razorpay — the authoritative source of truth
    for payment status. The browser's verify_payment call gives a fast redirect, but if
    the user closes the tab right after paying, this webhook is what still confirms the
    booking, so a completed payment is never lost."""
    signature = request.headers.get("X-Razorpay-Signature", "")
    body = request.body
    try:
        verify_webhook_signature(body, signature)
    except PaymentVerificationError:
        return HttpResponse(status=400)

    try:
        event = json.loads(body.decode("utf-8"))
    except (ValueError, TypeError, UnicodeDecodeError):
        return HttpResponse(status=400)

    event_type = event.get("event", "")
    payload_entity = event.get("payload", {}).get("payment", {}).get("entity", {})
    order_id = payload_entity.get("order_id")
    razorpay_payment_id = payload_entity.get("id")

    if not order_id:
        return HttpResponse(
            status=200
        )  # nothing actionable — acknowledge so Razorpay stops retrying

    try:
        payment = Payment.objects.get(razorpay_order_id=order_id)
    except Payment.DoesNotExist:
        return HttpResponse(status=200)

    if event_type == "payment.captured":
        _finalize_booking_for_payment(payment.id, razorpay_payment_id)
    elif event_type == "payment.failed":
        reason = payload_entity.get("error_description") or "Payment failed"
        _mark_payment_unsuccessful(
            payment.id,
            Payment.STATUS_FAILED,
            reason=reason,
            razorpay_payment_id=razorpay_payment_id,
        )

    return HttpResponse(status=200)


@login_required(login_url="/login/")
def payment_history(request):
    """Full payment + booking history for the logged-in user's profile."""
    payments = (
        Payment.objects.filter(user=request.user)
        .select_related("show", "show__movie", "show__theater")
        .prefetch_related("bookings")
        .order_by("-created_at")
    )
    return render(request, "movies/payment_history.html", {"payments": payments})


@login_required(login_url="/login/")
def book_seats_legacy(request, theater_id):
    """Kept for any legacy Theater rows that don't yet have a matching Show."""
    theater = get_object_or_404(Theater, id=theater_id)
    seats = Seat.objects.filter(theater=theater, show__isnull=True)
    if request.method == "POST":
        selected_seats = request.POST.getlist("seats")
        error_seats = []
        if not selected_seats:
            return render(
                request,
                "movies/seat_selection.html",
                {"theater": theater, "seats": seats, "error": "No seat selected"},
            )
        for seat_id in selected_seats:
            seat = get_object_or_404(Seat, id=seat_id, theater=theater)
            if seat.is_booked:
                error_seats.append(seat.seat_number)
                continue
            try:
                Booking.objects.create(
                    user=request.user, seat=seat, movie=theater.movie, theater=theater
                )
                seat.is_booked = True
                seat.save()
            except IntegrityError:
                error_seats.append(seat.seat_number)
        if error_seats:
            error_message = (
                f"The following seats are already booked: {', '.join(error_seats)}"
            )
            return render(
                request,
                "movies/seat_selection.html",
                {"theater": theater, "seats": seats, "error": error_message},
            )
        return redirect("profile")
    return render(
        request, "movies/seat_selection.html", {"theater": theater, "seats": seats}
    )
