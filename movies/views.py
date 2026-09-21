from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError
from django.db.models import Q
from django.shortcuts import get_object_or_404, redirect, render
from django.utils import timezone

from .forms import ReviewForm, ReviewReportForm
from .models import Booking, Movie, Review, Seat, Show, Theater


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


@login_required(login_url="/login/")
def book_seats(request, show_id):
    show = get_object_or_404(Show, id=show_id)
    seats = Seat.objects.filter(show=show)
    if request.method == "POST":
        selected_seats = request.POST.getlist("seats")
        error_seats = []
        if not selected_seats:
            return render(
                request,
                "movies/seat_selection.html",
                {"show": show, "seats": seats, "error": "No seat selected"},
            )
        for seat_id in selected_seats:
            seat = get_object_or_404(Seat, id=seat_id, show=show)
            if seat.is_booked:
                error_seats.append(seat.seat_number)
                continue
            try:
                Booking.objects.create(
                    user=request.user,
                    seat=seat,
                    movie=show.movie,
                    theater=show.theater,
                    show=show,
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
                {"show": show, "seats": seats, "error": error_message},
            )
        return redirect("profile")
    return render(request, "movies/seat_selection.html", {"show": show, "seats": seats})


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
