from django.conf import settings
from django.core.validators import MaxValueValidator, MinValueValidator, RegexValidator
from django.db import models
from django.db.models import Avg, Count
from django.utils import timezone

# YouTube video IDs are always 11 characters of [A-Za-z0-9_-].
# We only ever store the ID (never a raw <iframe>/script snippet coming from a user),
# and we always render the trailer ourselves via a fixed, sandboxed embed URL. This
# keeps trailer embedding safe against injected markup/XSS from admin- or user-supplied text.
youtube_id_validator = RegexValidator(
    regex=r"^[A-Za-z0-9_-]{11}$",
    message="Enter a valid 11-character YouTube video ID (not a full URL).",
)


def extract_youtube_id(value):
    """Best-effort extraction of an 11-char YouTube video ID from a pasted URL or raw ID."""
    if not value:
        return value
    value = value.strip()
    import re

    patterns = [
        r"(?:youtube\.com/watch\?v=|youtube\.com/embed/|youtu\.be/|youtube\.com/shorts/)([A-Za-z0-9_-]{11})",
    ]
    for pattern in patterns:
        match = re.search(pattern, value)
        if match:
            return match.group(1)
    return value


class Genre(models.Model):
    name = models.CharField(max_length=100, unique=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Language(models.Model):
    name = models.CharField(max_length=100, unique=True)
    code = models.CharField(
        max_length=10, blank=True, help_text="ISO code, e.g. en, hi"
    )

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class CastMember(models.Model):
    ROLE_ACTOR = "actor"
    ROLE_DIRECTOR = "director"
    ROLE_PRODUCER = "producer"
    ROLE_WRITER = "writer"
    ROLE_CHOICES = [
        (ROLE_ACTOR, "Actor"),
        (ROLE_DIRECTOR, "Director"),
        (ROLE_PRODUCER, "Producer"),
        (ROLE_WRITER, "Writer"),
    ]

    name = models.CharField(max_length=255)
    photo = models.ImageField(upload_to="cast/", blank=True, null=True)
    bio = models.TextField(blank=True)

    class Meta:
        ordering = ["name"]

    def __str__(self):
        return self.name


class Movie(models.Model):
    CERT_U = "U"
    CERT_UA = "UA"
    CERT_A = "A"
    CERT_S = "S"
    CERTIFICATION_CHOICES = [
        (CERT_U, "U - Universal"),
        (CERT_UA, "UA - Parental Guidance"),
        (CERT_A, "A - Adults Only"),
        (CERT_S, "S - Special"),
    ]

    name = models.CharField(max_length=255)
    tagline = models.CharField(max_length=255, blank=True)
    image = models.ImageField(upload_to="movies/", help_text="Primary poster image")
    description = models.TextField(blank=True, null=True)
    duration_minutes = models.PositiveIntegerField(
        default=0, help_text="Runtime in minutes"
    )
    age_certification = models.CharField(
        max_length=2, choices=CERTIFICATION_CHOICES, default=CERT_UA
    )
    release_date = models.DateField(default=timezone.now)

    genres = models.ManyToManyField(Genre, related_name="movies", blank=True)
    languages = models.ManyToManyField(Language, related_name="movies", blank=True)

    # Legacy free-text field kept so existing data/admin flows keep working.
    cast = models.TextField(
        blank=True,
        help_text="Legacy free-text cast list (optional now that Cast Members are supported)",
    )
    cast_members = models.ManyToManyField(
        CastMember, through="MovieCastCredit", related_name="movies", blank=True
    )

    trailer_youtube_id = models.CharField(
        max_length=20,
        blank=True,
        validators=[youtube_id_validator],
        help_text="Paste the YouTube URL or just the 11-character video ID.",
    )

    # Legacy manually-set rating (kept for backward compatibility / fallback display).
    rating = models.DecimalField(max_digits=3, decimal_places=1, default=0)
    average_rating = models.DecimalField(
        max_digits=3, decimal_places=2, default=0, editable=False
    )
    ratings_count = models.PositiveIntegerField(default=0, editable=False)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-release_date", "name"]

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.trailer_youtube_id:
            self.trailer_youtube_id = extract_youtube_id(self.trailer_youtube_id)
        super().save(*args, **kwargs)

    @property
    def trailer_embed_url(self):
        """Safe, sandboxed embed URL. Never build this from raw user HTML."""
        if not self.trailer_youtube_id:
            return None
        return f"https://www.youtube-nocookie.com/embed/{self.trailer_youtube_id}"

    @property
    def display_rating(self):
        """The rating to show to users: review average once there is one, else the legacy rating."""
        return self.average_rating if self.ratings_count else self.rating

    def recalculate_rating(self):
        agg = self.reviews.filter(is_hidden=False).aggregate(
            avg=Avg("rating"), count=Count("id")
        )
        self.average_rating = agg["avg"] or 0
        self.ratings_count = agg["count"] or 0
        self.save(update_fields=["average_rating", "ratings_count"])

    def similar_movies(self, limit=8):
        genre_ids = list(self.genres.values_list("id", flat=True))
        language_ids = list(self.languages.values_list("id", flat=True))
        if not genre_ids and not language_ids:
            return Movie.objects.none()
        return (
            Movie.objects.filter(
                models.Q(genres__in=genre_ids) | models.Q(languages__in=language_ids)
            )
            .exclude(id=self.id)
            .distinct()[:limit]
        )

    @classmethod
    def trending(cls, limit=8, days=30):
        cutoff = timezone.now() - timezone.timedelta(days=days)
        return (
            cls.objects.filter(shows__date_time__gte=cutoff)
            .annotate(booking_count=Count("shows__bookings", distinct=True))
            .filter(booking_count__gt=0)
            .order_by("-booking_count", "-average_rating")
            .distinct()[:limit]
        )

    @classmethod
    def recently_released(cls, limit=8):
        return cls.objects.filter(release_date__lte=timezone.now().date()).order_by(
            "-release_date"
        )[:limit]


class MovieCastCredit(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="credits")
    cast_member = models.ForeignKey(
        CastMember, on_delete=models.CASCADE, related_name="credits"
    )
    character_name = models.CharField(max_length=255, blank=True)
    role_type = models.CharField(
        max_length=20, choices=CastMember.ROLE_CHOICES, default=CastMember.ROLE_ACTOR
    )
    billing_order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["billing_order", "id"]
        unique_together = ("movie", "cast_member", "role_type")

    def __str__(self):
        return f"{self.cast_member.name} in {self.movie.name}"


class MoviePoster(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="posters")
    image = models.ImageField(upload_to="movies/posters/")
    caption = models.CharField(max_length=255, blank=True)
    is_primary = models.BooleanField(default=False)
    order = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ["order", "id"]

    def __str__(self):
        return f"Poster for {self.movie.name}"

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        if self.is_primary:
            MoviePoster.objects.filter(movie=self.movie).exclude(id=self.id).update(
                is_primary=False
            )


class Theater(models.Model):
    """A physical venue/screen. Show schedules (movie + time + language + format) live on Show."""

    name = models.CharField(max_length=255)
    city = models.CharField(max_length=100, blank=True)
    address = models.CharField(max_length=255, blank=True)

    # --- Legacy fields kept only so old rows/migrations keep working; new code should
    # use the Show model instead of these. ---
    movie = models.ForeignKey(
        Movie, on_delete=models.CASCADE, related_name="theaters", null=True, blank=True
    )
    time = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return self.name


class Show(models.Model):
    FORMAT_2D = "2D"
    FORMAT_3D = "3D"
    FORMAT_IMAX = "IMAX"
    FORMAT_CHOICES = [
        (FORMAT_2D, "2D"),
        (FORMAT_3D, "3D"),
        (FORMAT_IMAX, "IMAX"),
    ]

    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="shows")
    theater = models.ForeignKey(Theater, on_delete=models.CASCADE, related_name="shows")
    language = models.ForeignKey(
        Language, on_delete=models.SET_NULL, null=True, blank=True, related_name="shows"
    )
    screen_format = models.CharField(
        max_length=10, choices=FORMAT_CHOICES, default=FORMAT_2D
    )
    date_time = models.DateTimeField()
    price = models.DecimalField(max_digits=8, decimal_places=2, default=200)

    class Meta:
        ordering = ["date_time"]

    def __str__(self):
        return f"{self.movie.name} @ {self.theater.name} on {self.date_time:%d %b, %I:%M %p}"

    @property
    def has_started(self):
        return timezone.now() >= self.date_time


