import time

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone
from django.views.decorators.http import require_POST

from .forms import ReviewForm, ReviewReportForm
from .models import Booking, Movie, Review, Seat, SeatHold, Show, Theater


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
    search_query = request.GET.get("search")
    movies = Movie.objects.all()
    if search_query:
        movies = movies.filter(name__icontains=search_query)
    return render(request, "movies/movie_list.html", {"movies": movies})


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
    """Summary screen: shows the seats the user currently has on hold, the total price,
    and a countdown to when the hold expires."""
    show = get_object_or_404(Show, id=show_id)
    _purge_expired_holds(show)
    holds = (
        SeatHold.objects.filter(show=show, user=request.user)
        .select_related("seat")
        .order_by("seat__seat_number")
    )
    if not holds:
        messages.info(
            request, "Your seat selection has expired. Please choose your seats again."
        )
        return redirect("book_seats", show_id=show.id)

    earliest_expiry = min(hold.expires_at for hold in holds)
    total_price = show.price * holds.count()
    return render(
        request,
        "movies/checkout.html",
        {
            "show": show,
            "holds": holds,
            "total_price": total_price,
            "expires_at": earliest_expiry,
        },
    )


@login_required(login_url="/login/")
@require_POST
def confirm_booking(request, show_id):
    """Finalize payment: turn this user's still-valid holds into real Bookings.

    Everything happens inside one atomic transaction with row-level locks on the seats
    being booked, so if two requests somehow race to confirm the same seat, only one can
    succeed — the Booking.seat OneToOneField's unique constraint is a further,
    database-enforced guarantee that a seat is never double-booked.
    """
    show = get_object_or_404(Show, id=show_id)
    outcome = {}

    def _do_confirm():
        holds = list(
            SeatHold.objects.select_for_update()
            .select_related("seat")
            .filter(show=show, user=request.user)
        )
        if not holds:
            outcome["result"] = "no_holds"
            return

        expired = [h for h in holds if h.is_expired()]
        if expired:
            SeatHold.objects.filter(id__in=[h.id for h in expired]).delete()
            outcome["result"] = "expired"
            return

        seat_ids = [h.seat_id for h in holds]
        seats = Seat.objects.select_for_update().filter(id__in=seat_ids)
        failed = []
        created_bookings = []
        for seat in seats:
            if seat.is_booked:
                failed.append(seat.seat_number)
                continue
            try:
                booking = Booking.objects.create(
                    user=request.user,
                    seat=seat,
                    movie=show.movie,
                    theater=show.theater,
                    show=show,
                )
            except IntegrityError:
                failed.append(seat.seat_number)
                continue
            seat.is_booked = True
            seat.save(update_fields=["is_booked"])
            created_bookings.append(booking)

        SeatHold.objects.filter(seat_id__in=seat_ids, user=request.user).delete()

        if failed:
            outcome["result"] = "partial_failure" if created_bookings else "failure"
            outcome["failed"] = failed
        else:
            outcome["result"] = "success"

    try:
        _run_atomic_with_retry(_do_confirm)
    except OperationalError:
        messages.error(
            request, "The system is busy — please try confirming your booking again."
        )
        return redirect("checkout", show_id=show.id)

    result = outcome.get("result")
    if result == "no_holds":
        messages.error(
            request,
            "Your seat selection expired before payment was completed. Please reselect your seats.",
        )
        return redirect("book_seats", show_id=show.id)
    if result == "expired":
        messages.error(
            request,
            "Some of your selected seats' hold time ran out. Please reselect your seats.",
        )
        return redirect("book_seats", show_id=show.id)
    if result in ("failure", "partial_failure"):
        failed = ", ".join(outcome.get("failed", []))
        if result == "partial_failure":
            messages.error(
                request,
                f"Sorry — these seats were just booked by someone else: {failed}. "
                "The rest of your seats were booked successfully.",
            )
        else:
            messages.error(
                request,
                f"Sorry — these seats were just booked by someone else: {failed}.",
            )
        return redirect("book_seats", show_id=show.id)

    messages.success(request, "Payment successful — your seats are booked!")
    return redirect("profile")


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