class Seat(models.Model):
    # Legacy link, kept for old rows.
    theater = models.ForeignKey(
        Theater, on_delete=models.CASCADE, related_name="seats", null=True, blank=True
    )
    show = models.ForeignKey(
        Show, on_delete=models.CASCADE, related_name="seats", null=True, blank=True
    )
    seat_number = models.CharField(max_length=10)
    is_booked = models.BooleanField(default=False)

    def __str__(self):
        return self.seat_number


class Booking(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="bookings"
    )
    seat = models.OneToOneField(Seat, on_delete=models.CASCADE)
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="bookings")
    theater = models.ForeignKey(
        Theater, on_delete=models.CASCADE, null=True, blank=True
    )
    show = models.ForeignKey(
        Show, on_delete=models.CASCADE, null=True, blank=True, related_name="bookings"
    )
    booked_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Booking by {self.user.username} for {self.seat.seat_number}"

    @property
    def show_datetime(self):
        if self.show_id:
            return self.show.date_time
        if self.theater_id:
            return self.theater.time
        return None

    def has_been_watched(self):
        """A booking counts as 'watched' once its showtime has passed."""
        when = self.show_datetime
        if when is None:
            return True  # legacy booking with no known showtime — don't block reviewing
        return timezone.now() >= when


class Review(models.Model):
    movie = models.ForeignKey(Movie, on_delete=models.CASCADE, related_name="reviews")
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="reviews"
    )
    booking = models.ForeignKey(
        Booking, on_delete=models.SET_NULL, null=True, blank=True, related_name="review"
    )
    rating = models.PositiveSmallIntegerField(
        validators=[MinValueValidator(1), MaxValueValidator(5)]
    )
    title = models.CharField(max_length=255, blank=True)
    body = models.TextField()
    is_verified_viewer = models.BooleanField(
        default=False,
        editable=False,
        help_text="Set automatically when the reviewer has a completed booking for this movie.",
    )
    is_hidden = models.BooleanField(
        default=False, help_text="Hide from public display (e.g. after moderation)."
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        unique_together = ("movie", "user")

    def __str__(self):
        return f"{self.user.username}'s review of {self.movie.name}"

    def report_count(self):
        return self.reports.count()

    def save(self, *args, **kwargs):
        super().save(*args, **kwargs)
        self.movie.recalculate_rating()

    def delete(self, *args, **kwargs):
        movie = self.movie
        super().delete(*args, **kwargs)
        movie.recalculate_rating()


class ReviewReport(models.Model):
    REASON_SPAM = "spam"
    REASON_ABUSE = "abuse"
    REASON_SPOILER = "spoiler"
    REASON_OTHER = "other"
    REASON_CHOICES = [
        (REASON_SPAM, "Spam"),
        (REASON_ABUSE, "Abusive or inappropriate"),
        (REASON_SPOILER, "Unmarked spoiler"),
        (REASON_OTHER, "Other"),
    ]

    review = models.ForeignKey(Review, on_delete=models.CASCADE, related_name="reports")
    reported_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name="review_reports",
    )
    reason = models.CharField(
        max_length=10, choices=REASON_CHOICES, default=REASON_OTHER
    )
    comment = models.TextField(blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ("review", "reported_by")

    def __str__(self):
        return f"Report on review #{self.review_id} by {self.reported_by}"
